"""goal_manager：目标管理者（D28 / D141 / D148）。

**它是第三个 agent，不是 director 顺手做的一步。** 叫「管理者」而不是「判定者」，
因为判定只是它几件事里的一件——它还拆阶段、核对进度、替用户写下一条输入。

上下文只有目标、推进节奏、以及逐轮的剧情梗概。**它不读正文**（同 D54 的理由），
也不读世界设定和状态——它管的是"推到哪儿了"，不是"这一轮怎么写"。

## 一份跨 run 的长对话，不是三次干净调用

D141 那一版把三件事拆成三个 `task` 档、三次独立调用，每次重发全量剧情梗概。
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

from ..llm import LLMGateway, Message, Role
from ..models import (
    Actor,
    ExpansionDensity,
    Goal,
    GoalRun,
    InputSource,
    Node,
    SessionMeta,
    TraceEventType,
)
from ..prompting import assemble, titled
from ..tools import GoalContext, ToolRegistry
from ..tracing import Tracer
from .replay import interjections_of

MAX_ROUNDS = 4
"""一轮里最多几次模型往返。正常一到两次（调工具、然后空手回来收口）。"""


@dataclass
class GoalStep:
    """goal_manager 这一轮的产出。"""

    next_input: str | None = None
    """替用户写的下一条输入，喂给下一轮自动续跑。"""

    finished: tuple[str, str] | None = None
    """``(outcome, reason)``。非空即该交回控制权。"""


def render_before(nodes: list[Node]) -> str:
    """goal 设立**之前**那段剧情，给**细纲**（D150）。

    摘要策略和 director 一样（`summary_or_head`）：归过段的用那段摘要，
    没归段的用头部截断并写明还剩多少字（D144）。理由也一样——这段只回答
    "故事到这儿了"，细纲够用而且便宜；它还在静态前缀里，全量给一次就够。
    """
    if not nodes:
        return "（还没有已经写出来的剧情。）"
    return "\n\n".join(f"{i + 1}. {n.summary_or_head()}" for i, n in enumerate(nodes))


def _prefix(
    meta: SessionMeta, goal: Goal, density: ExpansionDensity, before: list[Node]
) -> list[Message]:
    """静态前缀：任务说明 + 用户扮演谁 + 目标 + 节奏 + goal 之前的剧情。

    **整个 goal 生命周期不变**——"之前"那段不会再长，goal 期间的轮次逐条追加在后面。

    **`user_character` 必须给**（D152）：它替用户写输入，不知道用户扮演谁就只能
    从种子输入的措辞里猜人称。猜对是运气——种子恰好是第一人称时它跟着写"我"，
    而那一句本可以是第三人称、也可能根本没有种子（goal 从会话一开始就在）。
    没设这个字段时整节不出现，它退回去猜，和以前一样。
    """
    return assemble(
        "goal_manager",
        contexts={
            "user_character": meta.user_character,
            "goal": goal.description,
            "history_outline": render_before(before),
        },
        options={"pace": density.value},
    ).messages


_SOURCE_LABEL = {
    InputSource.USER: "用户自己的输入",
    InputSource.SIMULATED: "我写的输入",
}


def turn_record(node: Node, index: int, interjections: list[str]) -> Message:
    """goal 期间某一轮的记录：**输入是谁给的** + **那一轮的正文**。

    给正文而不是细纲（D150）：goal 期间要判的是"这一步到底有没有发生"，而**细纲
    只说定了什么，正文才是最终写出来的东西**——留白处落成了什么、话说到什么份上，
    只有正文看得见。goal 设立之前那段仍然给细纲，它在静态前缀里（`render_before`）。

    来源标注是这条消息里最要紧的东西（D148）：剧情偏了，是 goal_manager 自己
    推错了，还是用户主动把剧情带走了？后者强得多——用户的意图优先于目标。
    不标注的话两种偏离长得一模一样，而应对完全不同。
    """
    said = node.user_input
    parts = []
    if said and said.text:
        parts.append(f"〔{_SOURCE_LABEL[said.source]}：{said.text}〕")
    else:
        parts.append("〔没有输入，直接推进〕")
    for word in interjections:
        parts.append(f"〔用户插话：{word}〕")
    # **正文没写成时不要回退到细纲**（D151）。回退看起来只是"降级用一份更粗的
    # 记录"，实际上把一个失败悄悄补齐了：那一轮什么都没写出来，而 goal_manager
    # 读到一段像模像样的剧情，会照着它判某个中间状态已经成立。
    # 说出来，让它当成一次没有产出的轮次处理。
    parts.append(node.prose or "（这一轮没有写出正文。）")
    return Message(role=Role.USER, content=titled(f"第 {index} 轮", "\n".join(parts)))


def plan_snapshot(run: GoalRun) -> Message | None:
    """恢复时注入的那条「当前计划与进度」。

    重建对话会丢掉 goal_manager 自己那些工具调用的历史（它当时怎么想的），
    但**计划和进度以投影的形式完整留着**——这正是"权威在投影不在对话"买到的。
    """
    if run.plan is None or not run.plan.subgoals:
        return None
    return Message(role=Role.USER, content=titled("当前的阶段计划与进度", run.plan.render()))


def sync_conversation(
    meta: SessionMeta,
    goal: Goal,
    run: GoalRun,
    path: list[Node],
    *,
    density: ExpansionDensity = ExpansionDensity.NORMAL,
) -> None:
    """把对话补到当前路径的末端。**能续就续，续不上就整份重建。**

    续得上的条件是：已有对话，且上次看到的那个节点**还在当前路径上**。
    不满足就重建——那正是暂停期间用户切了分支的情形，而「以最终选择的分支为准」
    只有重建做得到：增量追加撤不回来（D148）。

    重建丢掉的是 goal_manager 自己那些工具调用的历史，但计划与进度以投影的形式
    完整留着（`plan_snapshot`），**代价只在恢复那一刻付一次**——之后又是增量，
    静态前缀重新稳定下来。
    """
    # **路径切成两段**（D150）：goal 之前给细纲（进静态前缀），之后给正文（逐条追加）。
    ids = [n.id for n in path]
    cut = ids.index(run.started_at_leaf_id) + 1 if run.started_at_leaf_id in ids else 0
    before, during = path[:cut], path[cut:]

    during_ids = [n.id for n in during]
    resumable = bool(run.conversation) and run.seen_leaf_id in during_ids
    start = during_ids.index(run.seen_leaf_id) + 1 if resumable and run.seen_leaf_id else 0

    if not resumable:
        run.conversation = _prefix(meta, goal, density, before)
        snapshot = plan_snapshot(run)
        if snapshot is not None:
            run.conversation.append(snapshot)

    for index in range(start, len(during)):
        run.conversation.append(
            turn_record(during[index], cut + index + 1, interjections_of(during[index]))
        )
    run.seen_leaf_id = path[-1].id if path else None


async def advance_goal(
    gateway: LLMGateway,
    tracer: Tracer,
    goal: Goal,
    run: GoalRun,
    *,
    density: ExpansionDensity = ExpansionDensity.NORMAL,
    model: str | None = None,
    user_id: str | None = None,
    max_rounds: int = MAX_ROUNDS,
) -> GoalStep:
    """跑一轮 goal_manager。**对话由调用方备好**（含这一轮新追加的那条记录）。

    这里只负责：把对话发出去、执行工具、把新增的消息追加回 `run.conversation`。
    进度与终态由工具写进 `GoalContext`。
    """
    ctx = GoalContext(goal=goal, run=run)
    registry = ToolRegistry.for_goal_manager()
    specs = registry.specs()
    messages = list(run.conversation)

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
        response = await gateway.complete(messages, tools=specs, model=model, user_id=user_id)
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

    run.conversation = messages
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
