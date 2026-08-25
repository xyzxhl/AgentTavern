"""一个 run 的编排。

时序见 docs/design/Architecture.md §4。**director → writer 是静态 workflow，交接
固定且单向**（S4）——编排层按固定顺序调用二者，中间没有回流、没有协商、
没有重试循环。

**run = node = 一份细纲 = 一份正文**，四者严格一一对应（S1）。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ..agents.director import DirectorFailed, DirectorResult, run_director
from ..agents.postrun import ask_segment_cut
from ..agents.replay import without_reasoning
from ..agents.state_manager import run_state_manager
from ..agents.summarizer import MAX_SEGMENT_RUNS, summarize_text
from ..agents.writer import run_writer
from ..llm import LLMGateway, Message, Usage
from ..models import (
    Actor,
    Node,
    RunDraft,
    RunStage,
    RunStatus,
    Session,
    StateDelta,
    TraceEventType,
    UserInput,
    new_id,
)
from ..prompting import enabled as agent_enabled
from ..prompting import selected, special_on
from ..storage import SessionRepository
from ..tools import AskUser, Inbox, RunContext, RunDelta, ToolRegistry
from ..tracing import OnEvent, Tracer
from .checks import sampling_flags


@dataclass
class RunOutcome:
    node: Node
    usage: Usage
    notes: list[str] = field(default_factory=list)
    """eval 采样标记。**从不影响运行时行为**（P12/P12）——只是把可疑样本挑出来送审。"""


async def execute_run(
    repo: SessionRepository,
    session: Session,
    gateway: LLMGateway,
    registry: ToolRegistry | None = None,
    *,
    user_input: UserInput | None = None,
    parent_id: str | None = None,
    ask_user: AskUser | None = None,
    on_trace: OnEvent | None = None,
    inbox: Inbox | None = None,
    developer: bool = False,
    persist_trace: bool = False,
    on_delta: RunDelta | None = None,
    run_id: str | None = None,
) -> RunOutcome:
    """``run_id`` 由调用方指定时，**这一轮开始之前发生的事也能挂在它名下**。

    唯一的用处是 goal 的开场（S100）：goal_manager 要先规划、写出这一轮的输入，
    而那几次调用理应属于**它开出来的那一轮**，不是上一轮。id 在这里生成的话，
    调用方在规划那一刻还拿不到它，只能另起一个没有节点的 id——那些记录就再也
    没地方显示了。
    """
    # **故事一开始就把初始状态定下来**（S67）：之后再改 worldbook 的初始状态
    # 条目，只影响新会话，不再回头改写既有的每一轮。第二轮起这里是空操作。
    repo.freeze_initial_state()

    run_id = run_id or new_id("run")
    # **开发者模式强制落盘**（S113）：记了不落盘，下次就查不了了，而查问题
    # 恰恰总是事后。它与服务级的 `Settings.trace`（`persist_trace`）是或的关系，
    # 不是覆盖。
    tracer = Tracer(
        repo,
        run_id,
        on_trace,
        persist=persist_trace or developer,
        developer=developer,
    )

    # 工具面按各 agent YAML 里的开关组装（S80）：关掉的工具**不注册**，
    # 模型看不到。worldinfo 全部常驻时也不注册其工具——没有东西可展开。
    if registry is None:
        registry = ToolRegistry.for_session(repo.worldbook())

    # **打断是挂起**（S48）：上一次被截断的那一段在这里被接回来。
    # 认领的条件是父节点一致——不一致说明用户已经切到别的分支去了，接不上（S42）。
    draft = repo.load_draft()
    if draft is not None and draft.parent_id != parent_id:
        repo.clear_draft()
        draft = None

    stage = _resume_point(draft, user_input)

    ctx = RunContext(
        run_id=run_id,
        repo=repo,
        session=session,
        gateway=gateway,
        parent_id=parent_id,
        ask_user=ask_user,
        inbox=inbox,
        on_delta=on_delta,
        # state 阶段被打断时，已经写下的那几笔是**已经完成的动作**，不必重来（S48）。
        staged_delta=draft.staged_delta if draft and stage is RunStage.STATE else StateDelta(),
    )

    tracer.emit(
        TraceEventType.RUN_STARTED,
        Actor.ORCHESTRATOR,
        "开始新的一轮" if draft is None else f"从「{stage.value}」接着跑",
        {
            "parent": parent_id,
            "resumed": draft is not None,
            "stage": stage.value,
        },
    )

    outline = draft.outline if draft is not None else ""
    prose = draft.prose if draft is not None else ""
    deviation = draft.deviation if draft is not None else None
    turn_messages: list[Message] = []
    loop_messages: list[Message] = draft.messages if draft is not None else []
    rounds = draft.rounds if draft is not None else 0
    # 被打断那一轮的输入是**唯一不可再生的东西**，续跑时它仍然是这一轮的输入；
    # 新说的话是「追加」，由 director 接在推演之后（S119），不顶替它。
    turn_input = (draft.user_input if draft is not None else None) or user_input
    # 从 writer / state 阶段续跑时**这一轮根本没有 director**，所以它是可空的。
    director: DirectorResult | None = None

    if stage is RunStage.DIRECTOR:
        if inbox is not None:
            inbox.accepting = True
        try:
            director = await run_director(
                ctx,
                registry,
                tracer,
                user_input,
                # **草稿一律接下去，不分阶段。** writer / state 阶段被打断而用户
                # 又说了话时，这一轮回 director 重来——但已经查过的资料、以及那次
                # `submit_outline` 连同它交出的细纲，都还在这几条消息里。
                # 于是「上一次写的是什么」不必再单开一节说明：**它就在对话里**。
                resume=draft,
            )
        except asyncio.CancelledError:
            # 细纲还没成型，所以不落节点——但**推演本身留着**，存成草稿等下一条
            # 输入接下去。草稿由 `run_director` 在同一处截断并写入，因为要在那里
            # 才知道该从哪里截：尾部那个没收到结果的工具调用必须一起丢掉。
            tracer.emit(
                TraceEventType.RUN_INTERRUPTED, Actor.ORCHESTRATOR, "推演阶段被打断，已挂起"
            )
            raise
        except DirectorFailed as exc:
            tracer.emit(
                TraceEventType.RUN_FAILED, Actor.ORCHESTRATOR, "推演失败", {"error": str(exc)}
            )
            raise
        finally:
            # 出了 tool loop 就没有操作边界了，信箱**必须关掉**——否则写正文时
            # 按下的停止会进一个没人读的信箱，看起来像按钮坏了。
            if inbox is not None:
                inbox.accepting = False

        outline, prose, deviation = director.outline, "", None
        turn_messages = director.turn_messages
        loop_messages, rounds = director.loop_messages, director.rounds

    if stage is not RunStage.STATE:
        try:
            written = await run_writer(ctx, tracer, outline)
            prose, deviation = written.prose, written.deviation
        except asyncio.CancelledError:
            # **半截正文不留**（S49）。留着看似省事，实则是
            # 把一个失败的阶段当成了产出：下一轮会接着那半句往下写，而用户按停止
            # 恰恰是因为不想要那半句。细纲留着，它是上游、已经完成了。
            _suspend(
                ctx,
                tracer,
                RunStage.WRITER,
                turn_input,
                messages=loop_messages,
                rounds=rounds,
                outline=outline,
            )
            raise

    # **状态维护后置**（S7）：读着刚写完的细纲和正文去记，而不是 director
    # 边推演边记。位置卡在 `save_node` **之前**——节点是原子的，细纲、正文、
    # 增量必须同时抵达（S42/S20）；放到落盘之后会出现一个"有正文没增量"的
    # 瞬时节点，那一刻沿路径重放状态的人（分支、`state_at`）会算错。
    # 代价是这一轮的结束晚一次模型调用，而用户此时已经看完正文了。
    if agent_enabled("state_manager"):
        try:
            await _maintain_state(ctx, tracer, outline, prose)
        except asyncio.CancelledError:
            # 这一段的上游全都完成了，所以草稿把它们一并带上：空输入时下一次
            # 只重跑这一段，正文不会被重写一遍。
            _suspend(
                ctx,
                tracer,
                RunStage.STATE,
                turn_input,
                messages=loop_messages,
                rounds=rounds,
                outline=outline,
                prose=prose,
                deviation=deviation,
            )
            raise

    notes = sampling_flags(
        delta=ctx.staged_delta,
        prose=prose,
        density=selected("writer", "density"),
        deviation=deviation,
        outline=outline,
        state_before=repo.state_at(parent_id).values,
    )
    for note in notes:
        tracer.emit(TraceEventType.CROSS_CHECK, Actor.ORCHESTRATOR, note)

    node = Node(
        id=run_id,
        parent_id=parent_id,
        status=RunStatus.COMPLETE,
        user_input=turn_input,
        outline=outline,
        prose=prose,
        deviation_report=deviation,
        state_delta=ctx.staged_delta,
        # 推理段只活到本 run 结束（S62）——一轮推理有上万字，存下来会让持续
        # 对话每两轮就撞预算，压缩变成每轮都发生，前缀反复失效。
        director_messages=without_reasoning(turn_messages),
    )
    repo.clear_draft()

    # 暂存的增量在此刻随节点一并落盘——细纲与增量同时抵达，节点是原子的（S42/S20）。
    repo.save_node(node)
    tracer.emit(
        TraceEventType.STATE_COMMITTED,
        Actor.ORCHESTRATOR,
        "本轮已提交",
        {"empty_delta": ctx.staged_delta.is_empty(), "resumed_from": stage.value},
    )

    # **重新载入再写，不要把手里这份整个盖回去。** `session` 是这一轮开始那一刻
    # 的快照，而元操作与生成是并行的（P10）：这期间用户可能设了目标、改了配置，
    # 而且**这一轮自己**在开头就调过 `freeze_initial_state()`——那也是一次
    # 读-改-写。整份写回等于把它们一起抹掉，而且一个字的报错都不会有。
    # （实测：初始状态的冻结在每一轮结束时都被这样撤销，S67 一直没真正生效。）
    latest = repo.load_session()
    latest.active_leaf_id = node.id
    repo.save_session(latest)
    session.active_leaf_id = node.id

    # **生成后追问**：本轮产出已定，这一步只影响以后怎么回看（S22）。
    # 问的是"这一轮是不是一段的结尾"——**判定在生成时，压缩在需要时**（S58）：
    # 分界往往要等下一轮才认得出来，而等它滑出窗口再问就已经看不见下一轮了。
    #
    # **必须问刚写完的 director 本人**：它手里有 state、有前情、知道故事要往哪走。
    # 所以从 writer / state 阶段续跑的那一轮不问——这一轮根本没有 director 在跑，
    # 另起一个只看得见几段细纲的冷调用，判据全靠猜（S22）。那一轮因此不带
    # 分段标记，代价只是这一处分界要等下一轮才被认出来。
    if special_on("director", "segment") and director is not None:
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
                # 模型指的那一条上，不一定是刚写完的这一轮（S58）。
                open_nodes[cut - 1].closes_segment = True
                repo.save_node(open_nodes[cut - 1])
        await close_segment(repo, gateway, node.id, tracer)

    tracer.emit(
        TraceEventType.RUN_FINISHED,
        Actor.ORCHESTRATOR,
        "本轮结束",
        {"rounds": director.rounds if director else 0, "prose_chars": len(prose)},
        usage=None,
    )

    return RunOutcome(node=node, usage=tracer.usage, notes=notes)


def _resume_point(draft: RunDraft | None, user_input: UserInput | None) -> RunStage:
    """这一轮从哪一段开始跑（S49）。

    **判据是生成内容的依赖关系**：state 依赖正文，正文依赖细纲，细纲依赖输入。
    用户又说了话，细纲就可能要改，那下游全都不作数——回 director 重来；用户
    什么也没说，就是「刚才那步没跑完，接着跑」，从被打断的那一段接。

    director 阶段不受这条影响：它本来就是**续跑**（S48），新输入追加进那段
    没跑完的推演里，已经查过的资料一并留着。
    """
    if draft is None:
        return RunStage.DIRECTOR
    if user_input is not None and user_input.text.strip():
        return RunStage.DIRECTOR
    return draft.stage


def _previous_attempt(draft: RunDraft | None) -> str:
    """回 director 时摆给它看的「上一次的推演」。

    只在 writer / state 阶段的草稿上有内容——那两种情况这一轮从头重跑，
    而旧细纲不该白写。director 阶段的草稿走续跑，它自己的消息里就有细纲。
    """
    if draft is None or draft.stage is RunStage.DIRECTOR or not draft.outline:
        return ""
    said = draft.user_input.text if draft.user_input else "（当时没有新的用户输入。）"
    return f"当时的用户输入：{said}\n\n你写下的细纲：\n{draft.outline}"


def _suspend(
    ctx: RunContext,
    tracer: Tracer,
    stage: RunStage,
    user_input: UserInput | None,
    *,
    messages: list[Message],
    rounds: int,
    outline: str,
    prose: str = "",
    deviation: str | None = None,
) -> None:
    """把这一段挂起：上游产物存进草稿，节点不落。

    **不落节点是关键**。留半截产出看似省事，实则会让下一轮接着那半句往下写，
    而用户按停止恰恰是因为不想要它（S49）。
    """
    ctx.repo.save_draft(
        RunDraft(
            parent_id=ctx.parent_id,
            stage=stage,
            user_input=user_input,
            # **这一轮的推演一并存着**：回 director 重来时它接得下去，
            # 那次 `submit_outline` 连同它交出的细纲就在里面。用户新说的话会被
            # 接在它之后——**位置就是它的含义**：模型读到的是"你交的这份细纲，
            # 用户看了之后又说了话"，而不是"这一轮的输入换了一条"（S119）。
            messages=messages,
            rounds=rounds,
            outline=outline,
            prose=prose,
            deviation=deviation,
            staged_delta=ctx.staged_delta,
        )
    )
    tracer.emit(
        TraceEventType.RUN_INTERRUPTED,
        Actor.ORCHESTRATOR,
        {RunStage.WRITER: "成文阶段被打断，已挂起", RunStage.STATE: "状态阶段被打断，已挂起"}[
            stage
        ],
        {"stage": stage.value},
    )


async def _maintain_state(ctx: RunContext, tracer: Tracer, outline: str, prose: str) -> None:
    """跑 state_manager，把增量写进 ``ctx.staged_delta``。

    **失败不阻塞已产出的内容**（同归段）：这一轮就当没有状态变化，节点照落。
    打断是另一回事——那是用户喊停，交给上层挂起（S49），所以 `CancelledError`
    原样往上抛。
    """
    try:
        await run_state_manager(
            ctx,
            ToolRegistry.for_state_manager(),
            tracer,
            outline=outline,
            prose=prose,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        tracer.emit(
            TraceEventType.CROSS_CHECK,
            Actor.ORCHESTRATOR,
            f"状态维护失败，本轮不记增量：{type(exc).__name__}",
            {"error": str(exc)},
        )


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
    那是生成时当场判的（S58）——分界往往要等下一轮才认得出来，
    而等它滑出窗口再问，问的人已经看不见下一轮了。

    三条支路：

    - **主路**：待归段的轮次里出现了打过标记的结尾，就压到那里。
    - **强制**：攒到 ``MAX_SEGMENT_RUNS`` 还没有结尾，全压掉止损。
    - **手动**：用户主动要求（``forced=True``）——用户说收了就是收了。

    **允许单轮成段。** 一次干脆的转场本来就是一段。

    **失败不影响已落盘的内容**：没有摘要只是回退到头部截断。
    """
    # **段一收就压**，不再等它滑出什么窗口——摘要生成与上下文替换是两件事：
    # 摘要在段收束时生成并缓存，替换在整份对话越界时一次性发生（S60）。
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
        # **一律压**（S25）。这里以前有条"已经比摘要还短就原样留下"的近路，
        # 阈值就是那个已经删掉的 `summary_max_chars`——它是同一个说了不算的数，
        # 换个名字留下来只是把问题藏起来。近路本来也几乎不触发：一段细纲动辄上千字。
        end.outline_summary = await summarize_text(
            gateway, joined, user_id=repo.session_id, tracer=tracer
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
    # **压缩比要落进事件里**（S25）：没有上限之后，"压到多少"是唯一还能看的东西，
    # 而它只有和"从多少压下来的"放在一起才有意义——单看 249 字看不出是 10× 还是 1.6×。
    tracer.emit(
        TraceEventType.CROSS_CHECK,
        Actor.ORCHESTRATOR,
        f"归段（{reason}）：{len(segment)} 轮 {len(joined)} 字压成 {len(end.outline_summary)} 字",
        {
            "end": end.id,
            "span": len(segment),
            "reason": reason,
            "chars_in": len(joined),
            "chars_out": len(end.outline_summary),
        },
    )
    return end
