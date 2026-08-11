"""分支与打断。

两件事合在一起测，因为它们问的是**同一个问题**：一条路径上该看见什么。

- **分支**：两支从同一个节点岔出，谁都不该看见对方（D48/D114）。
- **打断**：被截断的推演该被下一条输入接下去，而不是丢掉（D118）。

分支是**纯代码层面的正确性**，不需要真实模型——要验的是"上下文里有没有别支的
东西"，而这是三个确定的数据通道：`user_input`、`state`、重放出来的 `history`。
拿真实模型跑只是把确定的事变成概率的事，还贵。
"""

import asyncio
from pathlib import Path

import pytest
from conftest import CancelWriter, ScriptedLLM, text, tool, tools

from agenttavern.llm import Message, Role, ToolCall
from agenttavern.models import RunStage, Session, UserInput, trim_dangling_tool_call
from agenttavern.orchestration import execute_run
from agenttavern.storage import Store
from agenttavern.tools import Inbox, ToolRegistry


def _turn(outline: str, prose: str) -> list[dict]:
    """一轮的两次模型调用。状态那一次不在这里——它跑在 writer 之后（D143），
    由 `ScriptedLLM(state=...)` 应答。
    """
    return [tool("submit_outline", outline=outline), text(prose)]


def _state(key: str, value: str) -> dict:
    return {"set": {key: value}}


def _setup(tmp_path: Path, llm: ScriptedLLM):
    repo = Store(tmp_path).create_session(Session())
    return repo, repo.load_session(), llm.gateway(), ToolRegistry()


# -- 分支：三个通道分别查 -------------------------------------------------------


async def test_a_sibling_branch_leaks_into_neither_input_nor_history(tmp_path: Path) -> None:
    """B 支的 director 上下文里不能有 A 支的**用户输入**，也不能有 A 支的**细纲**。

    两样分开断言，因为它们走的是不同的通道：用户输入进的是每轮的 user 消息，
    细纲进的是 `submit_outline` 的调用参数。**一个坏了另一个可能还是好的。**
    """
    trunk_llm = ScriptedLLM(*_turn("主干：她推开门。", "主干正文"), state=_state("地点", "钟表铺"))
    repo, session, gw, reg = _setup(tmp_path, trunk_llm)
    trunk = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    a_llm = ScriptedLLM(*_turn("A 支：铁皮盒放上柜台。", "A 支正文"), state=_state("A物", "铁皮盒"))
    await execute_run(
        repo,
        repo.load_session(),
        a_llm.gateway(),
        reg,
        user_input=UserInput(text="有人送来一只铁皮盒"),
        parent_id=trunk.node.id,
    )

    # B 支从**同一个**分岔点岔出——此刻 active_leaf_id 指着 A 的叶子，
    # 父节点若从活跃叶取，B 就直接长在 A 后面了（D103）。
    b_llm = ScriptedLLM(*_turn("B 支：便签放上柜台。", "B 支正文"), state=_state("B物", "便签"))
    branch_b = await execute_run(
        repo,
        repo.load_session(),
        b_llm.gateway(),
        reg,
        user_input=UserInput(text="我把便签放在柜台上"),
        parent_id=trunk.node.id,
    )

    assert branch_b.node.parent_id == trunk.node.id

    context = str(b_llm.messages_of(0))
    assert "主干：她推开门。" in context, "主干必须还在——隔离过头和污染同样是错"
    assert "我推开门" in context
    assert "有人送来一只铁皮盒" not in context, "A 支的用户输入串过来了"
    assert "铁皮盒" not in context, "A 支的细纲串过来了"
    assert "A 支正文" not in context, "director 本来就不该看见任何正文（D54）"


