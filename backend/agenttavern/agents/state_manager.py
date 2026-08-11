"""state_manager：状态维护者（D143）。

**它是第四个 agent，跑在 writer 之后。** 拿到这一轮的细纲和成文，把该被记住的
东西折成几个键值写进 state。以前这活在 director 手上，边推演边记。

## 为什么后置

- **记的人得看得见最终写出来的是什么。** 细纲常常留白——「他含糊地应了一句」，
  正文才落实成具体那句话。边推演边记的人只能记那个含糊的版本。
- **director 那一轮少了一件要惦记的事。** 它原先要在推演途中反复自问"这个要不要
  记"，而它当时还不知道最后会写成什么样；催促词里那条「先 state_write 再 submit」
  的分支也是这么来的，实测在 `pulp` 第 4 轮空转掉整场 84% 的 input token。

## 这削掉了 D54 的一半，必须说在明处

D54 是「director 从不读正文」，它换来 director 能覆盖五倍历史跨度。现在正文经由
这里转写进 state，state 下一轮进 director——**正文确实回流了**，但回流的是被压缩
和筛选过的几十个键值，不是原文。D54 买到的东西还在，说法要改。

它也不违反 D53（director → writer 单向、无回流）：那条讲的是**一个 run 内部**，
这里的回流跨了 run 边界，走 state 这个共享数据，性质和 history 一样。

## loop 的出口是"这一次不再调用工具"

没有 `submit` 之类的终止工具，这是有意的。director 需要一个是因为它要交付**产物**
（细纲），交付这个动作得有地方承接；这里的产物就是那几次写入本身，写完就没事了。
`MAX_ROUNDS` 只是保底。

**「没变化也要调用一次」那条契约（E0）随之作废**：它原先靠 `submit_outline` 退回
来强制，为的是区分"确实没变"和"忘了记"。director 会忘是因为它同时在推演；
这个 agent 唯一的任务就是记状态，空手回来就当没变化——为消这个歧义再花一轮
往返不值。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import LIMITS
from ..llm import Message, Role
from ..models import Actor, StateDelta, TraceEventType
from ..prompting import state_manager_prompt
from ..tools import RunContext, ToolRegistry
from ..tracing import Tracer

MAX_ROUNDS = LIMITS.state_manager.max_rounds
"""硬上限。正常一到两轮就完（写一次、然后空手回来收口）。"""

# 给几轮示范由 **`Capabilities.state_recent_runs` 决定（默认 3）**。
# **这是这个 agent 最大的一笔开销**——前几轮的正文占了上下文的大头，而且是滑动
# 窗口，每轮都在变，一点缓存前缀都吃不到。实测 `data` 场景取 3 时它占了全场
# 42% 的 input token。往下调省钱，代价是键名和值的形状更容易漂移。


@dataclass
class StateResult:
    delta: StateDelta
    rounds: int


async def run_state_manager(
    ctx: RunContext,
    registry: ToolRegistry,
    tracer: Tracer,
    *,
    outline: str,
    prose: str,
    max_rounds: int = MAX_ROUNDS,
) -> StateResult:
    """读这一轮的细纲和正文，把状态变化写进 ``ctx.staged_delta``。

    **只暂存不落盘**：增量随节点一并落地，细纲、正文、增量必须同时抵达，
    节点是原子的（D48/D56）。
    """
    path = ctx.repo.path_to(ctx.parent_id) if ctx.parent_id else []
    window = ctx.capabilities.state_recent_runs
    recent = path[-window:] if window else []
    messages = state_manager_prompt(
        # 本 run 开始时的状态。loop 中途写下的增量由 `state_query` 反映
        # （那个走 `current_state`），不必重新装配一次。
        ctx.base_state(),
        recent=recent,
        outline=outline,
        prose=prose,
        tools=registry.render(),
        capabilities=ctx.capabilities,
    ).messages

    tracer.emit(
        TraceEventType.CONTEXT_ASSEMBLED,
        Actor.ORCHESTRATOR,
        "装配了 state_manager 的上下文",
        {
            "prompt_messages": len(messages),
            "prompt_chars": sum(len(m.content or "") for m in messages),
            "recent_turns": len(recent),
        },
    )

    specs = registry.specs()
    rounds = 0

    for rounds in range(1, max_rounds + 1):  # noqa: B007 — 出口在循环体里，要留住轮数
        response = await ctx.gateway.complete(messages, tools=specs, user_id=ctx.session.id)

        tracer.emit(
            TraceEventType.MODEL_RESPONSE,
            Actor.STATE_MANAGER,
            f"state_manager 第 {rounds} 轮",
            {
                "finish_reason": response.finish_reason,
                "tool_calls": [c.name for c in response.tool_calls],
            },
            usage=response.usage,
        )

        # **出口：这一次不再调用任何工具。** 空手回来就是"没什么要改的"。
        if not response.tool_calls:
            break

        messages.append(response.as_assistant_message())
        for call in response.tool_calls:
            result = await registry.dispatch(ctx, call)
            tracer.emit(
                TraceEventType.TOOL_CALL if not result.is_error else TraceEventType.TOOL_RESULT,
                Actor.STATE_MANAGER,
                result.label or call.name,
                {"tool": call.name, "arguments": call.arguments, "error": result.is_error}
                | result.payload,
            )
            messages.append(Message(role=Role.TOOL, content=result.content, tool_call_id=call.id))

    tracer.emit(
        TraceEventType.STATE_COMMITTED,
        Actor.STATE_MANAGER,
        "本轮无状态变化" if ctx.staged_delta.is_empty() else "状态已更新",
        {"rounds": rounds, "writes": ctx.state_write_count},
    )
    return StateResult(delta=ctx.staged_delta, rounds=rounds)
