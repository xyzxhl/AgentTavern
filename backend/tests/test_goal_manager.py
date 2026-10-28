"""goal_manager：一份跨 run 的长对话 + 三个工具（S92）。

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
PLAYER = "林昭"
"""用户扮演谁。**收的是解析好的名字**（S63）：那条「标了 is_player 的条目
优先」的规则只有 `player_character` 一处实现，goal_manager 不再自己判一遍。"""


def _node(index: int, *, source: InputSource = InputSource.SIMULATED, said: str = "我推开门"):
    return Node(
        id=f"run_{index}",
        parent_id=f"run_{index - 1}" if index else None,
        user_input=UserInput(text=said, source=source),
        prose=f"第{index}轮的正文。",
    )


def _joined(run: GoalRun) -> str:
    return "\n".join(m.content or "" for m in run.conversation)


# -- 对话怎么长 ---------------------------------------------------------------


def test_the_prefix_is_built_once_and_each_turn_only_appends() -> None:
    """**每轮只追加一条**——这才是维持对话真正买到的东西（S92）。

    以前每次调用都重发全量剧情梗概：梗概越来越长、前缀每轮失效，缓存完全用不上。
    """
    run = GoalRun()
    path = [_node(0), _node(1)]

    sync_conversation(PLAYER, GOAL, run, path)
    after_two = len(run.conversation)
    assert "写到他们摊牌为止" in _joined(run)

    path.append(_node(2))
    sync_conversation(PLAYER, GOAL, run, path)

    assert len(run.conversation) == after_two + 1, "只该多出第 3 轮那一条"
    assert run.seen_leaf_id == "run_2"


def test_user_interventions_survive_but_simulated_inputs_do_not_echo() -> None:
    """剧情事实只认正文；用户接管方向的输入与插话另外保留（S102）。

    goal_manager 自己写的模拟输入已经落实成正文，逐轮回灌只会让它反复解释自己的
    指令；用户亲自写的内容则是暂停期间接管方向的证据，不能丢。
    """
    mine = turn_record(_node(1, source=InputSource.SIMULATED, said="我把照片推过去"), 1, [])
    theirs = turn_record(_node(2, source=InputSource.USER, said="我不想去那儿"), 2, [])
    butted_in = turn_record(_node(3), 3, ["<用户追加输入>\n让她别走"])

    assert "我把照片推过去" not in (mine.content or "")
    assert "用户自己的输入" in (theirs.content or "")
    assert "用户插话" in (butted_in.content or "")
    assert "让她别走" in (butted_in.content or "")


def test_a_turn_without_prose_says_so_instead_of_falling_back() -> None:
    """**正文没写成时不要回退到细纲**（S94）。

    回退看起来只是"降级用一份更粗的记录"，实际上把一个失败悄悄补齐了：那一轮
    什么都没写出来，而 goal_manager 读到一段像模像样的剧情，会照着它判某个中间
    状态已经成立。
    """
    empty = Node(id="run_9", prose="")
    body = turn_record(empty, 9, []).content or ""

    assert "没有写出正文" in body


def test_switching_branches_rebuilds_instead_of_extending() -> None:
    """暂停期间用户切了分支 → **以最终选择的分支为准**（S103）。

    增量追加撤不回来：被放弃那一支的轮次会留在对话里。所以上次看到的那个节点
    不在当前路径上时，整份重建。
    """
    run = GoalRun(plan=GoalPlan(subgoals=[SubGoal(description="他先松口", done=True)]))
    sync_conversation(PLAYER, GOAL, run, [_node(0), _node(1)])
    assert "第1轮的正文。" in _joined(run)

    # 切到另一支：run_1 不在这条路径上了
    other = Node(id="run_x", parent_id="run_0", prose="另一支的正文。")
    sync_conversation(PLAYER, GOAL, run, [_node(0), other])

    body = _joined(run)
    assert "另一支的正文。" in body
    assert "第1轮的正文。" not in body, "被放弃那一支不该留在对话里"
    # 计划的权威独立保留；调用前再投影，不混进持久化产品记录。
    assert run.plan is not None and run.plan.subgoals[0].done


def test_before_and_during_the_goal_both_get_prose() -> None:
    """静态前缀与逐轮追加都只读正文（S93）。"""
    run = GoalRun(started_at_leaf_id="run_1")
    sync_conversation(PLAYER, GOAL, run, [_node(0), _node(1), _node(2), _node(3)])
    body = _joined(run)

    assert all(f"第{i}轮的正文。" in body for i in range(4))
    assert "细纲" not in body


def test_a_goal_set_at_session_start_treats_everything_as_during() -> None:
    """`started_at_leaf_id` 为空 = 这个 goal 从会话一开始就在，整条路径都算期间。"""
    run = GoalRun()
    sync_conversation(PLAYER, GOAL, run, [_node(0), _node(1)])
    body = _joined(run)

    assert "第0轮的正文。" in body and "第1轮的正文。" in body
    assert "还没有已经写出来的剧情" in body, "之前那段是空的，要说出来"


async def test_goal_history_compacts_like_director_and_can_expand(tmp_path: Path) -> None:
    """goal_manager 复用同一段落摘要、同一地址和同一展开工具。"""
    repo = _repo(tmp_path)
    first = _node(0, source=InputSource.USER, said="先记住门上的划痕")
    second = _node(1)
    second.parent_id = first.id
    second.prose_summary = "他们穿过了旧门。"
    second.segment_span = 2
    repo.save_node(first)
    repo.save_node(second)

    run = GoalRun()
    sync_conversation(PLAYER, GOAL, run, [first, second])
    body = _joined(run)
    assert "他们穿过了旧门" in body
    assert "第0轮的正文" not in body
    assert "先记住门上的划痕" in body, "用户接管方向的内容不能随正文折叠消失"
    assert second.id in body, "摘要留下的 key 必须能直接用于展开"

    ctx = GoalContext(goal=GOAL, run=run, repo=repo, parent_id=second.id)
    result = await _call(ctx, "history_read", paths=[[second.id]])
    assert not result.is_error
    assert "第0轮的正文" in result.content and "第1轮的正文" in result.content


# -- 工具 ---------------------------------------------------------------------


async def _call(ctx: GoalContext, name: str, **args):
    return await ToolRegistry.for_goal_manager().dispatch(
        ctx, ToolCall(id="c1", name=name, arguments=args)
    )


async def test_replanning_resets_progress() -> None:
    """**中间状态是脚手架，不是执行契约**（S93）。

    剧情变了就地重给一版，不限次数、不算失败。但重给会清空进度——新的一版一律
    从未成立算起，否则「已成立」会跨着两版混在一起。
    """
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙"])
    await _call(ctx, "check", reached=[1])
    await _call(ctx, "steer", next_input="我走过去")

    assert ctx.run.plan is not None
    assert [s.done for s in ctx.run.plan.subgoals] == [True, False]
    assert ctx.next_input == "我走过去"

    await _call(ctx, "plan", subgoals=["丙", "丁", "戊"])
    assert [s.done for s in ctx.run.plan.subgoals] == [False, False, False]


async def test_finish_does_not_check_the_subgoals() -> None:
    """`finish` **不检查中间状态是否全部成立**（S94）。

    S93 曾在这里加过一条硬前提，想靠它挡住自我确认倾向。但中间状态无序、可跳过、
    可能已经不必要（S93）——于是在「目标确实达成、只是某条中间状态已经作废」
    这个常见情形下，那条前提只是多逼出两次工具调用（先 `plan` 重给一版、再
    `check` 标全），然后原样通过。**挡不住真问题，只收了个过路费。**

    该由提示词担保的事交给提示词：判据写在 `goal_judge` 那一节里。
    """
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙"])

    ok = await _call(ctx, "finish", outcome="done", reason="逐条核对：目标那两项都已成立")
    assert not ok.is_error
    assert ctx.finished == (GoalOutcome.DONE, "逐条核对：目标那两项都已成立")


async def test_finish_failed_needs_no_such_precondition() -> None:
    """`failed` 是「当前局面下拆不出任何能到达目标的路径」——**阶段没走完正是常态**。"""
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙"])

    result = await _call(ctx, "finish", outcome="failed", reason="用户把剧情带去了别的城市")
    assert not result.is_error
    assert ctx.finished is not None and ctx.finished[0] is GoalOutcome.FAILED


async def test_progress_is_reported_as_what_is_left_not_as_a_next_one() -> None:
    """**中间状态之间没有先后依赖**（S93）：可以跳过、乱序、同时成立。

    所以回执列的是「还剩哪些」，不是「下一个是哪个」——挑出一个说"当前这个"
    等于凭空造了个顺序，而模型会照着那个顺序推。
    """
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    await _call(ctx, "plan", subgoals=["甲", "乙", "丙"])

    # 跳过甲，直接成立乙和丙——这在新语义下完全正常。
    result = await _call(ctx, "check", reached=[2, 3])

    assert "甲" in result.content
    assert "乙" not in result.content and "丙" not in result.content
    assert "当前" not in result.content


async def test_checking_without_a_plan_is_recoverable() -> None:
    """还没给过中间状态就来核对 → 可恢复的错误，回一句让它先给一版。"""
    ctx = GoalContext(goal=GOAL, run=GoalRun())
    result = await _call(ctx, "check", reached=[])
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
            ("steer", {"next_input": "我把照片推过去。"}),
        ),
        text("先推到这里。"),
    )
    run = GoalRun()
    sync_conversation(PLAYER, GOAL, run, [_node(0)])
    before = len(run.conversation)

    step = await advance_goal(llm.gateway(), Tracer(_repo(tmp_path), "run_0"), GOAL, run)

    assert step.next_input == "我把照片推过去。"
    assert step.finished is None
    assert run.plan is not None and len(run.plan.subgoals) == 2
    assert len(run.conversation) == before, "工具往返只活在这一次调用里"
    assert "先推到这里。" not in _joined(run)


async def test_a_round_that_never_stops_calling_tools_is_cut_off(tmp_path: Path) -> None:
    """**收口条件靠不住时得有个保底**（同 director / state_manager 的硬上限）。

    出口是"这一次不再调用工具"，而那是模型自己的选择——它完全可以每一轮都
    再调一次。没有上限的话这里就是个死循环，而且每转一圈都是一次真实的模型
    调用：卡住的不只是这一轮，是整条自动续跑的链。

    撞上上限**不是失败**，只是这一轮到此为止：写下的东西照常生效，终态留空，
    下一轮接着判。
    """
    # 脚本只剩一条时会一直重复它——一个永远在调工具、永远不收口的模型。
    llm = ScriptedLLM(tool("plan", subgoals=["甲"]))
    run = GoalRun()
    sync_conversation(PLAYER, GOAL, run, [_node(0)])
    before = len(run.conversation)

    step = await advance_goal(
        llm.gateway(), Tracer(_repo(tmp_path), "run_0"), GOAL, run, max_rounds=3
    )

    assert len(llm.requests) == 3, "撞上上限就得停，不能一直转"
    assert step.finished is None, "撞上限不是终态——下一轮接着判"
    assert len(run.conversation) == before, "即使撞上上限也不把工具过程写回"
    assert run.plan is not None


async def test_finishing_comes_back_as_an_outcome(tmp_path: Path) -> None:
    """`finish` 调完就不必再往下跑——终态已经定了。"""
    llm = ScriptedLLM(
        tool("plan", subgoals=["甲"]),
        tool("check", reached=[1]),
        tool("finish", outcome="done", reason="逐条核对：甲已兑现"),
        text("完了。"),
    )
    run = GoalRun()
    sync_conversation(PLAYER, GOAL, run, [_node(0)])

    step = await advance_goal(llm.gateway(), Tracer(_repo(tmp_path), "run_0"), GOAL, run)

    assert step.finished == ("done", "逐条核对：甲已兑现")


# -- 预算 ---------------------------------------------------------------------


def test_a_spent_budget_is_a_pause_not_a_failure() -> None:
    """**预算耗尽不是终态**（S96）：用户可以把上限调大接着跑。

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


