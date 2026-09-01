"""工具层：契约、可恢复错误、暂存语义、懒惰摘要。"""

import re
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import using

from agenttavern.config import Settings
from agenttavern.llm import LLMGateway, ToolCall
from agenttavern.models import (
    EntryKind,
    Node,
    Session,
    StateDelta,
    WorldbookEntry,
    WorldbookMode,
)
from agenttavern.storage import Store
from agenttavern.tools import (
    RunContext,
    ToolOutcome,
    ToolRegistry,
    build_state_tools,
    build_tools,
)
from agenttavern.tools.builtin import MAX_READ_CHARS


def fake_gateway(reply: str = "摘要文本") -> LLMGateway:
    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return LLMGateway(Settings(llm_model="m"), http_client=client)


@pytest.fixture
def ctx(tmp_path: Path) -> RunContext:
    repo = Store(tmp_path).create_session(Session())
    return RunContext(
        run_id="run_test",
        repo=repo,
        session=repo.load_session(),
        gateway=fake_gateway(),
    )


async def call(reg: ToolRegistry, ctx: RunContext, name: str, **args: Any):
    return await reg.dispatch(ctx, ToolCall(id="c1", name=name, arguments=args))


ALL_ON = {"tools": {"director": {"history_search": True}}}
"""全部开关打开。**开关住在各 agent 的 YAML 里**（S80），所以"打开"
现在是换一份提示词副本（`using`），不是往 session 上写一个对象。
"""


@pytest.fixture
def reg() -> ToolRegistry:
    """全部能力打开，**两个 agent 的工具面并在一起**。

    这个 fixture 测的是工具本身怎么工作，不是谁能看到它。`state_write` 从 S7
    起只在 state_manager 那一面（见文末的开关测试），但它的行为该在这里测。
    默认配置的工具面要小得多。
    """
    with using(**ALL_ON):
        return ToolRegistry(
            build_tools([WorldbookEntry(name="占位", mode=WorldbookMode.ON_DEMAND)])
            + build_state_tools()
        )


# -- 契约 -------------------------------------------------------------------


async def test_submit_outline_has_no_precondition(reg, ctx) -> None:
    """**E0 契约已作废**（S7）。

    它原先要求提交细纲前必须写过一次 `state_write`，为的是区分"确实没变化"与
    "模型忘了提交"。director 不再维护状态，这个前置条件也就没有对象了。
    """
    ok = await call(reg, ctx, "submit_outline", outline="她推开门。")
    assert ok.outcome is ToolOutcome.TERMINATE
    assert ctx.outline == "她推开门。"


async def test_an_empty_write_is_legal(reg, ctx) -> None:
    """不带参数调用是合法的，只是不再有谁**要求**它非调不可。"""
    await call(reg, ctx, "state_write")
    assert ctx.state_write_count == 1
    assert ctx.staged_delta.is_empty()


# -- 暂存与累积 --------------------------------------------------------------


async def test_state_writes_accumulate_into_one_delta(reg, ctx) -> None:
    """可多次调用，累积成一份增量——节点与 run 一一对应，一个 run 只有一份增量。"""
    await call(reg, ctx, "state_write", set={"地点": "门口"}, append={"林": {"知情": ["有人来了"]}})
    await call(reg, ctx, "state_write", set={"地点": "客厅"}, append={"林": {"知情": ["是陈"]}})

    assert ctx.state_write_count == 2
    assert ctx.staged_delta.set["地点"] == "客厅"
    assert ctx.staged_delta.append["林"]["知情"] == ["有人来了", "是陈"]


async def test_unmentioned_keys_survive(reg, ctx) -> None:
    """离场再出场天然成立：只改提到的键，其余保持原状。不需要"角色实体"这类预设。"""
    await call(
        reg, ctx, "state_write", set={"陈": {"在场": True}}, append={"陈": {"知情": ["照片"]}}
    )
    await call(reg, ctx, "state_write", set={"陈": {"在场": False}})

    after = ctx.current_state().values
    assert after["陈"]["在场"] is False
    # **深合并**：只动提到的那一项，同一个角色名下别的原样留着（S31）。
    assert after["陈"]["知情"] == ["照片"]

    await call(reg, ctx, "state_write", set={"陈": {"在场": True}})
    assert ctx.current_state().values["陈"]["在场"] is True


