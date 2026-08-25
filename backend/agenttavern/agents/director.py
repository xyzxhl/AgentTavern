"""director：主 agent，tool loop。

产出一份**无文风约束的文章**（细纲）。它拿走全部推演与状态职责——writer 只是
从它身上拆下来的一个负担，只承担文风（S3）。

**唯一终止点是 `submit_outline`**（S4）。调用即结束 loop，编排层接手调 writer。

**它读状态，但不写状态**（S7）：状态维护后置成了 writer 之后的 state_manager。
于是正文经那一步转写进 state，state 下一轮回到这里——**S5 因此被削了一半**：
回流的是被压缩筛选过的几十个键值，不是原文，五倍历史跨度还在，但不能再说
「正文完全不回流」了。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TypeVar

from ..config import LIMITS
from ..llm import Message, Role, Usage
from ..models import (
    Actor,
    RunDraft,
    RunStage,
    TraceEventType,
    UserInput,
    trim_dangling_tool_call,
)
from ..prompting import director_prompt, special_text, tool_enabled
from ..tools import RunContext, ToolOutcome, ToolRegistry
from ..tracing import Tracer
from .replay import append_message, replay_messages, turn_messages

MAX_ROUNDS = LIMITS.director.max_rounds
"""硬上限。撞上即强制收尾——tool 往返直接加在用户等待上（P21 已放弃流式）。"""

SOFT_ROUNDS = LIMITS.director.soft_rounds
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

    留着是为了**生成后的追问**（S22）——比如"这一段收了没有"。那类问题必须问
    刚写完的 director 本人：它手里有 state、有前情、知道故事要往哪走。
    另起一个只看得见几段细纲的冷调用，判据全靠猜。
    """

    loop_messages: list[Message] = field(default_factory=list)
    """loop 里产出的那几条：模型的回复、工具调用与结果，**不含提示词各节**。

    writer / state 阶段被打断时草稿存的就是它（S49）——那两段之后回 director
    重来，接回去的应该只有推演本身；提示词各节每次重新装配，接回去只会让旧的
    state 快照和新的并排出现。
    """

    turn_messages: list[Message] = field(default_factory=list)
    """**这一轮新增的那几条**，落在节点上供以后重放（S60）。

    只存增量而不是整份对话：一条消息只属于一个节点，重放时沿路径拼接即可，
    分支天然安全（S42）。生成后追问的那一问一答**不进这里**——它是维护动作，
    留在上下文里只是噪声。
    """


