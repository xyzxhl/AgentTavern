"""goal_manager：目标管理者（R24 / S92）。

**它是第四个 agent，不是 director 顺手做的一步。** 叫「管理者」而不是「判定者」，
因为判定只是它几件事里的一件——它还拆阶段、核对进度、替用户写下一条输入。

上下文只有目标、推进节奏、当前计划，以及目标前后统一的逐轮正文。
它不读世界设定和状态——它管的是"推到哪儿了"，不是"这一轮怎么写"。

## 一份跨 run 的长对话，不是三次干净调用

早先那一版把三件事拆成三个 `task` 档、三次独立调用，每次重发全量剧情梗概。
两个毛病：

- **梗概越来越长，前缀每轮失效**，缓存完全用不上。现在静态前缀跨整个 goal
  生命周期不变，每轮只追加一条「第 N 轮发生了什么」。
- **replan 只在「全部阶段走完」之后才有机会发生**，而剧情偏离随时发生。于是
  偏了之后还要抱着一份垮掉的计划推好几轮，直到名义上走完才被发现。

现在模型自己决定这一轮调哪几个工具：平常 `advance`，偏了就先 `plan` 重拆，
该收了就 `finish`。**收口条件是「这一次不再调用任何工具」**（同 state_manager）。

## subgoal 是脚手架，不是执行契约

它们存在的目的是逼模型在动手前先想清楚路径；某一条最终有没有原样实现并不要紧。
**但 `finish(done)` 要求全部标为达成**——两条合起来才是要点：重拆随时可以，
而宣布完成之前必须用**当前的**计划重核一遍，不是照着三轮前那份打勾。
维持对话之后模型读的是自己写的计划、自己判的进度，比干净上下文更容易往
"已经达标"的方向读，这条硬前提挡的就是它。

## 终态只有两种

`done` / `failed`。`replan` 不是去向是动作，`derailed` 并进了 `failed`。
**预算耗尽不是终态**——用户可以加预算接着跑，所以它是暂停（`GoalRun.paused`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import LIMITS
from ..llm import LLMGateway, Message, OnDelta, Role
from ..models import (
    Actor,
    Goal,
    GoalRun,
    Node,
    TraceEventType,
)
from ..prompting import assemble, call_of, rendering, tool_enabled
from ..storage import SessionRepository
from ..tools import GoalContext, ToolRegistry
from ..tracing import Tracer
from .replay import goal_background_messages, goal_replay_messages, goal_turn_record

MAX_ROUNDS = LIMITS.goal_manager.max_rounds
"""一轮里最多几次模型往返。正常一到两次（调工具、然后空手回来收口）。