async def test_any_shape_of_key_is_accepted(reg, ctx) -> None:
    """时间、地点、角色属性的粒度与形态都取决于故事，存储层不预设任何维度。

    从属关系用嵌套，深浅由模型自己定（S31）：这里「地点」有一层，
    「林与陈」也有一层，而「时间」就是个裸值——三种都合法。
    """
    await call(
        reg,
        ctx,
        "state_write",
        set={"时间": "下午一点", "地点": {"场合": "婚礼进行中"}, "林与陈": {"关系": "疏远"}},
    )
    result = await call(
        reg, ctx, "state_query", paths=[["时间"], ["地点", "场合"], ["林与陈", "关系"]]
    )
    assert "下午一点" in result.content
    assert "婚礼进行中" in result.content


async def test_staged_delta_is_not_persisted_until_run_ends(reg, ctx) -> None:
    """S20：run 内暂存。否则中途失败会造成"增量已落盘但正文不存在"。"""
    await call(reg, ctx, "state_write", set={"地点": "厨房"})
    assert ctx.repo.all_nodes() == {}
    assert ctx.current_state().values["地点"] == "厨房"
    assert ctx.base_state().values == {}


# -- 可恢复错误 --------------------------------------------------------------


async def test_unknown_tool_is_recoverable(reg, ctx) -> None:
    result = await reg.dispatch(ctx, ToolCall(id="c", name="nope", arguments={}))
    assert result.is_error
    assert "可用" in result.content


async def test_malformed_json_arguments_are_recoverable(reg, ctx) -> None:
    call_ = ToolCall.from_raw("c", "state_write", "{坏的")
    result = await reg.dispatch(ctx, call_)
    assert result.is_error
    assert "重新调用" in result.content


async def test_invalid_params_are_recoverable(reg, ctx) -> None:
    result = await call(reg, ctx, "ask_question", question="?", options=["只有一个"])
    assert result.is_error


async def test_unknown_state_path_returns_what_is_available(reg, ctx) -> None:
    """查不到就把现有结构回给模型，顺带起到防键名漂移的作用。"""
    await call(reg, ctx, "state_write", set={"林与陈": {"关系": "疏远"}})
    result = await call(reg, ctx, "state_query", paths=[["心情"]])
    assert result.is_error
    assert "林与陈 › 关系" in result.content


# -- 查询 -------------------------------------------------------------------


async def test_state_query_reads_staged_value(reg, ctx) -> None:
    await call(reg, ctx, "state_write", append={"林": {"知情": ["照片的存在"]}})
    result = await call(reg, ctx, "state_query", paths=[["林", "知情"]])
    assert "照片的存在" in result.content


async def test_querying_a_branch_gives_the_whole_subtree(reg, ctx) -> None:
    """路径可以停在中间层——**取一整棵子树**（S31）。

    展开层级把深处折成了「N 项」，模型看到的就是那一层的位置，
    所以它必须能照着那个位置直接要下去。
    """
    await call(reg, ctx, "state_write", set={"林": {"在场": True, "知情": ["照片"]}})
    result = await call(reg, ctx, "state_query", paths=[["林"]])
    assert "在场" in result.content and "照片" in result.content


# -- history / worldinfo ----------------------------------------------------


async def test_history_search_marks_hits_without_filtering(reg, ctx) -> None:
    """**命中只做标注，不筛掉任何一轮**（P13）。

    此前 `query` 是子串过滤器：查不中就返回空列表，而模型会把"空"读成
    "没这回事"——这正是 lorebook 关键词触发不合格的那条理由，我们自己却犯了。
    早前埋的伏笔常常换了个说法，逐字查不中是**常态**，不是例外。
    """
    a = Node(outline="她推开门，屋里没人。")
    b = Node(parent_id=a.id, outline="陈从阳台走进来。")
    ctx.repo.save_node(a)
    ctx.repo.save_node(b)
    ctx.parent_id = b.id

    index = await call(reg, ctx, "history_search", query="阳台")
    assert b.id in index.content
    assert a.id in index.content, "没命中的轮次仍必须出现在索引里"
    assert "命中" in index.content
    # 空结果的含义要写在结果里，不能靠模型自己领会
    assert "没命中不等于没发生过" in index.content

    body = await call(reg, ctx, "history_read", run_ids=[a.id])
    assert "屋里没人" in body.content