async def test_a_sibling_branch_leaks_no_tool_traffic(tmp_path: Path) -> None:
    """连**工具调用与结果**都不能串。

    D114 之后这条才成立得起来：持续对话把工具往返也留在上下文里，所以它多了一条
    以前不存在的泄漏通道。查细纲查不到它——工具结果不出现在细纲里。
    """
    trunk_llm = ScriptedLLM(*_turn("主干：她推开门。", "主干正文"), state=_state("地点", "钟表铺"))
    repo, session, gw, reg = _setup(tmp_path, trunk_llm)
    trunk = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    a_llm = ScriptedLLM(
        tools(("state_query", {"keys": ["地点"]}), ("state_write", {"set": {"A物": "铁皮盒"}})),
        tool("submit_outline", outline="A 支的细纲。"),
        text("A 支正文"),
    )
    await execute_run(
        repo,
        repo.load_session(),
        a_llm.gateway(),
        reg,
        user_input=UserInput(text="A 支输入"),
        parent_id=trunk.node.id,
    )

    b_llm = ScriptedLLM(*_turn("B 支的细纲。", "B 支正文"), state=_state("B物", "便签"))
    await execute_run(
        repo,
        repo.load_session(),
        b_llm.gateway(),
        reg,
        user_input=UserInput(text="B 支输入"),
        parent_id=trunk.node.id,
    )

    context = str(b_llm.messages_of(0))
    # **查的是那次调用留下的痕迹，不是工具名**——工具面的索引每轮都在，
    # 它列出所有注册了的工具（D140），名字出现在上下文里是正常的。
    assert "A物" not in context
    assert "铁皮盒" not in context
    assert "state_query(" not in context


async def test_state_of_a_sibling_never_folds_in(tmp_path: Path) -> None:
    """状态是**路径**的函数，不是 session 的函数（D48）。

    单独测一遍是因为它坏起来最隐蔽：文本可以完全正确，状态悄悄错，
    要等很多轮之后才以"角色行为不合理"的形式暴露。
    """
    trunk_llm = ScriptedLLM(*_turn("主干。", "主干正文"), state=_state("灵石", "3"))
    repo, session, gw, reg = _setup(tmp_path, trunk_llm)
    trunk = await execute_run(repo, session, gw, reg, user_input=UserInput(text="起"))

    a_llm = ScriptedLLM(*_turn("A 支花光了。", "A 正文"), state=_state("灵石", "0"))
    arm_a = await execute_run(
        repo,
        repo.load_session(),
        a_llm.gateway(),
        reg,
        user_input=UserInput(text="全买了"),
        parent_id=trunk.node.id,
    )

    b_llm = ScriptedLLM(*_turn("B 支赚到了。", "B 正文"), state=_state("灵石", "5"))
    arm_b = await execute_run(
        repo,
        repo.load_session(),
        b_llm.gateway(),
        reg,
        user_input=UserInput(text="接了活"),
        parent_id=trunk.node.id,
    )

    assert repo.state_at(arm_a.node.id).values["灵石"] == "0"
    assert repo.state_at(arm_b.node.id).values["灵石"] == "5"
    assert repo.state_at(trunk.node.id).values["灵石"] == "3"


# -- 打断：挂起，不作废（D118） -------------------------------------------------


class _CancelAfter(ScriptedLLM):
    """跑满 ``after`` 次调用就掐掉——模拟"打断落在等 API 回复的那一刻"。"""

    def __init__(self, *replies, after: int, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*replies, **kwargs)
        self._after = after
        self._calls = 0

    def _handle(self, request):  # type: ignore[no-untyped-def]
        self._calls += 1
        if self._calls > self._after:
            raise asyncio.CancelledError
        return super()._handle(request)


