"""一个 run 的编排。

时序见 docs/MultiAgentSystem.md §10。**director → writer 是静态 workflow，交接
固定且单向**（D53）——编排层按固定顺序调用二者，中间没有回流、没有协商、
没有重试循环。

**run = node = 一份细纲 = 一份正文**，四者严格一一对应（D51）。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ..agents.director import DirectorFailed, run_director
from ..agents.postrun import ask_segment_cut
from ..agents.replay import without_reasoning
from ..agents.summary import MAX_SEGMENT_RUNS, SUMMARY_MAX_CHARS, summarize_text
from ..agents.writer import run_writer
from ..llm import LLMGateway, Usage
from ..models import (
    Actor,
    ControlOverride,
    Node,
    RunStatus,
    Session,
    StateDelta,
    SummarizeMode,
    TraceEventType,
    UserInput,
    new_id,
)
from ..storage import SessionRepository
from ..tools import AskUser, Inbox, RunContext, ToolRegistry
from ..tracing import OnEvent, Tracer
from .checks import sampling_flags


@dataclass
class RunOutcome:
    node: Node
    usage: Usage
    notes: list[str] = field(default_factory=list)
    """eval 采样标记。**从不影响运行时行为**（D43/D44）——只是把可疑样本挑出来送审。"""


async def execute_run(
    repo: SessionRepository,
    session: Session,
    gateway: LLMGateway,
    registry: ToolRegistry | None = None,
    *,
    user_input: UserInput | None = None,
    parent_id: str | None = None,
    ask_user: AskUser | None = None,
    control_override: ControlOverride | None = None,
    on_trace: OnEvent | None = None,
    inbox: Inbox | None = None,
) -> RunOutcome:
    run_id = new_id("run")
    tracer = Tracer(repo, run_id, on_trace, persist=session.capabilities.trace)

    control = session.control.merged_with(control_override)
    effective = session.model_copy(update={"control": control})

    # 工具面按能力开关组装：关闭的能力其工具**不注册**，模型看不到。
    # worldinfo 全部常驻时也不注册其工具——没有东西可展开。
    if registry is None:
        registry = ToolRegistry.for_session(session.capabilities, repo.worldbook())

    # **打断是挂起**（D118）：上一次被截断的推演在这里被接回来。
    # 认领的条件是父节点一致——不一致说明用户已经切到别的分支去了，接不上（D48）。
    draft = repo.load_draft()
    if draft is not None and draft.parent_id != parent_id:
        repo.clear_draft()
        draft = None

    ctx = RunContext(
        run_id=run_id,
        repo=repo,
        session=effective,
        gateway=gateway,
        parent_id=parent_id,
        ask_user=ask_user,
        inbox=inbox,
        # 已经写进暂存区的增量是**已经完成的动作**，和已经拿到的工具结果没有区别。
        staged_delta=draft.staged_delta if draft else StateDelta(),
        state_write_count=draft.state_write_count if draft else 0,
    )

    tracer.emit(
        TraceEventType.RUN_STARTED,
        Actor.ORCHESTRATOR,
        "开始新的一轮",
        {
            "parent": parent_id,
            "control": control.model_dump(mode="json"),
            "resumed": draft is not None,
        },
    )

    if inbox is not None:
        inbox.accepting = True
    try:
        director = await run_director(ctx, registry, tracer, user_input, resume=draft)
    except asyncio.CancelledError:
        # 细纲还没成型，所以不落节点——但**推演本身留着**，存成草稿等下一条输入
        # 接下去（D118）。草稿由 `run_director` 在同一处截断并写入，因为要在那里
        # 才知道该从哪里截：尾部那个没收到结果的工具调用必须一起丢掉。
        tracer.emit(
            TraceEventType.RUN_INTERRUPTED,
            Actor.ORCHESTRATOR,
            "推演阶段被打断，已挂起等待续跑",
        )
        raise
    except DirectorFailed as exc:
        tracer.emit(TraceEventType.RUN_FAILED, Actor.ORCHESTRATOR, "推演失败", {"error": str(exc)})
        raise
    finally:
        # 出了 tool loop 就没有操作边界了，信箱**必须关掉**——否则写正文时
        # 按下的停止会进一个没人读的信箱，看起来像按钮坏了。
        if inbox is not None:
            inbox.accepting = False

    status = RunStatus.COMPLETE
    prose = ""
    deviation: str | None = None

    try:
        written = await run_writer(ctx, tracer, director.outline)
        prose, deviation = written.prose, written.deviation
    except asyncio.CancelledError:
        # writer 阶段被打断：**保留已生成的正文片段**，state 增量照常提交——
        # 那段剧情确实发生了。「话说了一半被打断」在 RP 里是真实感的来源，
        # 不是需要清理的垃圾（D60）。
        status = RunStatus.PARTIAL
        tracer.emit(
            TraceEventType.RUN_INTERRUPTED,
            Actor.ORCHESTRATOR,
            "成文阶段被打断，保留已产出的部分",
        )

    notes = sampling_flags(
        delta=ctx.staged_delta,
        prose=prose,
        density=control.expansion_density,
        deviation=deviation,
        outline=director.outline,
        state_before=repo.state_at(parent_id).values,
    )
    for note in notes:
        tracer.emit(TraceEventType.CROSS_CHECK, Actor.ORCHESTRATOR, note)

    node = Node(
        id=run_id,
        parent_id=parent_id,
        status=status,
        user_input=user_input,
        control=control,
        outline=director.outline,
        prose=prose,
        deviation_report=deviation,
        state_delta=ctx.staged_delta,
        # 推理段只活到本 run 结束（D120）——一轮推理有上万字，存下来会让持续
        # 对话每两轮就撞预算，压缩变成每轮都发生，前缀反复失效。
        director_messages=without_reasoning(director.turn_messages),
    )

    # 暂存的增量在此刻随节点一并落盘——细纲与增量同时抵达，节点是原子的（D48/D56）。
    repo.save_node(node)
    tracer.emit(
        TraceEventType.STATE_COMMITTED,
        Actor.ORCHESTRATOR,
        "本轮已提交",
        {"empty_delta": ctx.staged_delta.is_empty(), "status": status.value},
    )

    session.active_leaf_id = node.id
    repo.save_session(session)

    # **生成后追问**：本轮产出已定，这一步只影响以后怎么回看（D112）。
    # 问的是"这一轮是不是一段的结尾"——**判定在生成时，压缩在需要时**（D113）：
    # 分界往往要等下一轮才认得出来，而等它滑出窗口再问就已经看不见下一轮了。
    # 被打断的一轮不追问：它连正文都没写完，谈不上"是不是一段的结尾"。
    if session.capabilities.summarize is SummarizeMode.AUTO and status is RunStatus.COMPLETE:
        with contextlib.suppress(Exception):
            open_nodes = _open_segment(repo, node.id)
            cut = await ask_segment_cut(
                gateway,
                director.messages,
                [n.outline for n in open_nodes],
                tracer,
                user_id=session.id,
            )
            if cut > 0:
                # **可以回溯**：分界常常要等下一轮才看得出来，所以标记打在
                # 模型指的那一条上，不一定是刚写完的这一轮（D116）。
                open_nodes[cut - 1].closes_segment = True
                repo.save_node(open_nodes[cut - 1])
        await close_segment(repo, gateway, node.id, tracer)

    tracer.emit(
        TraceEventType.RUN_FINISHED,
        Actor.ORCHESTRATOR,
        "本轮结束",
        {"rounds": director.rounds, "prose_chars": len(prose)},
        usage=None,
    )

    return RunOutcome(node=node, usage=tracer.usage, notes=notes)


def _open_segment(repo: SessionRepository, leaf_id: str) -> list[Node]:
    """当前这一段还没归段的那几轮，含刚写完的这一轮。

    **算的是"上一个结尾之后有哪些轮"**——从叶子往回走，碰到已归段或已标记为
    结尾的节点就停。
    """
    path = repo.path_to(leaf_id)
    out: list[Node] = []
    for node in reversed(path):
        if node.id != leaf_id and (node.closes_segment or node.outline_summary):
            break
        if node.outline:
            out.append(node)
    out.reverse()
    return out


async def close_segment(
    repo: SessionRepository,
    gateway: LLMGateway,
    leaf_id: str,
    tracer: Tracer,
    *,
    forced: bool = False,
) -> Node | None:
    """把已经滑出全文窗口、又还没归段的轮次压成一段；没归成返回 ``None``。

    **只负责压缩，不负责判定。** 哪一轮是结尾由 `Node.closes_segment` 记着，
    那是生成时当场判的（D113）——分界往往要等下一轮才认得出来，
    而等它滑出窗口再问，问的人已经看不见下一轮了。

    三条支路：

    - **主路**：待归段的轮次里出现了打过标记的结尾，就压到那里。
    - **强制**：攒到 ``MAX_SEGMENT_RUNS`` 还没有结尾，全压掉止损。
    - **手动**：用户主动要求（``forced=True``）——用户说收了就是收了。

    **允许单轮成段。** 一次干脆的转场本来就是一段。

    **失败不影响已落盘的内容**：没有摘要只是回退到头部截断。
    """
    # **段一收就压**，不再等它滑出什么窗口——摘要生成与上下文替换是两件事：
    # 摘要在段收束时生成并缓存，替换在整份对话越界时一次性发生（D114）。
    # 早先按 `RECENT_OUTLINES` 卡住不压，会让刚收的段落在越界时**还没有摘要可用**，
    # 于是压不掉。
    path = repo.path_to(leaf_id)

    # 已归段的节点连同它覆盖的那几轮一起跳过——`segment_span` 是"末节点 + 跨度"，
    # 从后往前扫才能正确跳过整段。
    pending: list[Node] = []
    index = len(path) - 1
    while index >= 0:
        node = path[index]
        if node.outline_summary:
            break
        if node.outline:
            pending.append(node)
        index -= 1
    pending.reverse()

    if not pending:
        return None

    hit_cap = len(pending) >= MAX_SEGMENT_RUNS
    if forced or hit_cap:
        segment = pending
        reason = "到上限强制" if hit_cap else "手动"
    else:
        # 压到**最靠后的那个结尾**为止；后面没打标记的留着，等它们自己的结尾。
        marked = [i for i, n in enumerate(pending) if n.closes_segment]
        if not marked:
            return None
        segment = pending[: marked[-1] + 1]
        reason = "生成时判定的结尾"

    try:
        joined = "\n\n".join(n.outline for n in segment)
        end = segment[-1]
        # 已经比摘要还短就原样留下——压它只会白白引入损失，还要多花一次调用。
        end.outline_summary = (
            joined
            if len(joined) <= SUMMARY_MAX_CHARS
            else await summarize_text(gateway, joined, user_id=repo.session_id)
        )
        end.segment_span = len(segment)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        tracer.emit(
            TraceEventType.CROSS_CHECK,
            Actor.ORCHESTRATOR,
            f"归段失败，回退到头部截断：{type(exc).__name__}",
        )
        return None

    repo.save_node(end)
    tracer.emit(
        TraceEventType.CROSS_CHECK,
        Actor.ORCHESTRATOR,
        f"归段（{reason}）：{len(segment)} 轮压成 {len(end.outline_summary)} 字",
        {"end": end.id, "span": len(segment), "reason": reason},
    )
    return end