async def test_history_read_expands_the_whole_segment(reg, ctx) -> None:
    """**索引按段给 ref，展开也必须按段给回全文**（S57）。

    摘要在上下文里顶替的是**整段**——那几轮的细纲、当时查了什么，全被它替掉了。
    只还回末节点那一轮比不展开更糟：模型会以为自己已经看全了。
    """
    a = Node(outline="第一轮：她推开门。")
    b = Node(parent_id=a.id, outline="第二轮：陈从里屋出来。")
    c = Node(parent_id=b.id, outline="第三轮：他把门锁上。", outline_summary="她进店，他锁门。")
    c.segment_span = 3
    for node in (a, b, c):
        ctx.repo.save_node(node)
    ctx.parent_id = c.id

    body = await call(reg, ctx, "history_read", run_ids=[c.id])

    assert "她推开门" in body.content
    assert "陈从里屋出来" in body.content
    assert "他把门锁上" in body.content


async def test_history_read_rejects_unknown_ids(reg, ctx) -> None:
    result = await call(reg, ctx, "history_read", run_ids=["run_ghost"])
    assert result.is_error


# -- 截断必须出声 ------------------------------------------------------------
#
# **隐藏不是截断。** 长值换成"用 state_query 展开"是无损降维，展开的路还在；
# 这里几处是真的砍掉了尾巴，砍掉了就必须说清丢了多少、怎么拿回来——否则模型
# 拿到一份看起来完整的结果，而它不知道自己丢了什么（P13 同一条理由）。


async def test_an_index_entry_says_how_much_it_left_out(reg, ctx) -> None:
    """索引里的头部截断要报出全文字数——那是模型决定要不要 `history_read` 的依据。"""
    node = Node(outline="很长的一轮细纲。" * 60)
    assert "……（截断，全文共" in node.summary_or_head(120)
    assert str(len(node.outline)) in node.summary_or_head(120)

    short = Node(outline="很短。")
    assert short.summary_or_head(120) == "很短。", "没截断就不要加噪声"


