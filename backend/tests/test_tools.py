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
    ToolRegistry,
    build_state_tools,
    build_tools,
    goal_tools,
)


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
    return LLMGateway(Settings(), http_client=client)


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


ALL_ON = {"tools": {"director": {"history_read": True}}}
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

    wrapped = await call(
        reg,
        ctx,
        "state_write",
        arguments={"set": {"地点": "客厅"}},
    )
    assert wrapped.is_error, "多套一层 arguments 不能被静默当成空增量"
    assert ctx.state_write_count == 0


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


# -- 三个域，一份实现（S157）----------------------------------------------
#
# 折叠时留下的占位符和展开工具接受的地址**必须是同一套 key**，所以三个工具
# 走同一个 `_expand`：取、报缺、拼回执都是一份代码。这一节钉的是三件事——
# 各自的树摊成什么样、地址是什么、取不到时说什么。


async def test_all_three_expand_the_same_way(reg, ctx) -> None:
    """**同一套参数形状、同一套回执**（S157）：路径数组进去，JSON 出来。"""
    await call(reg, ctx, "state_write", set={"林": {"在场": True}})
    ctx.repo.book().save_entry(
        WorldbookEntry(name="教团", content="总部在旧城区。", mode=WorldbookMode.ON_DEMAND)
    )
    node = Node(prose="门轴响了。", prose_summary="她进了店。")
    ctx.repo.save_node(node)
    ctx.parent_id = node.id

    got = [
        await call(reg, ctx, "state_query", paths=[["林"]]),
        await call(reg, ctx, "worldinfo_read", paths=[["教团"]]),
        await call(reg, ctx, "history_read", paths=[[node.id]]),
    ]
    assert "在场" in got[0].content
    assert "旧货" not in got[1].content and "总部在旧城区" in got[1].content
    assert "门轴响了" in got[2].content
    for result in got:
        assert not result.is_error
        assert set(result.payload) == {"found", "missing"}


async def test_worldinfo_is_addressed_by_name(reg, ctx) -> None:
    """**地址是条目名，不是 id**（S157「有名字的用名字」）。

    上下文里那一行写的就是名字，模型照着写回来即可——中间不需要一次翻译，
    也不需要先花一轮把名字换成 id。
    """
    entry = ctx.repo.book().save_entry(
        WorldbookEntry(name="教团", content="总部在旧城区。", mode=WorldbookMode.ON_DEMAND)
    )
    body = await call(reg, ctx, "worldinfo_read", paths=[["教团"]])
    assert "总部在旧城区" in body.content

    miss = await call(reg, ctx, "worldinfo_read", paths=[[entry.id]])
    assert miss.is_error, "id 不再是地址"
    assert "教团" in miss.content, "取不到时要把可用的 key 回给它"


async def test_only_expandable_worldinfo_is_on_the_tree(reg, ctx) -> None:
    """**常驻条目不在展开树上**：它的正文已经在上下文里了。

    放进来的话，模型会去展开一段它眼前就有的东西——白花一轮往返。
    """
    ctx.repo.book().save_entry(WorldbookEntry(name="常驻的", content="每轮都在。"))
    ctx.repo.book().save_entry(
        WorldbookEntry(name="按需的", content="要时再看。", mode=WorldbookMode.ON_DEMAND)
    )
    result = await call(reg, ctx, "worldinfo_read", paths=[["常驻的"]])
    assert result.is_error
    assert "按需的" in result.content


async def test_history_read_expands_the_whole_segment(reg, ctx) -> None:
    """**索引按段折叠，展开也必须按段给回全文**（S57）。

    摘要在上下文里顶替的是**整段**——那几轮的正文全被它替掉了。
    只还回末节点那一轮比不展开更糟：模型会以为自己已经看全了。
    """
    a = Node(prose="第一轮：她推开门。")
    b = Node(parent_id=a.id, prose="第二轮：陈从里屋出来。")
    c = Node(parent_id=b.id, prose="第三轮：他把门锁上。", prose_summary="她进店，他锁门。")
    c.segment_span = 3
    for node in (a, b, c):
        ctx.repo.save_node(node)
    ctx.parent_id = c.id

    body = await call(reg, ctx, "history_read", paths=[[c.id]])

    assert "她推开门" in body.content
    assert "陈从里屋出来" in body.content
    assert "他把门锁上" in body.content