async def test_an_interrupted_deduction_is_suspended_not_discarded(tmp_path: Path) -> None:
    """打断后下一条输入**接在已完成的部分后面**继续，不从头重来。

    这是打断与重生成的分界：重生成是"换一个结果，已完成的不要了"；打断只是
    **截断了时机**，已经查过的、已经暂存的都还算数。
    """
    llm = _CancelAfter(
        tool("history_search", query="门"),
        tool("state_query", keys=["地点"]),
        after=2,
    )
    repo, session, gw, reg = _setup(tmp_path, llm)

    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    assert repo.all_nodes() == {}, "细纲没成型，不该落节点"

    draft = repo.load_draft()
    assert draft is not None
    assert draft.parent_id is None
    # **草稿里剩下的就是查过的东西**。它以前还存 `staged_delta` / `state_write_count`，
    # 那时 director 边推演边写状态；D143 把状态维护整个搬到 writer 之后，
    # 这一阶段的暂存区恒为空，两个字段一并删了。
    assert any("state_query" in str(m.tool_calls) for m in draft.messages if m.tool_calls)

    resume_llm = ScriptedLLM(
        tool("submit_outline", outline="她站在门口。"),
        text("门口的光把她的影子拉长。"),
        state=_state("门", "开着"),
    )
    outcome = await execute_run(
        repo,
        repo.load_session(),
        resume_llm.gateway(),
        reg,
        user_input=UserInput(text="等等，让她先别进去"),
    )

    context = str(resume_llm.messages_of(0))
    assert "我推开门" in context, "被打断那一轮的输入必须还在"
    assert "state_query" in context, "已经查过的资料不该白查一遍"
    assert "等等，让她先别进去" in context

    # 一次打断 + 一次续跑 = **一个节点**，不是两个。
    assert len(repo.all_nodes()) == 1
    assert outcome.node.state_delta.set["门"] == "开着"
    assert repo.load_draft() is None, "提交后草稿必须清掉"


async def test_a_dangling_tool_call_is_trimmed_before_resuming(tmp_path: Path) -> None:
    """打断落在工具调用和它的结果之间时，那条调用整条丢掉。

    **留着它，续跑时整份请求会被端点拒绝**——`tool_calls` 没有对应的 tool 消息
    是协议错误。于是"打断过一次的会话再也跑不起来"，而报错发生在下一轮，
    看上去和打断毫无关系。
    """

    llm = _CancelAfter(tool("state_query", keys=["地点"]), after=1)
    repo, session, gw, reg = _setup(tmp_path, llm)
    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    draft = repo.load_draft()
    assert draft is not None
    # 第一次调用返回了一个工具调用，它的结果已经拿到了（dispatch 是同步完成的），
    # 掐在第二次 complete 上——所以这份草稿本身是配平的。
    calls = [m for m in draft.messages if m.tool_calls]
    answers = {m.tool_call_id for m in draft.messages if m.tool_call_id}
    for message in calls:
        for call in message.tool_calls:
            assert call.id in answers, "草稿里不能留下没有结果的工具调用"


def test_the_trimmer_drops_a_call_whose_result_never_arrived() -> None:
    """直接测截断本身。

    上面那条集成测试证明的是"草稿恰好是配平的"——**打断落在真正危险的那一刻
    很难在测试里复现**（脚本化的 dispatch 是同步完成的）。所以危险的那一刻
    要在这里手工构造出来，否则截断逻辑等于没被测过。
    """
    call = ToolCall(id="c1", name="state_query")
    messages = [
        Message(role=Role.USER, content="本轮输入"),
        Message(role=Role.ASSISTANT, tool_calls=[call]),
    ]
    assert trim_dangling_tool_call(messages) == messages[:1]

    answered = [*messages, Message(role=Role.TOOL, content="结果", tool_call_id="c1")]
    assert trim_dangling_tool_call(answered) == answered

    # 一条消息里发起两个调用而只回了一个：整条一起丢——半份工具结果对应不上任何
    # 调用，比没有更糟。
    two = ToolCall(id="c2", name="worldinfo_read")
    partial = [
        Message(role=Role.USER, content="本轮输入"),
        Message(role=Role.ASSISTANT, tool_calls=[call, two]),
        Message(role=Role.TOOL, content="结果", tool_call_id="c1"),
    ]
    assert trim_dangling_tool_call(partial) == partial[:1]


