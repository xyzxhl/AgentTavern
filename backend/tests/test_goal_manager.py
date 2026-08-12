"""goal_manager：一份跨 run 的长对话 + 三个工具（D148）。

这里守的是三件事：

- **对话怎么长**（静态前缀 + 每轮追加一条带来源标注的记录；切了分支就重建）。
- **三个工具各自的语义**，尤其 `finish(done)` 那条硬前提。
- **终态只有两种**，预算耗尽是暂停不是失败。
"""

import json
from pathlib import Path

from conftest import ScriptedLLM, text, tool, tools

from agenttavern.agents.goal_manager import advance_goal, sync_conversation, turn_record
from agenttavern.llm import Message, Role, ToolCall
from agenttavern.models import (
    Goal,
    GoalOutcome,
    GoalPlan,
    GoalRun,
    InputSource,
    Node,
    SubGoal,
    UserInput,
)
from agenttavern.storage import Store
from agenttavern.tools import GoalContext, ToolRegistry
from agenttavern.tracing import Tracer

GOAL = Goal(description="写到他们摊牌为止")


def _node(index: int, *, source: InputSource = InputSource.SIMULATED, said: str = "我推开门"):
    return Node(
        id=f"run_{index}",
        parent_id=f"run_{index - 1}" if index else None,
        user_input=UserInput(text=said, source=source),
        outline=f"第{index}轮的细纲。",
        prose=f"第{index}轮的正文。",
    )


def _joined(run: GoalRun) -> str:
    return "\n".join(m.content or "" for m in run.conversation)


# -- 对话怎么长 ---------------------------------------------------------------


def test_the_prefix_is_built_once_and_each_turn_only_appends() -> None:
    """**每轮只追加一条**——这才是维持对话真正买到的东西（D148）。

    以前每次调用都重发全量剧情梗概：梗概越来越长、前缀每轮失效，缓存完全用不上。
    """
    run = GoalRun()
    path = [_node(0), _node(1)]

    sync_conversation(GOAL, run, path)
    after_two = len(run.conversation)
    assert "写到他们摊牌为止" in _joined(run)

    path.append(_node(2))
    sync_conversation(GOAL, run, path)

    assert len(run.conversation) == after_two + 1, "只该多出第 3 轮那一条"
    assert run.seen_leaf_id == "run_2"


def test_each_turn_says_who_gave_the_input() -> None:
    """**来源标注是这条消息里最要紧的东西**（D148）。

    剧情偏了，是 goal_manager 自己推错了，还是用户主动把剧情带走了？后者强得多——
    用户的意图优先于目标。不标注的话两种偏离在梗概里长得一模一样，
    而应对完全不同。
    """
    mine = turn_record(_node(1, source=InputSource.SIMULATED, said="我把照片推过去"), 1, [])
    theirs = turn_record(_node(2, source=InputSource.USER, said="我不想去那儿"), 2, [])
    butted_in = turn_record(_node(3), 3, ["<用户追加输入>\n让她别走"])

    assert "我写的输入" in (mine.content or "")
    assert "用户自己的输入" in (theirs.content or "")
    assert "用户插话" in (butted_in.content or "")
    assert "让她别走" in (butted_in.content or "")


def test_switching_branches_rebuilds_instead_of_extending() -> None:
    """暂停期间用户切了分支 → **以最终选择的分支为准**（D148）。

    增量追加撤不回来：被放弃那一支的轮次会留在对话里。所以上次看到的那个节点
    不在当前路径上时，整份重建。
    """
    run = GoalRun(plan=GoalPlan(subgoals=[SubGoal(description="他先松口", done=True)]))
    sync_conversation(GOAL, run, [_node(0), _node(1)])
    assert "第1轮的正文。" in _joined(run)

    # 切到另一支：run_1 不在这条路径上了
    other = Node(id="run_x", parent_id="run_0", outline="另一支的细纲。", prose="另一支的正文。")
    sync_conversation(GOAL, run, [_node(0), other])

    body = _joined(run)
    assert "另一支的正文。" in body
    assert "第1轮的正文。" not in body, "被放弃那一支不该留在对话里"
    # 重建丢掉工具调用的历史，但**计划与进度以投影的形式留着**。
    assert "他先松口" in body and "已达成" in body