async def test_history_read_says_which_refs_it_dropped(reg, ctx) -> None:
    """预算耗尽时**一个字都没返回的那几条**必须点名。

    以前这里直接 break，少返回的无声消失。这是三处真截断里最糟的一处——
    别的至少还留了个头，这个是整条不见。
    """
    ids = []
    parent = None
    for i in range(3):
        node = Node(parent_id=parent, outline=f"第{i}轮。" + "长" * (MAX_READ_CHARS // 2))
        ctx.repo.save_node(node)
        ids.append(node.id)
        parent = node.id
    ctx.parent_id = parent

    body = await call(reg, ctx, "history_read", run_ids=ids)

    assert "没给全" in body.content
    assert ids[2] in body.content, "没返回的那条 ref 要点名"
    assert body.payload["unserved"] == [ids[2]]


async def test_worldinfo_read_says_where_to_resume(reg, ctx) -> None:
    """分页是无损的，但**得说清下一个 start 是多少**——只给一个布尔量等于让模型猜。"""
    entry = WorldbookEntry(name="教团", content="设定内容。" * 100)
    ctx.repo.book().save_entry(entry)

    body = await call(reg, ctx, "worldinfo_read", refs=[entry.id], max_chars=50)

    assert "还没读完" in body.content
    assert "start=50" in body.content


async def test_worldinfo_index_covers_everything(reg, ctx) -> None:
    """索引必须覆盖**全部**条目，不做预筛选（P13）——筛选子集式的规则不合格，
    因为漏掉时模型根本不知道条目存在。
    """
    for i in range(3):
        ctx.repo.book().save_entry(
            WorldbookEntry(name=f"设定{i}", content="内容" * 50, mode=WorldbookMode.ON_DEMAND)
        )

    index = await call(reg, ctx, "worldinfo_list")
    assert index.content.count("ref") == 3
    assert "chars" in index.content


# -- summarize --------------------------------------------------------------


async def test_summarize_caches_and_reuses(reg, ctx) -> None:
    """懒惰生成 + 系统缓存（S25）：第二次调用不再打模型。"""
    entry = WorldbookEntry(name="教团", content="很长的设定" * 100)
    ctx.repo.book().save_entry(entry)

    first = await call(reg, ctx, "summarize", source=f"wi:{entry.id}")
    assert first.content == "摘要文本"
    assert ctx.repo.book().load_entry(entry.id).summary == "摘要文本"

    ctx.gateway = fake_gateway("不该被调用")
    second = await call(reg, ctx, "summarize", source=f"wi:{entry.id}")
    assert second.content == "摘要文本"
    assert second.label == "复用了已有摘要"


async def test_summary_falls_back_to_head_in_index(reg, ctx) -> None:
    """尚无摘要的条目回退到"前 N 字 + 字数"——仍满足 P13。"""
    ctx.repo.book().save_entry(
        WorldbookEntry(name="教团", content="甲乙丙丁" * 100, mode=WorldbookMode.ON_DEMAND)
    )
    index = await call(reg, ctx, "worldinfo_list")
    assert "…" in index.content


async def test_summarize_rejects_bad_source(reg, ctx) -> None:
    result = await call(reg, ctx, "summarize", source="乱写")
    assert result.is_error


# -- ask_question -----------------------------------------------------------


_THREE = ["否认", "沉默", "反问"]


async def test_ask_question_awaits_user(reg, ctx) -> None:
    asked: list[tuple[str, list[str]]] = []

    async def ask(question: str, options: list[str]) -> str:
        asked.append((question, options))
        return options[1]

    ctx.ask_user = ask
    result = await call(reg, ctx, "ask_question", question="她怎么反应？", options=_THREE)

    assert asked == [("她怎么反应？", _THREE)]
    assert "沉默" in result.content
    assert result.payload["free_form"] is False


async def test_a_question_takes_exactly_three_options(reg, ctx) -> None:
    """**恰好三个**（S23）。两个把岔路压成二选一，四个开始出现凑数的那条。

    不留「其他」位是因为用户随时可以自己写一条——实测模型自己造过这么一个选项
    （「其他：你另指定这一轮实际要用的手段」），那是在没有一等自由作答入口时的
    将就，白白花掉三分之一的选项位。
    """
    ctx.ask_user = _pick_first
    for options in (["甲", "乙"], ["甲", "乙", "丙", "丁"]):
        result = await call(reg, ctx, "ask_question", question="?", options=options)
        assert result.is_error, f"{len(options)} 个选项该被退回"


async def test_an_answer_outside_the_options_reads_differently(reg, ctx) -> None:
    """三选一和自由作答**对后续推演的分量不同**，回执必须分得清（S23）。

    合成一句「用户选择：…」会把后者说成前者，于是模型照着"这是我列过的选项"
    去理解一句它其实没列过的话。
    """

    async def ask(question: str, options: list[str]) -> str:
        return "都不选：她把杯子推过来，不接话。"

    ctx.ask_user = ask
    result = await call(reg, ctx, "ask_question", question="她怎么反应？", options=_THREE)

    assert result.payload["free_form"] is True
    assert "没有选给出的选项" in result.content
    assert "杯子" in result.content


async def _pick_first(question: str, options: list[str]) -> str:
    return options[0] if options else ""


# -- 工具面 -----------------------------------------------------------------


async def test_history_never_leaks_prose(reg, ctx) -> None:
    """S5 的基础：director 从不读 run 的正文。它能覆盖五倍历史跨度正是因为这一点，
    这条一破，整个隔离设计作废。

    行为检查而非字符串检查：history 的两个工具都必须只吐细纲。
    """
    node = Node(outline="细纲：她推开门。", prose="正文：门轴发出一声长长的呻吟，她站在门口。")
    ctx.repo.save_node(node)
    ctx.parent_id = node.id

    index = await call(reg, ctx, "history_search")
    body = await call(reg, ctx, "history_read", run_ids=[node.id])

    for result in (index, body):
        assert "门轴" not in result.content
        assert "门轴" not in str(result.payload)

    assert "细纲" in body.content
    assert "text.quote" not in reg.names


async def test_specs_are_valid_json_schema(reg) -> None:
    for spec in reg.specs():
        assert spec.parameters["type"] == "object"
        assert spec.to_wire()["type"] == "function"


def test_the_criteria_are_not_in_the_tool_description() -> None:
    """判据归 **state_manager 的提示词**；工具描述只说「作用 + 参数格式」（S27）。

    它以前整整两千字堆在 `state_write` 的描述里。搬走的理由是**提示词正文归用户**：
    YAML 那一份用户随时能改，工具描述里那一份改不到——两份并存时，用户改完以为
    生效了，实际另一份还在原地顶着。

    ## 这条测试只钉「那边没有第二份副本」

    它以前还逐句钉了提示词里写着什么（"约束类""悬置类""兑现了"……）。
    **那些断言已经删掉**：提示词正文是用户的东西，用户改一次措辞就红一次，
    而红的不是缺陷——是他行使了那份所有权。

    代价说在明处：S34/S35 的判据现在**没有任何测试守着**，已记进 TODO。
    """
    tool_desc = next(t.description for t in build_state_tools() if t.name == "state_write")
    assert len(tool_desc) < 60
    assert "约束类" not in tool_desc
    assert "悬置" not in tool_desc


def test_state_entries_become_the_replay_base(tmp_path: Path) -> None:
    """条目分两类（S67）：静态的进 worldinfo，会变的作为**初始状态**进 state。

    这样模型不必把设定抄进 state——那种副本会在设定被改动后与之分叉。
    诱因消失了，比在提示词里禁止可靠。
    """
    repo = Store(tmp_path).create_session(Session())
    repo.book().save_entry(WorldbookEntry(name="林昭", content="记者，做事谨慎。"))
    repo.book().save_entry(
        WorldbookEntry(
            name="林昭·初始状态",
            kind=EntryKind.STATE,
            values={"林昭": {"在场": False, "知情": ["匿名信里的照片"]}},
        )
    )

    # 初始状态是一切分支共同的起点，空路径也能读到。
    assert repo.state_at(None).values["林昭"]["在场"] is False

    node = Node(state_delta=StateDelta(set={"林昭": {"在场": True}}))
    repo.save_node(node)
    after = repo.state_at(node.id).values
    assert after["林昭"]["在场"] is True
    # **深合并**：没提到的原样留着（S31）。
    assert after["林昭"]["知情"] == ["匿名信里的照片"]


def test_the_initial_state_freezes_once_the_story_starts(tmp_path: Path) -> None:
    """**开始之后改初始状态，不再回头改写既有的每一轮**（S67）。

    早先它一直是现算的，说法是"同改角色卡"。那个类比不成立：改角色卡改的是设定，
    而这里改的是**已经发生过的剧情所依赖的事实**——某一轮写着"她掏出钥匙"，
    而钥匙是初始状态给的；把它删掉，那一轮就凭空多了一把不存在的钥匙。
    """
    repo = Store(tmp_path).create_session(Session())

    def put(values: dict[str, object]) -> None:
        # **同一个 id** ——不固定的话每次都是新增一条，而不是改那一条。
        repo.book().save_entry(
            WorldbookEntry(id="wi_start", name="起点", kind=EntryKind.STATE, values=values)
        )

    put({"钥匙": "在她口袋里"})

    # 还没开始：改了立刻生效——故事没有过去，没有东西依赖它。
    assert repo.state_at(None).values == {"钥匙": "在她口袋里"}
    put({"钥匙": "在桌上"})
    assert repo.state_at(None).values == {"钥匙": "在桌上"}

    repo.freeze_initial_state()

    # 开始之后：改了只影响以后新建的会话。
    put({})
    assert repo.state_at(None).values == {"钥匙": "在桌上"}
    assert repo.compose_initial_state().values == {}, "模板本身确实变了"


def test_tool_names_match_the_api_pattern() -> None:
    """OpenAI 兼容端点要求工具名匹配 `^[a-zA-Z0-9_-]+$`。

    **点号会被 400 拒掉。** 最初用的是 `state.query` 这类命名，第一次真实调用
    就撞上了——而且是整个请求被拒，不是某个工具不可用。
    """
    pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
    with using(**ALL_ON):
        every = build_tools([WorldbookEntry(name="x", mode=WorldbookMode.ON_DEMAND)])
    for tool_ in every:
        assert pattern.match(tool_.name), tool_.name


# -- 能力开关：关闭即**不注册** -----------------------------------------------


def names_for(worldinfo: list[WorldbookEntry] | None = None, **setup: object) -> set[str]:
    """当前配置下 director 拿得到哪几个工具。**开关来自 YAML**（S80）。"""
    with using(**setup):  # type: ignore[arg-type]
        return set(ToolRegistry.for_session(worldinfo).names)


def test_submit_outline_always_exists() -> None:
    """它是 loop 的唯一终止点（S4），不可关闭。"""
    assert "submit_outline" in names_for(
        agents={"state_manager": False},
        tools={"director": {"ask_question": False, "history_search": False}},
    )


def test_each_switch_removes_its_tools() -> None:
    """**不注册**比「注册了但拒绝」强：模型看不到，不会浪费尝试，也不占 prompt 体量。"""
    assert "state_query" in names_for(agents={"state_manager": True})
    assert names_for(agents={"state_manager": False}).isdisjoint({"state_query", "state_write"})

    assert names_for(tools={"director": {"history_search": True}}) >= {
        "history_search",
        "history_read",
    }
    assert names_for(tools={"director": {"history_search": False}}).isdisjoint(
        {"history_search", "history_read"}
    )

    assert "summarize" in names_for(tools={"director": {"summarize": True}})
    assert "summarize" not in names_for(tools={"director": {"summarize": False}})

    assert "ask_question" in names_for(tools={"director": {"ask_question": True}})
    assert "ask_question" not in names_for(tools={"director": {"ask_question": False}})


def test_worldinfo_tools_only_when_something_is_expandable() -> None:
    """全部条目都常驻注入时不注册——没有东西可展开，留着只是白占 prompt 体量。"""
    always = [WorldbookEntry(name="甲", mode=WorldbookMode.ALWAYS)]
    mixed = [*always, WorldbookEntry(name="乙", mode=WorldbookMode.ON_DEMAND)]

    assert names_for([]).isdisjoint({"worldinfo_list", "worldinfo_read"})
    assert names_for(always).isdisjoint({"worldinfo_list", "worldinfo_read"})
    assert names_for(mixed) >= {"worldinfo_list", "worldinfo_read"}


def test_the_write_side_belongs_to_the_state_manager_alone(tmp_path: Path) -> None:
    """**director 读状态，state_manager 写状态**（S7）。

    这条隔离和 S3 那把刀是同一个性质：谁能碰什么，从工具面上就该看得出来。
    `state_query` 两边都有——写之前要知道现在是什么样。
    """
    assert "state_write" not in names_for(**ALL_ON)
    assert {t.name for t in build_state_tools()} == {"state_write", "state_query"}

    # 按需展开关掉时 state 全量注入，`state_query` 两边一起消失（P13）。
    # **两侧各有各的开关**（R31）：同一个工具在两个 agent 手上是两条
    # 描述、两个开关，所以要关就两边都关。
    off = {"tools": {"director": {"state_query": False}, "state_manager": {"state_query": False}}}
    assert "state_query" not in names_for(**off)  # type: ignore[arg-type]
    with using(**off):  # type: ignore[arg-type]
        assert {t.name for t in build_state_tools()} == {"state_write"}


async def test_submit_outline_never_has_a_precondition(tmp_path: Path) -> None:
    """E0 作废之后，state 开着关着都一样能直接提交。"""
    for on in (True, False):
        repo = Store(tmp_path / str(on)).create_session(Session())
        ctx = RunContext("run_x", repo, repo.load_session(), fake_gateway())

        with using(agents={"state_manager": on}):
            result = await call(
                ToolRegistry.for_session(), ctx, "submit_outline", outline="她推门。"
            )
        assert not result.is_error
        assert ctx.outline == "她推门。"


def test_base_state_uses_parent_path(tmp_path: Path) -> None:
    """状态是路径的函数（S42）：run 的基线来自它挂载的父节点，不是全局。"""
    repo = Store(tmp_path).create_session(Session())
    root = Node(state_delta=StateDelta(set={"地点": "门口"}))
    left = Node(parent_id=root.id, state_delta=StateDelta(set={"地点": "客厅"}))
    repo.save_node(root)
    repo.save_node(left)

    on_root = RunContext("r", repo, repo.load_session(), fake_gateway(), parent_id=root.id)
    on_left = RunContext("r", repo, repo.load_session(), fake_gateway(), parent_id=left.id)

    assert on_root.base_state().values["地点"] == "门口"
    assert on_left.base_state().values["地点"] == "客厅"
