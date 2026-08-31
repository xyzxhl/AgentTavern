"""专业 reranker 接进检索链路：**它能否决**（S141 / S148 / S139）。

这是 chat 模型那一版做不到的事——它只能重排。而"同领域但答不上"那一档
既拦不住又最危险：模型拿到几段答非所问的原文，**会当成事实写进剧情**。

一条贯穿的原则：**rerank 是锦上添花的一步，它坏掉不该让检索失败。**
所以"否决"和"出错"必须分得开——前者是结论，后者是降级。
"""

from __future__ import annotations

import pytest

from agenttavern.config import LIMITS
from agenttavern.corpus.bm25 import BM25Index
from agenttavern.corpus.lookup import Source, look_up
from agenttavern.corpus.rerankers import RerankError, Scored
from agenttavern.models.corpus import Chunk

# 三块**都含「长官」**：查询 `长官` 时三条全是候选，
# 于是"留下几条"完全由 rerank 的分数决定，不掺混合检索那一层的取舍。
PIECES = [
    "尚书省掌管六部政令，长官为尚书令。",
    "中书省负责起草诏令，长官为中书令。",
    "门下省负责审核封驳，长官为侍中。",
]


def source() -> Source:
    text, chunks, at = "", [], 0
    for i, piece in enumerate(PIECES):
        chunks.append(Chunk(index=i, start=at, end=at + len(piece), text=piece))
        text += piece
        at += len(piece)
    return Source(
        corpus_id="cp_x",
        name="唐代官制",
        chunks=chunks,
        bm25=BM25Index.build([c.text for c in chunks]),
    )


class FakeReranker:
    """按给定的分数回话。**分数是这一层唯一的输入**。"""

    provider = "qwen"

    def __init__(self, scores: list[float] | None = None, fails: bool = False) -> None:
        self.scores = scores
        self.fails = fails
        self.calls = 0

    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[Scored]:
        self.calls += 1
        if self.fails:
            raise RerankError("qwen rerank 回了 500")
        scores = self.scores or [0.9] * len(documents)
        out = [
            Scored(index=i, score=s, provider=self.provider)
            for i, s in enumerate(scores[: len(documents)])
        ]
        return sorted(out, key=lambda s: -s.score)[:top_n]


class NeverCalled:
    """chat 那条路的替身。**被叫到就是错**。"""

    async def complete(self, messages, *, user_id=None, **kw):  # type: ignore[no-untyped-def]
        raise AssertionError("配了专业 reranker 就不该再走 chat 那条路")


async def run(reranker, **kw):
    return await look_up([source()], query="长官", gateway=NeverCalled(), reranker=reranker, **kw)


# -- 否决 ----------------------------------------------------------------------


async def test_everything_below_the_threshold_is_a_miss() -> None:
    """**全都不够相关 → 一段都不返回**（S141/S148）。

    这正是 chat 那一版做不到的事。空的 `passages` 由调用方说成
    「这次没找到」（S139）——而那比端上几段答非所问的原文好得多。
    """
    lookup = await run(FakeReranker([0.4, 0.3, 0.2]))
    assert lookup.passages == []
    assert lookup.stages["rejected"] == 3


async def test_only_the_ones_above_the_threshold_come_back() -> None:
    """够格的留下，不够的丢掉——**部分否决也是否决**。"""
    lookup = await run(FakeReranker([0.9, 0.2, 0.8]))
    assert [p.chunk_index for p in lookup.passages] == [0, 2]
    assert lookup.stages["rejected"] == 1


async def test_the_threshold_comes_from_config() -> None:
    """阈值住在 `config.yaml`，**不写死在代码里**。

    它绑死在「哪一家 + 哪个模型」上，换任何一样都要重量——一个写死的数
    会让"换了 reranker"这件事悄无声息地改变整条链路的松紧。
    """
    floor = LIMITS.corpus.min_relevance
    assert (await run(FakeReranker([floor + 0.01]))).passages, "**刚好过线要留下**"
    assert not (await run(FakeReranker([floor - 0.01]))).passages

    # 显式传参能覆盖——bench 要在同一批素材上跑多档（P19）。
    assert (await run(FakeReranker([0.3]), min_relevance=0.2)).passages


async def test_the_scores_and_who_gave_them_land_in_the_trace() -> None:
    """**分数带着是谁给的**（S141）。

    换一家而阈值照抄，整条链路的松紧就变了且没有任何地方会报错——
    trace 里记着 provider，那件事才查得出来。
    """
    lookup = await run(FakeReranker([0.9, 0.2, 0.8]))
    trace = lookup.stages
    assert trace["reranker"] == "qwen"
    assert [round(s, 2) for s in trace["scores"]] == [0.9, 0.8], "留下的那几条的分"
    assert trace["threshold"] == LIMITS.corpus.min_relevance


# -- 出错 ≠ 否决 ---------------------------------------------------------------


async def test_a_rerank_failure_degrades_to_the_fusion_order() -> None:
    """**它坏掉时退回融合排序，不是判成「没找到」**。

    出错和否决是两件事：否决是"看过了，都不合适"，出错是"根本没看成"。
    把后者当成前者，一次网络抖动就会让模型收到一句「这次没找到」——
    而那是**假的结论**（同 S139 那条硬线：不下没有资格下的断言）。
    """
    lookup = await run(FakeReranker(fails=True))

    assert len(lookup.passages) == len(PIECES), "一条都不该丢"
    assert lookup.degraded, "降级要报给用户"
    assert any("rerank" in reason for reason in lookup.degraded)
    assert lookup.stages["rejected"] == 0, "没看成，谈不上否决"


async def test_a_failure_does_not_fall_back_to_the_chat_reranker() -> None:
    """**失败就退到"没 rerank"，不换一个 reranker 重试。**

    退到 chat 那条路要多花一次调用的时间，而且更糟的是：一次网络抖动会
    **悄悄换掉产出这份排序的算法**——那正是 P28 说的"偷偷加了一个变量"，
    此后任何一次前后对照都不成立了。

    `NeverCalled` 是 gateway 的替身，被叫到就抛。
    """
    lookup = await run(FakeReranker(fails=True))
    assert lookup.passages, "退化成融合排序，不是失败"


# -- 没配专业 reranker 时仍然走 chat 那条路 ------------------------------------


async def test_without_a_reranker_the_chat_path_still_runs() -> None:
    """**没配就退回 chat 模型重排**（S141）——它不需要任何额外端点。

    那一版只能排序不能否决，所以这时一段都不会被判掉。
    """

    class Chat:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, user_id=None, **kw):  # type: ignore[no-untyped-def]
            self.calls += 1

            class _R:
                content = "2,0,1"
                tool_calls: list[object] = []

            return _R()

    chat = Chat()
    lookup = await look_up([source()], query="长官", gateway=chat, reranker=None)

    assert chat.calls == 1
    assert [p.chunk_index for p in lookup.passages] == [2, 0, 1]
    assert lookup.stages["rejected"] == 0, "chat 那一版不能否决"
    assert "reranker" not in lookup.stages


@pytest.mark.parametrize("floor", [0.0, 1.0])
async def test_the_two_extremes_behave_sanely(floor: float) -> None:
    """阈值取两个极端时不该出怪事：0 全留，1 全否决。"""
    lookup = await run(FakeReranker([0.9, 0.5, 0.1]), min_relevance=floor)
    assert len(lookup.passages) == (3 if floor == 0.0 else 0)