def test_product_records_and_plan_survive_a_round_trip_through_disk(tmp_path: Path) -> None:
    """跨进程保留剧情产品记录与权威 plan，不保留工具执行流量。"""
    repo = _repo(tmp_path)
    session = repo.load_session()
    session.goal_run = GoalRun(
        conversation=[
            Message(role=Role.USER, content="第 1 轮"),
        ],
        plan=GoalPlan(subgoals=[SubGoal(description="甲", done=True)]),
        spent_runs=2,
        seen_leaf_id="run_1",
    )
    repo.save_session(session)

    back = repo.load_session().goal_run
    assert back is not None
    assert [m.role for m in back.conversation] == [Role.USER]
    assert back.plan is not None and back.plan.all_done
    assert back.spent_runs == 2
    assert json.loads(repo.session_path.read_text(encoding="utf-8"))["goal_run"]["spent_runs"] == 2


async def test_the_current_plan_is_injected_without_being_persisted(tmp_path: Path) -> None:
    llm = ScriptedLLM(text("先停在这里。"))
    run = GoalRun(
        conversation=[Message(role=Role.USER, content="产品记录")],
        plan=GoalPlan(subgoals=[SubGoal(description="他先松口", done=True)]),
    )

    await advance_goal(llm.gateway(), Tracer(_repo(tmp_path), "run_0"), GOAL, run)

    sent = llm.requests[0]["messages"]
    joined = "\n".join(str(message.get("content") or "") for message in sent)
    assert "他先松口" in joined and "已成立" in joined
    assert [message.content for message in run.conversation] == ["产品记录"]


def test_it_is_told_who_the_user_plays() -> None:
    """**它替用户写输入，得知道用户扮演谁**（S27）。

    实跑 `noir_goal` 时它写出了「我从外套内袋里摸出那张照片」——第一人称写对了，
    但那是**从种子输入的措辞里猜的**。种子恰好是第一人称就蒙对，换成第三人称、
    或者根本没有种子（goal 从会话一开始就在），它就只能瞎猜。
    """
    run = GoalRun()
    sync_conversation(PLAYER, GOAL, run, [_node(0)])
    assert "林昭" in _joined(run)

    # 没设这个字段时整节不出现——退回去猜，和以前一样，但不会凭空冒出一个名字。
    blank = GoalRun()
    sync_conversation("", GOAL, blank, [_node(0)])
    assert "用户扮演的角色" not in _joined(blank)
