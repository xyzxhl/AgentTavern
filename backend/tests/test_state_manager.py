"""state_manager：状态维护后置到 writer 之后（S7）。

这里守的是三件事，各自坏起来的样子完全不同：

- **它看得见什么**（正文在、世界设定不在）——上下文切错了，隔离就没了。
- **loop 怎么收口**（不再调用工具即结束，没有终止工具）。
- **它坏掉时不拖累别人**（fail-open：这一轮不记增量，节点照落）。
"""

import asyncio
import json
from pathlib import Path

import pytest
from conftest import STATE_MARK, ScriptedLLM, text, tool, using

from agenttavern.agents.state_manager import MAX_ROUNDS, run_state_manager
from agenttavern.models import (
    Node,
    Session,
    StateDelta,
    UserInput,
    WorldbookEntry,
)
from agenttavern.orchestration import execute_run
from agenttavern.prompting import state_recent_runs
from agenttavern.storage import Store
from agenttavern.tools import RunContext, ToolRegistry
from agenttavern.tracing import Tracer


def _ctx(tmp_path: Path, llm: ScriptedLLM, session: Session | None = None):
    repo = Store(tmp_path).create_session(session or Session())
    ctx = RunContext(run_id="run_1", repo=repo, session=repo.load_session(), gateway=llm.gateway())
    registry = ToolRegistry.for_state_manager()
    return ctx, Tracer(repo, "run_1"), registry


# -- 它看得见什么 -------------------------------------------------------------


async def test_it_reads_the_prose_it_just_wrote(tmp_path: Path) -> None:
    """**这是后置买到的东西**：细纲常常留白，正文才落实成具体那句话。

    边推演边记的人只能记那个含糊的版本——它当时还不知道最后会写成什么样。
    """
    llm = ScriptedLLM(text("无变化。"), auto_state=False)
    ctx, tracer, reg = _ctx(tmp_path, llm)

    await run_state_manager(
        ctx, reg, tracer, outline="他含糊地应了一句。", prose="「……回头再说吧。」"
    )

    sent = str(llm.messages_of(0))
    assert "他含糊地应了一句。" in sent
    assert "「……回头再说吧。」" in sent


async def test_it_does_not_read_the_world_or_the_goal(tmp_path: Path) -> None:
    """它不创作，所以不给它任何"该写什么"的依据——只给"发生了什么"。"""
    llm = ScriptedLLM(text("无变化。"), auto_state=False)
    ctx, tracer, reg = _ctx(tmp_path, llm, Session())
    # 题材和设定都是 worldbook 条目（S64）——两样都不该到它手里。
    book = ctx.repo.book()
    book.save_entry(WorldbookEntry(name="题材与基调", content="低魔悬疑"))
    book.save_entry(WorldbookEntry(name="教团", content="总部在旧城区。"))

    await run_state_manager(ctx, reg, tracer, outline="她推开门。", prose="门开了。")

    sent = str(llm.messages_of(0))
    assert "低魔悬疑" not in sent
    assert "总部在旧城区" not in sent


async def test_recent_turns_carry_the_full_triple(tmp_path: Path) -> None:
    """**「细纲 → 成文 → 当时记下的」三样缺一不可。**

    少了细纲看不出"定下的事实"和"记下的键"之间的取舍，少了正文看不出细纲留白
    处最后落成了什么，少了增量就根本没有示范——而这一节存在的全部理由就是示范，
    没有它键名和值的形状会一轮一个样。
    """
    llm = ScriptedLLM(text("无变化。"), auto_state=False)
    ctx, tracer, reg = _ctx(tmp_path, llm)
    ctx.repo.save_node(
        Node(
            id="run_0",
            outline="她把照片推过去。",
            prose="照片正面朝上，停在他手边。",
            state_delta=StateDelta(set={"照片.位置": "陈屿手边"}),
        )
    )
    ctx.parent_id = "run_0"

    await run_state_manager(ctx, reg, tracer, outline="他没有去看。", prose="他没有低头。")

    sent = str(llm.messages_of(0))
    assert "她把照片推过去。" in sent
    assert "照片正面朝上，停在他手边。" in sent
    assert "照片.位置" in sent and "陈屿手边" in sent


async def test_only_the_last_few_turns_come_along(tmp_path: Path) -> None:
    """滑动窗口，宽度是 `state_manager.yaml` 里 `recent` 那条 context 上的
    `runs`（默认 3，S80）——**它从来不是一条独立的配置，是那段 context
    的属性**。

    **这是它最大的一笔开销**——前几轮的正文占了上下文的大头，而且每轮全变，
    一点缓存前缀都吃不到。所以窗口是用户可调的，不是代码里的常量。
    """
    llm = ScriptedLLM(text("无变化。"), auto_state=False)
    ctx, tracer, reg = _ctx(tmp_path, llm)
    window = state_recent_runs()

    parent = None
    for i in range(window + 2):
        node = Node(
            id=f"run_{i}", parent_id=parent, outline=f"第{i}轮细纲。", prose=f"第{i}轮正文。"
        )
        ctx.repo.save_node(node)
        parent = node.id
    ctx.parent_id = parent

    await run_state_manager(ctx, reg, tracer, outline="本轮。", prose="本轮正文。")

    sent = str(llm.messages_of(0))
    assert "第0轮细纲。" not in sent, f"只该带最近 {window} 轮"
    assert f"第{window + 1}轮细纲。" in sent