async def test_only_folded_history_is_on_the_tree(reg, ctx) -> None:
    """**没归过段的轮次不在展开树上**：它们的正文原文还在上下文里。

    段落是三个域里唯一用 id 当地址的——它没有名字，只有一段摘要，
    而摘要当不了地址（太长、会重、传回来会被改写一两个字）。
    """
    open_run = Node(prose="还没归段的一轮。")
    folded = Node(parent_id=open_run.id, prose="归过段的一轮。", prose_summary="摘要。")
    for node in (open_run, folded):
        ctx.repo.save_node(node)
    ctx.parent_id = folded.id

    result = await call(reg, ctx, "history_read", paths=[[open_run.id]])
    assert result.is_error
    assert folded.id in result.content


async def test_a_miss_hands_back_the_keys_that_do_exist(reg, ctx) -> None:
    """**一条都没命中 → 把可用的 key 整个回给模型**（S28）。

    两件事：让它下一轮改对，以及顺带压键名漂移——它看见"心情"已经存在，
    就不会再造一个"情绪"。
    """
    await call(reg, ctx, "state_write", set={"林与陈": {"关系": "疏远"}})
    result = await call(reg, ctx, "state_query", paths=[["心情"]])
    assert result.is_error
    assert "林与陈 › 关系" in result.content


async def test_a_partial_miss_still_returns_what_it_found(reg, ctx) -> None:
    """**部分命中照样给**：为了几条不存在的地址把整次调用判成失败，
    等于让模型多花一轮重来。
    """
    await call(reg, ctx, "state_write", set={"地点": "客厅"})
    result = await call(reg, ctx, "state_query", paths=[["地点"], ["没有这个"]])
    assert not result.is_error
    assert "客厅" in result.content
    assert "不存在的位置" in result.content
    assert result.payload["missing"] == ["没有这个"]


async def test_expanding_gives_everything_no_budget_no_paging(reg, ctx) -> None:
    """**展开就是全给**（S26/S157）：不分页、不按体量截断、没有「没给全」。

    这里以前每个工具各有一套预算（`tool_read_chars`）和分页（`start` /
    `max_chars`）——那是在这一层猜"多少算多"。真的超了上下文是**整轮失败**，
    不是这一层能补救的事，而一个猜出来的预算会让模型在不知情的情况下少看几段。
    """
    long_text = "很长的设定。" * 5000
    ctx.repo.book().save_entry(
        WorldbookEntry(name="长的", content=long_text, mode=WorldbookMode.ON_DEMAND)
    )
    body = await call(reg, ctx, "worldinfo_read", paths=[["长的"]])

    assert long_text in body.content
    assert "没给全" not in body.content and "截断" not in body.content


# -- 截断必须出声 ------------------------------------------------------------
#
# **隐藏不是截断。** 长值换成"用 state_query 展开"是无损降维，展开的路还在。
# 真截断只剩一处了：前情索引里没归过段的那几轮给的是"正文前 N 字"，
# 那是真的砍掉了尾巴，砍掉了就得说清丢了多少（P13 同一条理由）。


async def test_an_index_entry_says_how_much_it_left_out(reg, ctx) -> None:
    """索引里的头部截断要报出全文字数——那是模型决定要不要 `history_read` 的依据。"""
    node = Node(prose="很长的一轮正文。" * 60)
    assert "……（截断，全文共" in node.summary_or_head(120)
    assert str(len(node.prose)) in node.summary_or_head(120)

    short = Node(prose="很短。")
    assert short.summary_or_head(120) == "很短。", "没截断就不要加噪声"


# -- close_segment ----------------------------------------------------------


def test_close_segment_is_in_no_tool_face() -> None:
    """**它不属于任何一个 agent 的工具面**（S22）。

    归段是生成后的动作：它对本轮产出毫无帮助，却要占往返、分散注意力，
    它只出现在生成后追问那一次独立调用里（`agents/postrun.py`）。

    `summarize` 曾经在 director 的工具面里（S25）。**它整个删掉了**——
    压缩由编排层在轮次滑出全文窗口时自己做，模型不该有一个主动触发压缩的
    入口：判定和压缩本来就不同步，给它这个工具等于请它去动一件它管不着、
    也看不见后果的事。
    """
    faces = [
        {t.name for t in build_tools()},
        {t.name for t in build_state_tools()},
        {t.name for t in goal_tools()},
    ]
    for face in faces:
        assert "close_segment" not in face
        assert "summarize" not in face