async def test_a_draft_from_another_branch_is_dropped(tmp_path: Path) -> None:
    """草稿按父节点认领。用户切了分支，这份推演就接不上了（D48）。"""
    first_llm = ScriptedLLM(*_turn("第一轮。", "第一轮正文"), state=_state("地点", "钟表铺"))
    repo, session, gw, reg = _setup(tmp_path, first_llm)
    first = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    aborted = _CancelAfter(tool("state_write", set={"作废": "另一条分支上的推演"}), after=1)
    with pytest.raises(asyncio.CancelledError):
        await execute_run(
            repo,
            repo.load_session(),
            aborted.gateway(),
            reg,
            user_input=UserInput(text="她走向里屋"),
            parent_id=first.node.id,
        )
    assert repo.load_draft() is not None

    # 换一个父节点（回到根重开一支）——草稿必须被丢掉。
    other_llm = ScriptedLLM(*_turn("另一支。", "另一支正文"), state=_state("地点", "街上"))
    other = await execute_run(
        repo,
        repo.load_session(),
        other_llm.gateway(),
        reg,
        user_input=UserInput(text="她根本没进门"),
        parent_id=None,
    )

    context = str(other_llm.messages_of(0))
    assert "作废" not in context
    assert "她走向里屋" not in context
    assert "作废" not in repo.state_at(other.node.id).values
    assert repo.load_draft() is None


async def test_a_half_written_turn_becomes_a_draft_not_a_node(tmp_path: Path) -> None:
    """writer 阶段被打断：**细纲存进草稿，节点不落**（D142，推翻 D60 的后半句）。

    这条以前测的是反面（保留正文片段、落一个 PARTIAL 节点）。改掉的理由是
    半截正文不是产出而是一个失败的阶段：下一轮会接着那半句往下写，而用户按
    停止恰恰是因为不想要它。
    """
    llm = CancelWriter(tool("submit_outline", outline="她话说到一半，停住了。"))
    repo, session, gw, reg = _setup(tmp_path, llm)

    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我走过去"))

    assert repo.all_nodes() == {}
    draft = repo.load_draft()
    assert draft is not None and draft.stage is RunStage.WRITER

    # 空输入：只重跑 writer，细纲原样用，仍然是**同一个节点**。
    next_llm = ScriptedLLM(text("她把话说完了。"), state=_state("地点", "里屋"))
    following = await execute_run(repo, repo.load_session(), next_llm.gateway(), reg)

    assert len(repo.all_nodes()) == 1, "一次打断 + 一次续跑 = 一个节点"
    assert following.node.outline == "她话说到一半，停住了。"
    assert following.node.prose == "她把话说完了。"
    assert repo.state_at(following.node.id).values["地点"] == "里屋"
    assert repo.load_draft() is None


# -- 引导与打断：单位是一次操作，不是一轮（D119） --------------------------------


class _Watched(ScriptedLLM):
    """每次模型调用前后给测试一个插手的机会。

    要测的正是**两次操作之间**发生了什么，所以钩子必须挂在那个缝上。
    """

    def __init__(self, *replies, before=None, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*replies, **kwargs)
        self._before = before
        self.rounds = 0

    def _handle(self, request):  # type: ignore[no-untyped-def]
        self.rounds += 1
        if self._before:
            self._before(self.rounds)
        return super()._handle(request)


async def test_guidance_lands_between_two_operations_not_after_the_turn(
    tmp_path: Path,
) -> None:
    """引导在**下一个操作边界**进入对话，这一轮照常跑完，只产出一个节点。

    如果要等这一轮写完才说得上话，那不叫引导——用户看着 director 往错方向查了
    几次资料，只能事后补救。
    """
    inbox = Inbox()
    inbox.accepting = True

    def push_after_first_tool(round_no: int) -> None:
        if round_no == 2:  # 第一次工具调用已经做完，正要发第二次模型调用
            inbox.push("别去码头，让她留在店里")

    llm = _Watched(
        tool("state_query", keys=["地点"]),
        tool("state_write", set={"地点": "店里"}),
        tool("submit_outline", outline="她留在店里。"),
        text("她没有动。"),
        before=push_after_first_tool,
    )
    repo, session, gw, reg = _setup(tmp_path, llm)

    outcome = await execute_run(
        repo, session, gw, reg, user_input=UserInput(text="我推开门"), inbox=inbox
    )

    context = str(llm.messages_of(2))
    assert "别去码头，让她留在店里" in context, "引导必须在这一轮之内进入对话"
    assert len(repo.all_nodes()) == 1, "引导不开新的一轮"
    assert outcome.node.outline == "她留在店里。"
    assert repo.load_draft() is None


