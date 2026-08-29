"""rerank：复用 chat 模型，但它是一次**独立调用**（S141 / S147）。

三条缺一不可：独立调用、独立缓存空间、提示词归「单次调用」那一类。
"""

from __future__ import annotations

import pytest

from agenttavern.corpus.rerank import RERANK_CACHE_SPACE, rerank_passages
from agenttavern.corpus.search import Hit
from agenttavern.prompting import AGENTS, CALLS, PROMPT_FILES

# -- 替身 ---------------------------------------------------------------------


class FakeGateway:
    """记下每次调用的消息与缓存空间。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def complete(self, messages, *, user_id=None, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({"messages": list(messages), "user_id": user_id})

        class _R:
            content = self.reply
            tool_calls: list[object] = []

        return _R()


def hits(*indices: int) -> list[Hit]:
    return [Hit(chunk_index=i, text=f"第{i}段", coverage=0.9) for i in indices]


# -- S141 独立调用 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_rerank_is_its_own_call_not_appended_to_the_director() -> None:
    """**它只看 [query + 候选段落]**，不带 director 那一轮的任何东西（S141）。

    接在 director 对话后面的话，它会读到整轮推演，于是"哪一段最相关"变成
    "哪一段最符合我已经想好的走向"——那是自我确认（P20）。
    """
    gateway = FakeGateway("2,0,1")
    await rerank_passages(gateway, query="尚书省", candidates=hits(0, 1, 2), limit=3)

    [call] = gateway.calls
    blob = "".join(str(m.content) for m in call["messages"])  # type: ignore[attr-defined]
    assert "尚书省" in blob and "第0段" in blob
    assert "细纲" not in blob and "上一轮" not in blob, "director 的上下文不能漏进来"


@pytest.mark.asyncio
async def test_rerank_uses_its_own_cache_space() -> None:
    """**不传 session 的 id**（S141）。

    它的静态前缀跨会话完全相同，独占一个空间能最大化复用；混进 session 的
    空间只会污染那边的缓存命中率——这个项目栽过一次（P29）。
    """
    gateway = FakeGateway("0,1")
    await rerank_passages(gateway, query="q", candidates=hits(0, 1), limit=2)
    assert gateway.calls[0]["user_id"] == RERANK_CACHE_SPACE
    assert RERANK_CACHE_SPACE != "", "空的等于不传，那就是所有会话挤在同一个空间"


@pytest.mark.asyncio
async def test_rerank_orders_rather_than_scores() -> None:
    """**给它候选让它排序，不问「这一段几分」**（P10）。

    打分是开放判断，排序是受约束的选择。
    """
    gateway = FakeGateway("2,0,1")
    out = await rerank_passages(gateway, query="q", candidates=hits(0, 1, 2), limit=3)
    assert [h.chunk_index for h in out] == [2, 0, 1]


# -- 稳健性：模型乱答时不能丢东西 ----------------------------------------------


@pytest.mark.asyncio
async def test_a_candidate_the_model_forgot_is_appended_not_dropped() -> None:
    """**rerank 只能重排，不能丢**（P34）。

    模型漏掉一条时把它接在后面——丢掉是一次**静默的降级**：那一段本来通过了
    阈值，是模型排序时忘了提，而不是它不相关。
    """
    gateway = FakeGateway("2,0")  # 漏了 1
    out = await rerank_passages(gateway, query="q", candidates=hits(0, 1, 2), limit=3)
    assert [h.chunk_index for h in out] == [2, 0, 1], "漏掉的要补在后面，不能消失"


@pytest.mark.asyncio
async def test_a_nonsense_reply_falls_back_to_the_original_order() -> None:
    """模型答得完全读不懂 → **退回原顺序**，不是抛异常、也不是返回空。

    rerank 是**锦上添花的一步**：它坏掉时整条链路应该退化成"没 rerank"，
    而不是让检索整个失败。
    """
    gateway = FakeGateway("我觉得都挺好的")
    out = await rerank_passages(gateway, query="q", candidates=hits(5, 6), limit=2)
    assert [h.chunk_index for h in out] == [5, 6]


@pytest.mark.asyncio
async def test_out_of_range_and_duplicate_indices_are_ignored() -> None:
    """越界与重复的序号直接忽略——模型时常多写一个数字。"""
    gateway = FakeGateway("1, 1, 9, 0")
    out = await rerank_passages(gateway, query="q", candidates=hits(3, 4), limit=2)
    assert [h.chunk_index for h in out] == [4, 3]


@pytest.mark.asyncio
async def test_one_candidate_needs_no_model_call() -> None:
    """只有一条候选时**不发这次调用**——排序一条东西没有意义，而它要花钱。"""
    gateway = FakeGateway("0")
    out = await rerank_passages(gateway, query="q", candidates=hits(7), limit=3)
    assert [h.chunk_index for h in out] == [7]
    assert gateway.calls == [], "一条候选不该触发模型调用"


@pytest.mark.asyncio
async def test_it_never_returns_more_than_asked() -> None:
    gateway = FakeGateway("0,1,2,3")
    out = await rerank_passages(gateway, query="q", candidates=hits(0, 1, 2, 3), limit=2)
    assert len(out) == 2


# -- S147 提示词分两类：agent 与单次调用 ---------------------------------------


def test_an_agent_is_a_noun_and_a_single_call_is_a_verb() -> None:
    """**名字就是分界线**（S147）。

    agent 是一个角色——会推演、会产出剧情、有自己的上下文与工具面，跨轮次一直
    在那儿，所以用名词。单次调用是一次动作——输入固定、输出固定、不参与剧情，
    做完就没了，所以用动词。

    `summarize` 曾经叫 `summarizer`，于是一直被算作「第五个 agent」。**那个误解
    会影响判断**：比如以为该给它工具面、以为它该读世界设定。

    这条断言钉死两份名单，是**故意的路障**：加一份 YAML 时它会红，读到这里
    才知道该用哪种词性、该进哪一类。
    """
    assert set(AGENTS) == {"director", "writer", "state_manager", "goal_manager"}
    assert set(CALLS) == {"summarize", "rerank"}
    assert not set(AGENTS) & set(CALLS)
    assert (*AGENTS, *CALLS) == PROMPT_FILES


def test_a_single_call_is_still_editable() -> None:
    """两类都可编辑——R31 管的是「模型会读到的字」，不分是谁在读。"""
    from agenttavern.prompting import load

    for name in CALLS:
        assert load(name).get("sections"), f"{name} 应当有可编辑的 sections"
