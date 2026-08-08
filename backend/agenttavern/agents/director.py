"""director：主 agent，tool loop。

产出一份**无文风约束的文章**（细纲）。它拿走全部推演与状态职责——writer 只是
从它身上拆下来的一个负担，只承担文风（D52）。

**唯一终止点是 `submit_outline`**（D53）。调用即结束 loop，编排层接手调 writer；
正文不回流——一旦回流，director 覆盖五倍历史跨度的能力就没了（D54）。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TypeVar

from ..llm import Message, Role, Usage
from ..models import (
    Actor,
    DirectorDraft,
    TraceEventType,
    UserInput,
    trim_dangling_tool_call,
)
from ..tools import RunContext, ToolOutcome, ToolRegistry
from ..tracing import Tracer
from .prompts import director_static_prefix, director_turn_prompt
from .replay import INTERJECTION_MARK, replay_messages, turn_messages

MAX_ROUNDS = 24
"""硬上限。撞上即强制收尾——tool 往返直接加在用户等待上（D7 已放弃流式）。"""

SOFT_ROUNDS = 16
"""软上限。到此提示模型收敛，但不强制。"""

T = TypeVar("T")


class DirectorFailed(RuntimeError):
    pass


@dataclass
class DirectorResult:
    outline: str
    usage: Usage
    rounds: int
    messages: list[Message] = field(default_factory=list)
    """loop 结束时的完整对话。

    留着是为了**生成后的追问**（D112）——比如"这一段收了没有"。那类问题必须问
    刚写完的 director 本人：它手里有 state、有前情、知道故事要往哪走。
    另起一个只看得见几段细纲的冷调用，判据全靠猜。
    """

    turn_messages: list[Message] = field(default_factory=list)
    """**这一轮新增的那几条**，落在节点上供以后重放（D114）。

    只存增量而不是整份对话：一条消息只属于一个节点，重放时沿路径拼接即可，
    分支天然安全（D48）。生成后追问的那一问一答**不进这里**——它是维护动作，
    留在上下文里只是噪声。
    """


async def run_director(
    ctx: RunContext,
    registry: ToolRegistry,
    tracer: Tracer,
    user_input: UserInput | None,
    *,
    resume: DirectorDraft | None = None,
    max_rounds: int = MAX_ROUNDS,
    soft_rounds: int = SOFT_ROUNDS,
) -> DirectorResult:
    session = ctx.session
    history = ctx.repo.path_to(ctx.parent_id) if ctx.parent_id else []

    # **对话跨轮持续**（D114）：system 只放不变的部分，之前几轮接在后面，
    # 这一轮只追加变化的部分。前缀因此稳定，缓存打得中。
    #
    # 之前几轮是**扁平化**过的（D121）：调了什么、返回了什么、细纲是什么，
    # 压成 assistant 文本，不带 `tool_calls`，也不带推理。所以 director 看得见
    # 自己上一轮**查过什么、写下了什么**，但看不见**为什么那么想**——推理只活到
    # 产出它的那个 run 结束（D120）。这是笔账：一轮推理一万五千字，而决定本身
    # 以细纲的形式完整留着。
    system = director_static_prefix(
        meta=session.meta,
        control=session.control,
        capabilities=session.capabilities,
        worldinfo=ctx.repo.worldbook(),
    )
    turn = director_turn_prompt(
        control=session.control,
        # 用 `current_state` 而不是 `base_state`：续跑时（D118）暂存区里已经有
        # 上次打断前写下的增量，注入的状态必须**把它算进去**——否则模型刚看完
        # 自己那条 `state_write` 的回执，紧接着就收到一份没有它的状态。
        # 没有草稿时暂存区是空的，两者等价。
        state=ctx.current_state(),
        user_text=user_input.text if user_input else None,
        capabilities=session.capabilities,
    )

    prior = replay_messages(history, readable=session.capabilities.history_search)

    # **打断是挂起**（D118）：上次被截断处之前的对话原样接回来，新输入接在它后面。
    # 草稿存的是"本轮新增的那几条"，历史仍然每次从路径重建——两者不重复。
    carried = list(resume.messages) if resume else []
    messages = [
        Message(role=Role.SYSTEM, content=system),
        *prior,
        *carried,
        Message(role=Role.USER, content=turn),
    ]
    # 续跑的那几条**属于同一个节点**：这一轮最终提交时，它们和后面新增的一起
    # 存进 `director_messages`，所以起点要划在草稿之前，不是新输入之前。
    turn_start = 1 + len(prior)

    tracer.emit(
        TraceEventType.CONTEXT_ASSEMBLED,
        Actor.ORCHESTRATOR,
        "装配了 director 的上下文",
        {
            "system_chars": len(system),
            "history_runs": len(history),
            "prior_messages": len(prior),
            "resumed_messages": len(carried),
        },
    )

    specs = registry.specs()
    # 打断过的轮次接着数，硬上限才仍然是硬上限——否则反复打断可以无限续下去。
    start_round = min(resume.rounds if resume else 0, max_rounds - 1)

    round_index = start_round

    try:
        for round_index in range(start_round, max_rounds):
            forced = round_index == max_rounds - 1
            if forced:
                messages.append(Message(role=Role.USER, content=_nudge(ctx, final=True)))
            elif round_index == soft_rounds:
                messages.append(
                    Message(role=Role.USER, content="调查得差不多了，请开始收敛并提交细纲。")
                )

            response = await _operation(
                ctx, ctx.gateway.complete(messages, tools=specs, user_id=session.id)
            )
            if response is None:
                # 掐在等模型回复的那一刻。这次调用什么都没产出，消息序还是齐的，
                # 直接到边界看有没有话要插。
                _boundary(ctx, messages, tracer, aborted=True)
                continue

            tracer.emit(
                TraceEventType.MODEL_RESPONSE,
                Actor.DIRECTOR,
                f"director 第 {round_index + 1} 轮思考",
                {
                    "finish_reason": response.finish_reason,
                    "tool_calls": [c.name for c in response.tool_calls],
                },
                usage=response.usage,
            )

            if not response.tool_calls:
                # 模型只回了文本。这在弱模型上很常见，推回去而不是当成产出——
                # 细纲必须经由 submit_outline 提交，否则契约（E0）就绕过去了。
                messages.append(response.as_assistant_message())
                messages.append(Message(role=Role.USER, content=_nudge(ctx)))
                _boundary(ctx, messages, tracer)
                continue

            messages.append(response.as_assistant_message())

            aborted = False
            for call in response.tool_calls:
                if aborted:
                    # 同一次响应里剩下的调用不再执行，但**每一个都得有结果**——
                    # 少一条，整份请求就不合法了。
                    messages.append(_ABORTED_RESULT(call.id))
                    continue

                result = await _operation(ctx, registry.dispatch(ctx, call))
                if result is None:
                    aborted = True
                    messages.append(_ABORTED_RESULT(call.id))
                    tracer.emit(
                        TraceEventType.RUN_INTERRUPTED,
                        Actor.DIRECTOR,
                        f"{call.name} 被打断",
                        {"tool": call.name},
                    )
                    continue

                tracer.emit(
                    TraceEventType.TOOL_CALL if not result.is_error else TraceEventType.TOOL_RESULT,
                    Actor.DIRECTOR,
                    result.label or call.name,
                    {"tool": call.name, "arguments": call.arguments, "error": result.is_error}
                    | result.payload,
                )
                messages.append(
                    Message(role=Role.TOOL, content=result.content, tool_call_id=call.id)
                )

                if result.outcome is ToolOutcome.TERMINATE:
                    assert ctx.outline is not None
                    tracer.emit(
                        TraceEventType.OUTLINE_SUBMITTED,
                        Actor.DIRECTOR,
                        "细纲完成",
                        {"chars": len(ctx.outline), "rounds": round_index + 1},
                    )
                    ctx.repo.clear_draft()
                    return DirectorResult(
                        outline=ctx.outline,
                        usage=tracer.usage,
                        rounds=round_index + 1,
                        messages=messages,
                        turn_messages=turn_messages(messages, turn_start),
                    )
                # 同一次响应里 submit_outline 之后的调用被忽略——它是终止点，
                # 之后不该再有动作。

            _boundary(ctx, messages, tracer, aborted=aborted)
    except asyncio.CancelledError:
        # **挂起，不作废**（D118）。已经查过的资料、已经暂存的增量、已经想到
        # 一半的推演都留着，等下一条输入把它接下去。
        _save_draft(ctx, messages, turn_start, round_index)
        raise

    raise DirectorFailed(f"director 在 {max_rounds} 轮内没有提交细纲")


def _ABORTED_RESULT(call_id: str) -> Message:  # noqa: N802 — 读作常量式的构造器
    return Message(
        role=Role.TOOL,
        content="（用户打断了这次调用，它没有执行完。）",
        tool_call_id=call_id,
    )


async def _operation(ctx: RunContext, coro: Awaitable[T]) -> T | None:
    """跑一次操作，同时盯着打断信号。被掐掉时返回 ``None``。

    **打断掐掉的是这一次操作，不是整个 run**（D119）。直接 cancel 当前 task
    会把整条 loop 一起带走，那就退化回"整轮作废"了——而用户按打断是为了
    **现在就插一句**，不是为了把已经查到的东西全扔掉。
    """
    if ctx.inbox is None:
        return await coro

    op: asyncio.Task[T] = asyncio.ensure_future(coro)
    signal = asyncio.ensure_future(ctx.inbox.interrupted.wait())
    try:
        await asyncio.wait({op, signal}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        signal.cancel()

    if op.done():
        return op.result()

    op.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await op
    return None


def _boundary(
    ctx: RunContext, messages: list[Message], tracer: Tracer, *, aborted: bool = False
) -> None:
    """操作边界：把等着的插话接进对话。

    ``aborted`` 表示刚才那次操作是被掐掉的。但**判据不能只看它**：打断信号和
    操作完成是在赛跑，操作先跑完时没有东西可掐，`aborted` 就是假的——而用户
    明明按了停止。所以这里补看一次信号本身，两条任一成立都算"用户喊了停"。

    喊了停而**没有话要插，就说明用户要的是"停下"**——抛出取消，让上层把这一轮
    挂起成草稿（D118）。有话要插则接着跑，这一轮不中断。
    """
    if ctx.inbox is None:
        return
    stopped = aborted or ctx.inbox.interrupted.is_set()
    texts = ctx.inbox.drain()
    for text in texts:
        messages.append(Message(role=Role.USER, content=f"{INTERJECTION_MARK}\n{text}"))
        tracer.emit(TraceEventType.USER_INTERJECTED, Actor.USER, text)
    if stopped and not texts:
        raise asyncio.CancelledError


def _save_draft(
    ctx: RunContext, messages: list[Message], turn_start: int, round_index: int
) -> None:
    kept = trim_dangling_tool_call(turn_messages(messages, turn_start))
    ctx.repo.save_draft(
        DirectorDraft(
            parent_id=ctx.parent_id,
            messages=kept,
            staged_delta=ctx.staged_delta,
            state_write_count=ctx.state_write_count,
            rounds=round_index,
        )
    )


def _nudge(ctx: RunContext, *, final: bool = False) -> str:
    """模型只回了文本时推回去的那句话。**只提还差的那一步。**

    以前它无条件写着「先 state_write，再 submit_outline」。模型照做，又写了一次
    `state_write`，然后又回到纯文本，于是再被推一次——实测 `pulp` 第 4 轮这样
    空转了 12 个来回，24 次模型调用、**69 万 input token，占整场的 84%**，
    最后靠硬上限才收住。

    **催促词把已经做完的那一步也算进去，它就成了一条把模型引回原地的指令。**

    这个判据 `_final_nudge` 一直是对的，只是当时没同步给循环里这一处——
    同一段逻辑抄了两份，只修好了一份。所以现在合成一个函数。
    """
    head = "这是最后一轮：请立即" if final else "请"
    if ctx.capabilities.state and ctx.state_write_count == 0:
        return f"{head}调用 state_write（没有变化就不带参数），然后 submit_outline。"
    return f"{head}调用 submit_outline 提交本轮细纲。"