**收口是模型自己的选择**，所以得有这条保底：一个每轮都再调一次工具的模型，
在这里就是个死循环，而且每转一圈都是一次真实调用——卡住的不只是这一轮，
是整条自动续跑的链。撞上它不是失败，只是这一轮到此为止（`advance_goal`）。
"""


@dataclass
class GoalStep:
    """goal_manager 这一轮的产出。"""

    next_input: str | None = None
    """替用户写的下一条输入，喂给下一轮自动续跑。"""

    finished: tuple[str, str] | None = None
    """``(outcome, reason)``。非空即该交回控制权。"""


def render_before(nodes: list[Node], *, readable: bool) -> str:
    """goal 设立之前的正文历史（S93）。

    它是背景事实，不是仍在生效的用户方向，因此只给正文；越界时与 director
    共用段落摘要、折叠时机和可展开地址。
    """
    if not nodes:
        return "（还没有已经写出来的剧情。）"
    return "\n\n".join(
        message.content or "" for message in goal_background_messages(nodes, readable=readable)
    )


def _prefix(
    user_character: str, goal: Goal, before: list[Node], *, readable: bool
) -> list[Message]:
    """静态前缀：任务说明 + 用户扮演谁 + 目标 + 节奏 + goal 之前的剧情。

    **整个 goal 生命周期不变**——"之前"那段不会再长，goal 期间的轮次逐条追加在后面。

    **`user_character` 必须给**（S27）：它替用户写输入，不知道用户扮演谁就只能
    从种子输入的措辞里猜人称。猜对是运气——种子恰好是第一人称时它跟着写"我"，
    而那一句本可以是第三人称、也可能根本没有种子（goal 从会话一开始就在）。
    没设这个字段时整节不出现，它退回去猜，和以前一样。

    收的是**已经解析好的名字**而不是整份 `WorldMeta`（S63）：标了 `is_player`
    的条目优先于那个遗留字段，而那条规则只有一处实现（`player_character`），
    在这里再判一次迟早会和它分叉。
    """
    return assemble(
        "goal_manager",
        contexts={
            "user_character": user_character,
            "goal": goal.description,
            "history_prose": render_before(before, readable=readable),
        },
    ).messages


def turn_record(node: Node, index: int, interjections: list[str]) -> Message:
    """兼容旧调用点：正文，加上用户亲自给出的输入与插话。

    goal 自己写的模拟输入不回灌；它已经落实成正文。用户输入与插话是接管方向的
    信号，暂停期间尤其必须保留（S102）。
    """
    copy = node.model_copy(update={"interjections": interjections})
    return goal_turn_record(copy, index)


def plan_snapshot(run: GoalRun) -> Message | None:
    """每次调用前注入的「当前计划与进度」。

    goal_manager 自己的工具调用历史不会跨 turn 保留，但计划和进度以权威状态投影
    完整留着——这正是"权威在投影不在对话"买到的。
    """
    if run.plan is None or not run.plan.subgoals:
        return None
    body = rendering("goal_manager", "plan_snapshot", body=run.plan.render())
    return Message(role=Role.USER, content=body)


def sync_conversation(
    user_character: str,
    goal: Goal,
    run: GoalRun,
    path: list[Node],
) -> None:
    """把对话补到当前路径的末端。**能续就续，续不上就整份重建。**

    续得上的条件是：已有对话，且上次看到的那个节点**还在当前路径上**——或者
    恰好是**切点**那个（见下）。不满足就重建——那正是暂停期间用户切了分支的
    情形，而「以最终选择的分支为准」只有重建做得到：增量追加撤不回来（S103）。

    持久层只存产品记录。当前计划不混进这条追加链，而是在每次模型调用前由
    ``plan_snapshot`` 动态放到末尾；工具对话只在本次调用的临时消息里存在。
    """
    # 路径仍切成静态前缀与 goal 期间两段；两边共用 director 的压缩时机、
    # 段落摘要和 history_read 地址。模型执行流量不进入这份产品记录。
    ids = [n.id for n in path]
    cut = ids.index(run.started_at_leaf_id) + 1 if run.started_at_leaf_id in ids else 0
    before, during = path[:cut], path[cut:]
    readable = tool_enabled("goal_manager", "history_read")
    # 每次由权威路径重建；内容不变的前缀仍是逐字稳定的，缓存不受影响。这样压缩触发
    # 和分支切换都只有一个来源，不再维护一套只能增长、无法折叠的第二历史。
    run.conversation = _prefix(user_character, goal, before, readable=readable)
    run.conversation.extend(goal_replay_messages(during, readable=readable, start_index=cut + 1))
    run.seen_leaf_id = path[-1].id if path else None


async def advance_goal(
    gateway: LLMGateway,
    tracer: Tracer,
    goal: Goal,
    run: GoalRun,
    *,
    model: str | None = None,
    user_id: str | None = None,
    max_rounds: int = MAX_ROUNDS,
    on_delta: OnDelta | None = None,
    repo: SessionRepository | None = None,
    parent_id: str | None = None,
) -> GoalStep:
    """跑一轮 goal_manager。**对话由调用方备好**（含这一轮新追加的那条记录）。

    这里只负责：把产品记录和当前 plan 投影发出去，并在本次调用内执行工具。
    工具对话不写回 ``run.conversation``；进度与终态由工具写进 `GoalContext`。

    ``on_delta`` 是流式出口（S118/S126）。**它和 director、writer 一样是个
    agent**，凭什么它的思考就该藏起来——而它恰恰是最需要看的那个：目标迟迟不
    达标时，唯一能回答"它到底在等什么"的就是这段。``None`` 走非流式。
    """
    ctx = GoalContext(goal=goal, run=run, repo=repo, parent_id=parent_id)
    registry = ToolRegistry.for_goal_manager()
    specs = registry.specs()
    messages = list(run.conversation)
    snapshot = plan_snapshot(run)
    if snapshot is not None:
        messages.append(snapshot)

    tracer.emit(
        TraceEventType.CONTEXT_ASSEMBLED,
        Actor.ORCHESTRATOR,
        "装配了 goal_manager 的上下文",
        {
            "prompt_messages": len(messages),
            "prompt_chars": sum(len(m.content or "") for m in messages),
        },
    )

    for _ in range(max_rounds):
        response = await gateway.complete(
            messages, tools=specs, call=call_of("goal_manager"), user_id=user_id, on_delta=on_delta
        )
        tracer.exchange(Actor.GOAL_MANAGER, messages, response, tools=specs)
        tracer.emit(
            TraceEventType.MODEL_RESPONSE,
            Actor.GOAL_MANAGER,
            "goal_manager 思考",
            {"tool_calls": [c.name for c in response.tool_calls]},
            usage=response.usage,
        )

        # **出口：这一次不再调用任何工具。** 那条纯文本不入对话——它是收口的
        # 信号，不是内容，留着只会在下一轮变成一句没头没尾的自言自语。
        if not response.tool_calls:
            break

        messages.append(response.as_assistant_message())
        for call in response.tool_calls:
            result = await registry.dispatch(ctx, call)
            kind = TraceEventType.TOOL_RESULT if result.is_error else TraceEventType.GOAL_EVALUATED
            tracer.emit(
                kind,
                Actor.GOAL_MANAGER,
                result.label or call.name,
                {"tool": call.name, "arguments": call.arguments, "error": result.is_error}
                | result.payload,
            )
            messages.append(Message(role=Role.TOOL, content=result.content, tool_call_id=call.id))

        if ctx.finished is not None:
            break

    outcome = (ctx.finished[0].value, ctx.finished[1]) if ctx.finished else None
    return GoalStep(next_input=ctx.next_input, finished=outcome)


__all__ = [
    "MAX_ROUNDS",
    "GoalStep",
    "advance_goal",
    "render_before",
    "plan_snapshot",
    "sync_conversation",
    "turn_record",
]