# -- loop 怎么收口 ------------------------------------------------------------


async def test_the_exit_is_calling_no_tool(tmp_path: Path) -> None:
    """**没有终止工具是有意的**。

    director 需要 `submit_outline` 是因为它要交付一件产物，交付这个动作得有地方
    承接；这里的产物就是那几次写入本身，写完就没事了。
    """
    llm = ScriptedLLM(
        tool("state_write", set={"地点": "客厅"}),
        text("记完了。"),
        auto_state=False,
    )
    ctx, tracer, reg = _ctx(tmp_path, llm)

    result = await run_state_manager(ctx, reg, tracer, outline="她推开门。", prose="门开了。")

    assert "submit_outline" not in reg.names
    assert result.rounds == 2
    assert result.delta.set["地点"] == "客厅"


async def test_several_writes_accumulate_into_one_delta(tmp_path: Path) -> None:
    """一轮内可多次写入，累积成**一份**增量——节点与 run 一一对应（S1/S20）。"""
    llm = ScriptedLLM(
        tool("state_write", set={"地点": "客厅"}),
        tool("state_write", append={"林.知情": ["照片"]}),
        text("记完了。"),
        auto_state=False,
    )
    ctx, tracer, reg = _ctx(tmp_path, llm)

    result = await run_state_manager(ctx, reg, tracer, outline="她推开门。", prose="门开了。")

    assert result.delta.set["地点"] == "客厅"
    assert result.delta.append["林.知情"] == ["照片"]


async def test_an_empty_hand_means_nothing_changed(tmp_path: Path) -> None:
    """**E0 契约已作废**（「没变化也必须调用一次」）。

    director 会忘是因为它同时在推演；这个 agent 唯一的任务就是记状态，空手回来
    就当没变化——为消这个歧义再花一轮往返不值。
    """
    llm = ScriptedLLM(text("这一轮没什么要改的。"), auto_state=False)
    ctx, tracer, reg = _ctx(tmp_path, llm)

    result = await run_state_manager(ctx, reg, tracer, outline="他们沉默着。", prose="没人说话。")

    assert result.rounds == 1
    assert result.delta.is_empty()


async def test_a_model_that_never_stops_hits_the_cap(tmp_path: Path) -> None:
    """硬上限保底——模型可能一直写下去。"""
    llm = ScriptedLLM(tool("state_write", set={"地点": "客厅"}), auto_state=False)
    ctx, tracer, reg = _ctx(tmp_path, llm)

    result = await run_state_manager(
        ctx, reg, tracer, outline="她推开门。", prose="门开了。", max_rounds=MAX_ROUNDS
    )
    assert result.rounds == MAX_ROUNDS


# -- 它坏掉时 -----------------------------------------------------------------


async def test_a_failure_does_not_cost_the_turn(tmp_path: Path) -> None:
    """fail-open：这一轮不记增量，**节点照落**（同归段失败）。

    已经产出的细纲和正文比一份增量值钱得多，不该被状态维护拖下水。
    """

    class Broken(ScriptedLLM):
        def _handle(self, request):  # type: ignore[no-untyped-def]
            if STATE_MARK in json.loads(request.content)["messages"][0].get("content", ""):
                raise RuntimeError("端点炸了")
            return super()._handle(request)

    llm = Broken(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        auto_state=False,
    )
    repo = Store(tmp_path).create_session(Session())

    outcome = await execute_run(
        repo,
        repo.load_session(),
        llm.gateway(),
        ToolRegistry(),
        user_input=UserInput(text="我推开门"),
    )

    assert outcome.node.outline == "她推开门。"
    assert outcome.node.prose == "门开了。"
    assert outcome.node.state_delta.is_empty()


async def test_an_interrupt_is_not_a_failure(tmp_path: Path) -> None:
    """打断是用户喊停，交给上层挂起——**不能被 fail-open 吞掉**。"""

    class Stopped(ScriptedLLM):
        def _handle(self, request):  # type: ignore[no-untyped-def]
            if STATE_MARK in json.loads(request.content)["messages"][0].get("content", ""):
                raise asyncio.CancelledError
            return super()._handle(request)

    llm = Stopped(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        auto_state=False,
    )
    repo = Store(tmp_path).create_session(Session())

    with pytest.raises(asyncio.CancelledError):
        await execute_run(
            repo,
            repo.load_session(),
            llm.gateway(),
            ToolRegistry(),
            user_input=UserInput(text="我推开门"),
        )


async def test_state_off_means_it_never_runs(tmp_path: Path) -> None:
    """**关掉这个 agent 就没人写 state**（S80）。

    开关是 `state_manager.yaml` 顶上的 `enabled`——它从来就是「这个 agent
    跑不跑」，而不是一条独立的能力，所以不需要第二个。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        state=dict(set={"不该出现": True}),
    )
    repo = Store(tmp_path).create_session(Session())

    with using(agents={"state_manager": False}):
        outcome = await execute_run(
            repo,
            repo.load_session(),
            llm.gateway(),
            ToolRegistry.for_session(),
            user_input=UserInput(text="我推开门"),
        )

    assert outcome.node.state_delta.is_empty()
    assert not any(STATE_MARK in str(r["messages"]) for r in llm.requests), "它根本不该被叫起来"