def test_before_the_goal_gets_outlines_and_during_gets_prose() -> None:
    """**路径切成两段**（D150）。

    goal 设立**之前**那段进静态前缀、给细纲——它只回答"故事到这儿了"，细纲够用
    而且便宜，还全量给一次就够。goal **期间**每轮逐条追加、给正文——要判的是
    "这一步到底有没有发生"，而**细纲只说定了什么，正文才是最终写出来的东西**。
    """
    run = GoalRun(started_at_leaf_id="run_1")
    sync_conversation(GOAL, run, [_node(0), _node(1), _node(2), _node(3)])
    body = _joined(run)

    # 之前那两轮：细纲在、正文不在
    assert "第0轮的细纲。" in body and "第1轮的细纲。" in body
    assert "第0轮的正文。" not in body and "第1轮的正文。" not in body
    # 之后那两轮：正文在
    assert "第2轮的正文。" in body and "第3轮的正文。" in body


def test_a_goal_set_at_session_start_treats_everything_as_during() -> None:
    """`started_at_leaf_id` 为空 = 这个 goal 从会话一开始就在，整条路径都算期间。"""
    run = GoalRun()
    sync_conversation(GOAL, run, [_node(0), _node(1)])
    body = _joined(run)

    assert "第0轮的正文。" in body and "第1轮的正文。" in body
    assert "还没有已经写出来的剧情" in body, "之前那段是空的，要说出来"


# -- 三个工具 -----------------------------------------------------------------


async def _call(ctx: GoalContext, name: str, **args):
    return await ToolRegistry.for_goal_manager().dispatch(
        ctx, ToolCall(id="c1", name=name, arguments=args)
    )


async def test_replanning_resets_progress() -> None:
    """**中间状态是脚手架，不是执行契约**（D148）。

    剧情变了就地重给一版，不限次数、不算失败。但重给会清空进度——新的一版一律
    从未成立算起，否则「已成立」会跨着两版混在一起。
    """
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙"])
    await _call(ctx, "advance", reached=[1], next_input="我走过去")

    assert ctx.run.plan is not None
    assert [s.done for s in ctx.run.plan.subgoals] == [True, False]
    assert ctx.next_input == "我走过去"

    await _call(ctx, "plan", subgoals=["丙", "丁", "戊"])
    assert [s.done for s in ctx.run.plan.subgoals] == [False, False, False]


async def test_finish_done_is_blocked_until_every_stage_is_marked() -> None:
    """**硬前提**：宣布完成之前，必须用**当前的**计划重核一遍（D148）。

    它挡的不是"剧情没按计划走"——那种情况该重拆。它挡的是模型的**自我确认倾向**：
    维持对话之后它读的是自己写的计划、自己判的进度，比干净上下文更容易往
    "已经达标"的方向读。

    两条合起来才是要点：**重拆随时可以，而宣布完成之前必须重核一遍**，
    不是照着三轮前那份打勾。
    """
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙"])

    blocked = await _call(ctx, "finish", outcome="done", reason="我觉得差不多了")
    assert blocked.is_error
    assert "乙" in blocked.content, "要点名还差哪些"
    assert ctx.finished is None

    await _call(ctx, "advance", reached=[1, 2])
    ok = await _call(ctx, "finish", outcome="done", reason="逐条核对：甲已兑现，乙已兑现")
    assert not ok.is_error
    assert ctx.finished == (GoalOutcome.DONE, "逐条核对：甲已兑现，乙已兑现")


async def test_finish_failed_needs_no_such_precondition() -> None:
    """`failed` 是「当前局面下拆不出任何能到达目标的路径」——**阶段没走完正是常态**。"""
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙"])

    result = await _call(ctx, "finish", outcome="failed", reason="用户把剧情带去了别的城市")
    assert not result.is_error
    assert ctx.finished is not None and ctx.finished[0] is GoalOutcome.FAILED


async def test_progress_is_reported_as_what_is_left_not_as_a_next_one() -> None:
    """**中间状态之间没有先后依赖**（D150）：可以跳过、乱序、同时成立。

    所以回执列的是「还剩哪些」，不是「下一个是哪个」——挑出一个说"当前这个"
    等于凭空造了个顺序，而模型会照着那个顺序推。
    """
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙", "丙"])

    # 跳过甲，直接成立乙和丙——这在新语义下完全正常。
    result = await _call(ctx, "advance", reached=[2, 3], next_input="我走过去")

    assert "甲" in result.content
    assert "乙" not in result.content and "丙" not in result.content
    assert "当前" not in result.content