def test_the_worldinfo_index_is_name_and_size(reg, ctx) -> None:
    """设定索引给的是**名字 + 体量**，没有别的（S53/S157）。

    它以前还给一段"正文开头"，那是 `summary` 字段没生成时的回退，顶着「摘要」
    的名字——而它其实是位置偏置最重的一种预览：开头原样保留、结尾完全消失。
    `summary` 字段随 `summarize` 工具一起删了，这个回退也就没有对象了。

    **索引不是工具**：它每轮都在上下文里（`render_worldinfo`），
    再给一个"列出索引"的工具等于让模型花一轮去要一份它眼前就有的东西。
    """
    from agenttavern.prompting.prompts import render_worldinfo

    index = render_worldinfo(
        [WorldbookEntry(name="教团", content="甲乙丙丁" * 100, mode=WorldbookMode.ON_DEMAND)]
    )
    assert "教团" in index
    assert "400 字" in index
    assert "甲乙丙丁" not in index, "折起来就是折起来，不留开头"


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


async def test_history_expands_authoritative_prose(reg, ctx) -> None:
    """展开前情只返回落成的正文，不存在临时细纲的第二份事实。"""
    node = Node(prose="正文：门轴发出一声长长的呻吟，她站在门口。")
    ctx.repo.save_node(node)
    ctx.parent_id = node.id

    node.prose_summary = "她进了店。"
    node.segment_span = 1
    ctx.repo.save_node(node)
    body = await call(reg, ctx, "history_read", paths=[[node.id]])

    assert "门轴" in body.content
    assert body.payload["found"] == [node.id]
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
    assert len(tool_desc) < 120
    assert "约束类" not in tool_desc
    assert "悬置" not in tool_desc


def test_state_write_parameters_are_direct_not_wrapped() -> None:
    """工具描述要把参数外形说清，错误外壳则由 schema 拒绝并回给模型（S27/S28）。"""
    tool = next(t for t in build_state_tools() if t.name == "state_write")
    assert all(name in tool.description for name in ("set", "append", "remove", "arguments"))

    schema = tool.spec().parameters
    assert schema.get("additionalProperties") is False


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


def test_director_has_no_submit_tool() -> None:
    """director 以非空最终文本收口，不注册专用提交工具（S174）。"""
    assert "submit_outline" not in names_for(
        agents={"state_manager": False},
        tools={"director": {"ask_question": False, "history_read": False}},
    )


def test_each_switch_removes_its_tools() -> None:
    """**不注册**比「注册了但拒绝」强：模型看不到，不会浪费尝试，也不占 prompt 体量。"""
    assert "state_query" in names_for(agents={"state_manager": True})
    assert names_for(agents={"state_manager": False}).isdisjoint({"state_query", "state_write"})

    assert "history_read" in names_for(tools={"director": {"history_read": True}})
    assert "history_read" not in names_for(tools={"director": {"history_read": False}})

    assert "ask_question" in names_for(tools={"director": {"ask_question": True}})
    assert "ask_question" not in names_for(tools={"director": {"ask_question": False}})


def test_state_query_is_hidden_when_there_is_no_state_to_expand() -> None:
    """空状态没有查询对象：工具不注册，避免第一轮模型白调一次（S21）。"""
    with using(agents={"state_manager": True}):
        assert "state_query" not in ToolRegistry.for_session(has_state=False).names
        assert {t.name for t in build_state_tools(has_state=False)} == {"state_write"}


def test_worldinfo_tools_only_when_something_is_expandable() -> None:
    """全部条目都常驻注入时不注册——没有东西可展开，留着只是白占 prompt 体量。"""
    always = [WorldbookEntry(name="甲", mode=WorldbookMode.ALWAYS)]
    mixed = [*always, WorldbookEntry(name="乙", mode=WorldbookMode.ON_DEMAND)]

    assert "worldinfo_read" not in names_for([])
    assert "worldinfo_read" not in names_for(always)
    assert "worldinfo_read" in names_for(mixed)


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