async def test_an_interrupt_kills_only_the_operation_in_flight(tmp_path: Path) -> None:
    """打断掐掉的是**这一次操作**，不是整个 run。

    掐掉之后带着话继续跑——所以这一轮仍然会产出节点，而且是**同一个**节点。
    如果实现成 cancel 当前 task，这里会拿不到节点。
    """
    inbox = Inbox()
    inbox.accepting = True
    inbox.push("等等，先别问他", interrupt=True)

    llm = ScriptedLLM(
        # 第一次操作得是**不终止的**，否则 loop 走不到边界，也就没地方接话。
        tool("state_query", keys=["地点"]),
        tool("submit_outline", outline="她把话咽了回去。"),
        text("她没有问出口。"),
        state=dict(set={"地点": "店里"}),
    )
    repo, session, gw, reg = _setup(tmp_path, llm)

    outcome = await execute_run(
        repo, session, gw, reg, user_input=UserInput(text="我推开门"), inbox=inbox
    )

    assert outcome.node.outline == "她把话咽了回去。"
    assert len(repo.all_nodes()) == 1
    # 插在**第一次操作之后**，所以出现在第二次模型调用的上下文里，而不是第一次。
    assert "等等，先别问他" not in str(llm.messages_of(0))
    assert "等等，先别问他" in str(llm.messages_of(1))


async def test_an_interrupt_with_nothing_to_say_suspends_the_run(tmp_path: Path) -> None:
    """打断但不说话 = "停下别跑了"：挂起成草稿，不落节点（D118）。"""
    inbox = Inbox()
    inbox.accepting = True
    inbox.push(interrupt=True)

    llm = ScriptedLLM(tool("state_write", set={"地点": "店里"}))
    repo, session, gw, reg = _setup(tmp_path, llm)

    with pytest.raises(asyncio.CancelledError):
        await execute_run(
            repo, session, gw, reg, user_input=UserInput(text="我推开门"), inbox=inbox
        )

    assert repo.all_nodes() == {}
    assert repo.load_draft() is not None


async def test_an_aborted_tool_call_still_gets_a_result(tmp_path: Path) -> None:
    """掐掉一次工具调用时必须补一条工具结果。

    **少这一条，下一次请求就不合法了**——`tool_calls` 没有对应的 tool 消息，
    端点会拒绝整份请求，而报错发生在之后，看上去和打断毫无关系。
    """
    inbox = Inbox()
    inbox.accepting = True

    slow = asyncio.Event()

    class SlowRegistry(ToolRegistry):
        async def dispatch(self, ctx, call):  # type: ignore[no-untyped-def]
            if call.name == "state_query":
                inbox.push("换个方向", interrupt=True)
                await slow.wait()  # 永远等不到——只能被掐掉
            return await super().dispatch(ctx, call)

    llm = ScriptedLLM(
        tools(("state_query", {"keys": ["地点"]}), ("state_write", {"set": {"地点": "店里"}})),
        tool("submit_outline", outline="她换了个方向。"),
        text("她转身。"),
        state=dict(set={"地点": "店里"}),
    )
    repo, session, gw, reg = _setup(tmp_path, llm)

    outcome = await execute_run(
        repo,
        session,
        gw,
        SlowRegistry(),
        user_input=UserInput(text="我推开门"),
        inbox=inbox,
    )

    # 第二次模型调用收到的消息序必须是配平的：每个 tool_call 都有一条 tool 消息。
    sent = llm.messages_of(1)
    issued = [c["id"] for m in sent if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in sent if m.get("role") == "tool"]
    assert sorted(issued) == sorted(answered)
    assert any("打断了这次调用" in (m.get("content") or "") for m in sent)
    assert outcome.node.outline == "她换了个方向。"