async def test_advance_without_a_plan_is_recoverable() -> None:
    """没有计划就推进 → 可恢复的错误，回一句让它先拆，而不是让这一轮崩掉。"""
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    result = await _call(ctx, "advance", reached=[], next_input="随便")
    assert result.is_error
    assert "plan" in result.content


# -- 一轮 loop ----------------------------------------------------------------


async def test_a_round_ends_when_it_stops_calling_tools(tmp_path: Path) -> None:
    """收口 = **这一次不再调用任何工具**（同 state_manager）。

    那条纯文本不入对话——它是收口的信号，不是内容，留着只会在下一轮变成一句
    没头没尾的自言自语。
    """
    llm = ScriptedLLM(
        tools(
            ("plan", {"subgoals": ["他先松口", "两人摊开"]}),
            ("advance", {"reached": [], "next_input": "我把照片推过去。"}),
        ),
        text("先推到这里。"),
    )
    run = GoalRun()
    sync_conversation(GOAL, run, [_node(0)])
    before = len(run.conversation)

    step = await advance_goal(llm.gateway(), Tracer(_repo(tmp_path), "run_0"), GOAL, run)

    assert step.next_input == "我把照片推过去。"
    assert step.finished is None
    assert run.plan is not None and len(run.plan.subgoals) == 2
    # 新增的是「assistant + 两条 tool 结果」，那句收口的文本不在里面。
    assert len(run.conversation) == before + 3
    assert "先推到这里。" not in _joined(run)


async def test_finishing_comes_back_as_an_outcome(tmp_path: Path) -> None:
    """`finish` 调完就不必再往下跑——终态已经定了。"""
    llm = ScriptedLLM(
        tool("plan", subgoals=["甲"]),
        tool("advance", reached=[1], next_input=""),
        tool("finish", outcome="done", reason="逐条核对：甲已兑现"),
        text("完了。"),
    )
    run = GoalRun()
    sync_conversation(GOAL, run, [_node(0)])

    step = await advance_goal(llm.gateway(), Tracer(_repo(tmp_path), "run_0"), GOAL, run)

    assert step.finished == ("done", "逐条核对：甲已兑现")


# -- 预算 ---------------------------------------------------------------------


def test_a_spent_budget_is_a_pause_not_a_failure() -> None:
    """**预算耗尽不是终态**（D148）：用户可以把上限调大接着跑。

    所以它不是 `GoalOutcome` 的一种——那里只有 `done` 和 `failed`，
    两个都意味着这个目标结束了。
    """
    goal = Goal(description="x", max_runs=3, max_total_tokens=1000)

    assert GoalRun(spent_runs=1, spent_tokens=10).exhausted(goal) is None
    assert "3 轮" in (GoalRun(spent_runs=3).exhausted(goal) or "")
    assert "token" in (GoalRun(spent_tokens=9999).exhausted(goal) or "")

    assert {o.value for o in GoalOutcome} == {"done", "failed"}


def _repo(tmp_path: Path):
    from agenttavern.models import Session

    return Store(tmp_path).create_session(Session())


def test_the_conversation_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    """对话跨 run 活着，所以它**必须能落盘再读回来**——包括 `tool_calls`。

    goal 是跨进程的：服务重启之后那份对话还得接得上，否则「维持对话」只在
    单个进程里成立。
    """
    repo = _repo(tmp_path)
    session = repo.load_session()
    session.goal_run = GoalRun(
        conversation=[
            Message(role=Role.USER, content="第 1 轮"),
            Message(role=Role.ASSISTANT, tool_calls=[ToolCall(id="c", name="plan", arguments={})]),
            Message(role=Role.TOOL, content="ok", tool_call_id="c"),
        ],
        plan=GoalPlan(subgoals=[SubGoal(description="甲", done=True)]),
        spent_runs=2,
        seen_leaf_id="run_1",
    )
    repo.save_session(session)

    back = repo.load_session().goal_run
    assert back is not None
    assert [m.role for m in back.conversation] == [Role.USER, Role.ASSISTANT, Role.TOOL]
    assert back.conversation[1].tool_calls[0].name == "plan"
    assert back.plan is not None and back.plan.all_done
    assert back.spent_runs == 2
    assert json.loads(repo.session_path.read_text(encoding="utf-8"))["goal_run"]["spent_runs"] == 2