async def run_director(
    ctx: RunContext,
    registry: ToolRegistry,
    tracer: Tracer,
    user_input: UserInput | None,
    *,
    resume: RunDraft | None = None,
    max_rounds: int = MAX_ROUNDS,
    soft_rounds: int = SOFT_ROUNDS,
) -> DirectorResult:
    session = ctx.session
    history = ctx.repo.path_to(ctx.parent_id) if ctx.parent_id else []

    prior = replay_messages(history, readable=tool_enabled("director", "history_search"))

    # **打断是挂起**（S48 / S49）：上次被截断处之前的推演原样接回来。
    #
    # **接在整份提示词之后，而不是塞进前情那一格。** 草稿里存的是 loop 里产出的
    # 那几条（模型的回复、工具调用与结果），**不含这一轮的提示词各节**——那几节
    # 每次都重新装配，塞进草稿只会让旧的 state 快照和新的并排出现，还顶着
    # "写在前面"的权威姿态跟新值打架（S61 已经在跨轮重放那侧记过这条）。
    #
    # 顺序因此是对的：前情 → 这一轮的要求与输入 → 上次想到一半的推演 → 追加的话。
    carried = list(resume.messages) if resume else []

    # **续跑时「用户输入」那一节仍是被打断那一轮的原输入。** 新说的话不进那一节，
    # 它作为「用户追加输入」接在推演之后——**位置就是它的含义**：排在
    # `submit_outline` 之后，模型才读得出"你交的这份细纲，用户看了之后又说了话"，
    # 而不是"这一轮的输入换了一条"（S119）。
    said = user_input.text if user_input else None
    appended = ""
    if resume is not None and user_input is not None:
        appended, said = user_input.text, (resume.user_input.text if resume.user_input else None)

    # **对话跨轮持续**（S60）：前情之前的部分跨轮不变，之前几轮接在后面，
    # 这一轮只追加变化的部分。前缀因此稳定，缓存打得中。
    #
    # 之前几轮是**扁平化**过的（S61）：调了什么、返回了什么、细纲是什么，
    # 压成 assistant 文本，不带 `tool_calls`，也不带推理。所以 director 看得见
    # 自己上一轮**查过什么、写下了什么**，但看不见**为什么那么想**——推理只活到
    # 产出它的那个 run 结束（S62）。这是笔账：一轮推理一万五千字，而决定本身
    # 以细纲的形式完整留着。
    prompt = director_prompt(
        # **本 run 开始时的状态。** 这里原先写的是 `current_state`（叠加暂存增量），
        # 为的是续跑时模型别看见一份不含自己刚写下那笔的状态。director 不再写
        # 状态之后暂存区在这一阶段恒为空，两者等价，写 `base_state` 更诚实（S7）。
        state=ctx.base_state(),
        user_text=said,
        history=prior,
        worldinfo=ctx.repo.worldbook(),
        # 目标是**这局故事**的，不跟着世界走（S63）。
        goal=session.meta.goal,
    )
    messages = prompt.messages

    # 续跑的那几条**属于同一个节点**：这一轮最终提交时，它们和后面新增的一起
    # 存进 `director_messages`，所以起点要划在草稿之前，不是新输入之前。
    turn_start = prompt.starts["history"] + len(prior)

    # 草稿里的推演接上，用户新说的话再接在它之后。
    loop_start = len(messages)
    messages.extend(carried)
    if appended:
        messages.append(append_message(appended))

    tracer.emit(
        TraceEventType.CONTEXT_ASSEMBLED,
        Actor.ORCHESTRATOR,
        "装配了 director 的上下文",
        {
            "prompt_messages": len(messages),
            "prompt_chars": sum(len(m.content or "") for m in messages),
            "history_runs": len(history),
            "prior_messages": len(prior),
            "resumed_messages": len(carried),
            "appended": bool(appended),
        },
    )

    specs = registry.specs()
    # **只有真正的续跑才接着数轮数。**
    #
    # 硬上限防的是「反复打断、把同一段推演无限续下去」，所以 director 阶段被打断
    # 再接回来时，用掉的额度不退（S48）。但 writer / state 阶段被打断后回到这里
    # 是**重新交一份细纲**——那是一次新的推演，不是把上次那段接着往下跑。
    # 算在它头上的话，在正文阶段反复打断几次就能把这 24 轮提前吃光。
    continuing = resume is not None and resume.stage is RunStage.DIRECTOR
    start_round = min(resume.rounds, max_rounds - 1) if continuing and resume else 0

    round_index = start_round

    try:
        for round_index in range(start_round, max_rounds):
            forced = round_index == max_rounds - 1
            if forced:
                _push(messages, _nudge(ctx, final=True))
            elif round_index == soft_rounds:
                _push(messages, special_text("director", "converge"))

            response = await _operation(
                ctx,
                ctx.gateway.complete(
                    messages,
                    tools=specs,
                    user_id=session.id,
                    # 推演阶段流的是**思考段**——正文还没开始写，这里的 `content`
                    # 多半只是几句自言自语，而思考段才是"它在想什么"。
                    on_delta=ctx.stream_to(Actor.DIRECTOR),
                ),
            )
            if response is None:
                # 掐在等模型回复的那一刻。这次调用什么都没产出，消息序还是齐的，
                # 直接到边界看有没有话要插。
                _boundary(ctx, messages, tracer, aborted=True)
                continue

            tracer.exchange(Actor.DIRECTOR, messages, response, tools=specs)
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
                _push(messages, _nudge(ctx))
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
                        loop_messages=messages[loop_start:],
                        turn_messages=turn_messages(messages, turn_start),
                    )
                # 同一次响应里 submit_outline 之后的调用被忽略——它是终止点，
                # 之后不该再有动作。

            _boundary(ctx, messages, tracer, aborted=aborted)
    except asyncio.CancelledError:
        # **挂起，不作废**（S48）。已经查过的资料、已经暂存的增量、已经想到
        # 一半的推演都留着，等下一条输入把它接下去。
        _save_draft(ctx, messages[loop_start:], round_index, said, user_input)
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

    **打断掐掉的是这一次操作，不是整个 run**（S50）。直接 cancel 当前 task
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
    挂起成草稿（S48）。有话要插则接着跑，这一轮不中断。
    """
    if ctx.inbox is None:
        return
    stopped = aborted or ctx.inbox.interrupted.is_set()
    texts = ctx.inbox.drain()
    for said in texts:
        # **说明之后再追加**：裸着塞进去，模型读到的是一条突然冒出来的用户发言，
        # 看不出它和这一轮已经进行到一半的推演是什么关系。措辞归
        # `director_append.yaml`，不写在这里（同 S10）。
        messages.append(append_message(said))
        tracer.emit(TraceEventType.USER_INTERJECTED, Actor.USER, said)
    if stopped and not texts:
        raise asyncio.CancelledError


def _save_draft(
    ctx: RunContext,
    loop: list[Message],
    round_index: int,
    said: str | None,
    user_input: UserInput | None,
) -> None:
    """挂起这一段推演。**草稿由这里写，不由编排层写**——只有这里知道该从哪儿截：
    尾部那个没收到结果的工具调用必须一起丢掉，否则续跑时端点会拒绝整份请求。

    存的是 loop 那一段，不含提示词各节（理由见上面装配处）。`user_input` 记的是
    **被打断那一轮的原输入**：续跑时它顶回「用户输入」那一节，而下一条新说的话
    另接在推演之后。
    """
    ctx.repo.save_draft(
        RunDraft(
            parent_id=ctx.parent_id,
            stage=RunStage.DIRECTOR,
            user_input=UserInput(text=said) if said else user_input,
            messages=trim_dangling_tool_call(loop),
            rounds=round_index,
        )
    )


def _push(messages: list[Message], body: str) -> None:
    """推一句话给模型。**空串就什么也不推**——那是外部把这一档关掉了（S10）。"""
    if body.strip():
        messages.append(Message(role=Role.USER, content=body))


def _nudge(ctx: RunContext, *, final: bool = False) -> str:
    """模型只回了文本时推回去的那句话。

    措辞在 `director.yaml` 的 `specials.nudge` / `specials.nudge_final` 里（R31）。
    **关掉它的开关就在那两条自己身上**（`enabled`，S80）——那从来不是一个独立
    的能力，是这条提示词发不发。关掉时返回空串，于是根本不推：模型空手回来，
    这一轮就算完。

    **最后一轮单独一条，不是同一条的第二档。** 「这是最后一轮」和「请提交」是两句
    不同的话，各自可编；做成一个 `choice` 的话「选哪一档」由代码按轮数决定，
    而 `selected` 的语义是用户的选择（R31）。

    **这里原本有个分支**：state_write 还没做就催「先 state_write 再 submit」。
    判据是实测来的——催促词以前无条件写着两步，模型照做又写一次 `state_write`，
    然后回到纯文本，于是再被推一次；`pulp` 第 4 轮这样空转了 12 个来回、
    **69 万 input token，占整场的 84%**。S7 把状态维护整个搬走之后，director
    这一轮只剩一件事要做，分支自己没有了。
    """
    return special_text("director", "nudge_final" if final else "nudge")
