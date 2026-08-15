"""SessionRunner：一个 session 一个常驻循环。

三条硬约束：

- **队列 FIFO，每条输入独立成一个 run，不打包**（D26）——打包会跳过中间的世界响应。
- **用户输入优先于自主推进**（D27）——队列非空时不做 goal 驱动的自动续跑，
  否则排队的输入被消费时已经过时。
- **`ask_question` 不设超时**（D66）——等待期间逻辑上什么都没发生，
  实现上就是 ``await`` 一个 future，loop 不需要任何特殊的挂起状态机。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast

from ..agents import GoalStep, advance_goal, sync_conversation
from ..llm import LLMGateway, Usage
from ..models import (
    Actor,
    ControlOverride,
    Goal,
    GoalRun,
    InputSource,
    Session,
    TraceEvent,
    TraceEventType,
    UserInput,
    new_id,
)
from ..orchestration import execute_run
from ..storage import SessionRepository
from ..tools import Inbox
from ..tracing import Tracer
from .protocol import (
    Delta,
    GoalFinished,
    GoalPaused,
    GoalProgress,
    Question,
    QueueStatus,
    RunFailed,
    RunFinished,
    RunInterrupted,
    RunStarted,
    ServerMessage,
    StateSnapshot,
    Trace,
    UsageUpdate,
)

Broadcast = Callable[[ServerMessage], Awaitable[None]]

DeltaKind = Literal["content", "reasoning"]
_DELTA_KINDS = ("content", "reasoning")


@dataclass
class _Queued:
    text: str
    control_override: ControlOverride | None = None
    regenerate: bool = False
    """重生成：挂在**当前叶的父**下，成为它的兄弟（D46）；普通输入挂在当前叶下。

    **这里不再带父节点 id**（D147）。以前带，是因为可以重生成任意历史节点，而那
    时不能靠改 `session.active_leaf_id` 传参——共享状态，若此刻有 run 在飞，
    它结束时会把活跃叶覆盖成自己。现在分支只能在当前节点上开，"父节点是谁"就是
    出队那一刻的活跃叶推出来的，没有第二个来源可对不上。

    **排队期间又落了一轮的话，重 roll 的就是新那一轮**——那是对的：约束本身就是
    「只能重 roll 当前节点」，而那时的当前节点已经是它了。入队时会先校验一次
    （见 `app.py` 的 `Regenerate` 分支），所以这种情况只发生在用户手速比 run 还快时。
    """


class SessionRunner:
    def __init__(
        self,
        repo: SessionRepository,
        gateway: LLMGateway,
        broadcast: Broadcast,
        *,
        developer: bool = False,
    ) -> None:
        self._repo = repo
        self._gateway = gateway
        self._broadcast = broadcast
        self._developer = developer
        """**服务级开发者模式**（D156，`Settings.developer`）：额外记录每次模型
        往返的完整输入输出，并强制 trace 落盘。session 级的 `capabilities.trace`
        管的是另一件事——那条决定要不要留 trace 文件，这条决定 trace 里有没有原文。
        """

        self._queue: asyncio.Queue[_Queued | None] = asyncio.Queue()
        """``None`` 是**唤醒信号**（`nudge`），不是一条输入。见那里的说明。"""
        self._pending: list[_Queued] = []
        self._questions: dict[str, asyncio.Future[str]] = {}
        self._current: asyncio.Task[None] | None = None
        self._inbox: Inbox | None = None
        """当前 run 的插话信箱。**一个 run 一个**——上一轮没人认领的话不该漏到下一轮。"""

        self._loop_task: asyncio.Task[None] | None = None

        self._run_id: str = ""
        """当前这一轮的 id。**流式分片要带上它**——前端靠它把字归到正确的一轮
        名下（D160）。id 是在 `execute_run` 内部生成的，外面只能从 trace 里接住。
        """

        # 所有出站消息走**同一条队列**，由单个 sender 顺序送出。
        # trace 是从同步回调里产生的，若各自 create_task 发送就会乱序——
        # UI 上会看到「本轮结束」排在「开始新的一轮」前面。
        self._out: asyncio.Queue[ServerMessage] = asyncio.Queue()
        self._sender_task: asyncio.Task[None] | None = None

        # goal 的预算计数**已经落盘**了（`GoalRun.spent_*`，D148）。它以前是这里的
        # 两个实例字段——进程重启就归零，于是 D28 那个用来兜住自主运行成本的上限
        # 实际上从来没生效过。

        self._goal_input: str | None = None
        """goal_manager 替用户写的下一条输入（D141）。

        自动续跑时拿它当 `user_input`——**这就是「模拟用户输入」落地的地方**。
        它不是持久状态：进程内的一次传递，用完即清。
        """

        self._goal_opening = False
        """目标刚被设上 / 恢复，**还没让 goal_manager 规划过**（D168）。

        置位的是 `nudge()`，消费的是下一次 goal 驱动的 run——它会在 director 之前
        先跑一次 goal_manager，拿到开场那条输入。判定跑过之后就清掉：那时它刚写
        好下一条输入，不需要再开场一次。
        """

        self._autocontinue_armed = False
        """自动续跑是**一次 run 的后果**，不是冷启动就发生的事。

        D28 的原话是「正常一轮生成结束后检查是否达标，未达标则开启下一轮」——
        所以它只在一次 run 成功结束后才被打开。否则带 goal 的 session 一连上
        就会自己开始生成，用户还没说话。
        """

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is None:
            self._sender_task = asyncio.create_task(self._sender())
            self._loop_task = asyncio.create_task(self._loop())

    async def aclose(self) -> None:
        if self._current is not None:
            self._current.cancel()
        for task in (self._loop_task, self._sender_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # -- 外部动作 -----------------------------------------------------------

    async def submit(
        self,
        text: str,
        override: ControlOverride | None = None,
        *,
        regenerate: bool = False,
    ) -> None:
        item = _Queued(text=text, control_override=override, regenerate=regenerate)
        self._pending.append(item)
        await self._queue.put(item)
        self._emit(QueueStatus(pending=[q.text for q in self._pending]))

    def nudge(self) -> None:
        """用户刚对 goal 做了动作（设立 / 恢复 / 加预算）：**打开自动续跑并唤醒循环**。

        两件事缺一不可。

        `_autocontinue_armed` 默认关着，挡的是「带 goal 的 session 一连上就自己
        开始生成」——**连接不是指令**。但设目标是指令：用户按下去的意思就是
        "去做"。所以一个还没写过任何一轮的会话，设完目标就该动起来，而不是
        举着「目标推进中」一动不动，等用户随便打句话把它推起来。

        唤醒是因为循环这时多半停在 `self._queue.get()` 上——`_should_autocontinue()`
        只在出队之后才被问到，光把开关拨开没人会去看。放一个 `None` 进去把它叫醒，
        出队那一刻**再确认一次**：中间可能已经被放弃或暂停了。

        有 run 在跑时它同样安全：`None` 在队列里等着，那一轮结束后照常被取出，
        而 `_check_goal` 反正也会重新打开自动续跑。

        **`_goal_opening` 一并置位**（D168）：紧接着那一轮要先让 goal_manager
        规划、写出开场的输入，否则第一轮是空手推的——而 director 从来不读 goal，
        那一轮完全不知道有目标这回事。
        """
        self._autocontinue_armed = True
        self._goal_opening = True
        self._queue.put_nowait(None)

    def answer(self, question_id: str, selected: str) -> bool:
        future = self._questions.pop(question_id, None)
        if future is None or future.done():
            return False
        future.set_result(selected)
        return True

    def interject(self, text: str, *, interrupt: bool) -> bool:
        """把一句话插进**正在跑的** run（F3b / D119）。没有 run 在跑则返回 False。

        插入点在**操作边界**上——一次模型调用或一次工具调用之间，不是等这一轮
        写完。两个动作的差别只有一处：``interrupt=True`` 掐掉在飞的那次操作，
        ``False`` 让它做完再插。

        **没有 run 在跑时不静默吞掉**：调用方（WS 层）应改走 `submit`，
        那才是"新的一轮"的语义（D26）。
        """
        if self._current is None or self._current.done() or self._inbox is None:
            return False
        if not self._inbox.accepting:
            return False
        self._inbox.push(text, interrupt=interrupt)
        return True

    def stop(self) -> bool:
        """打断当前 run，不带话。

        **不是"作废"**：掐掉在飞的那次操作后，如果没有话要插，这一轮挂起成草稿，
        下一条输入接着它跑（D118）。用户按停止只说明"别再往下跑了"。

        writer / state 阶段没有操作级的打断（各是一次不可分的生成），那里靠取消
        整个 task。**掐到哪一段，哪一段就判为失败、产出作废**，已完成的上游存进
        草稿等续跑（D142）——半截正文不留，留着下一轮会接着那半句写下去，
        而用户按停止恰恰是因为不想要它。
        """
        if self._current is None or self._current.done():
            return False
        if self._inbox is not None and self._inbox.accepting:
            self._inbox.push(interrupt=True)
            return True
        self._current.cancel()
        return True

    # -- 主循环 -------------------------------------------------------------

    async def _loop(self) -> None:
        while True:
            try:
                item: _Queued | None = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                # 队列空了才考虑 goal 驱动的自动续跑——用户输入优先（D27）。
                item = None if self._should_autocontinue() else await self._queue.get()

            # `None` 有两个来源：队列空了而 goal 该自己开下一轮，或者 `nudge()`
            # 放进来的唤醒信号。**两者都要在这一刻再问一次**——目标可能在信号
            # 排队期间被放弃、暂停或跑满了预算。
            if item is None and not self._should_autocontinue():
                continue

            if item is not None and item in self._pending:
                self._pending.remove(item)
                self._emit(QueueStatus(pending=[q.text for q in self._pending]))

            self._inbox = Inbox()
            self._current = asyncio.create_task(self._execute(item))

            # **不要 `await self._current`。** 那样 stop() 取消这个 run 时，
            # CancelledError 会打到循环身上，而循环必须活下来等下一条输入；
            # 可一旦把它 `except ... pass` 吞掉，循环就再也取消不掉了——
            # 断开连接时 `aclose()` 会永远等不到，测试拆解也会挂住。
            # `asyncio.wait` 只等待、不传播子任务的取消，两个要求同时满足。
            await asyncio.wait({self._current})

            # **这一轮结束时信箱里可能还压着话。** 最常见的一种：引导正好落在
            # `submit_outline` 那次调用上——它是终止点，直接返回，走不到操作边界。
            # 用户的输入在任何情况下都不该被吞掉，所以在这里改开新的一轮。
            for leftover in self._inbox.drain() if self._inbox else []:
                await self.submit(leftover)

            if self._current.cancelled():
                # 打断后推演多半已存成草稿（D118）——下一条输入会接着它跑，
                # 不是从头重来。用户必须知道这件事，否则会以为自己按的是"取消"。
                self._emit(RunInterrupted(resumable=self._repo.load_draft() is not None))
            else:
                # 兜底：任何漏网的异常都不能静默杀掉 session 循环——
                # 那会让连接看起来还活着，但此后所有输入石沉大海。
                exc = self._current.exception()
                if exc is not None:
                    self._autocontinue_armed = False
                    self._emit(RunFailed(reason=f"未预期的错误：{exc}"))

    def _should_autocontinue(self) -> bool:
        if not self._autocontinue_armed:
            return False
        session = self._repo.load_session()
        goal, run = session.meta.goal, session.goal_run
        if goal is None or run is None or run.paused:
            return False
        return run.exhausted(goal) is None

    async def _execute(self, item: _Queued | None) -> None:
        session = self._repo.load_session()
        # **id 提前生成**：目标开场时 goal_manager 要先说话，而那些记录属于
        # 它开出来的这一轮（D168）。
        run_id = new_id("run")

        # 自动续跑时用 goal_manager 写的那条输入（D141）。**用完即清**：它只对紧接着
        # 的这一轮有效，留着会在用户插话之后被错误地重放。真的用户输入优先（D27），
        # 所以只在 `item is None` 这条路上取。
        simulated = self._goal_input if item is None else None
        self._goal_input = None

        if item is None and self._goal_opening:
            self._goal_opening = False
            # **先宣告这一轮开始，再让它规划。** 规划要跑几十秒，不先宣告的话
            # 界面上那段时间什么都没有——而这一轮的记录已经在往外推了。
            self._run_id = run_id
            self._emit(RunStarted(run_id=run_id))
            simulated = await self._open_goal(session, run_id) or simulated
            # 规划期间目标可能当场判完、撞上预算、或被用户放弃。
            if not self._should_autocontinue():
                self._emit(RunInterrupted(resumable=False))
                return
            session = self._repo.load_session()

        try:
            outcome = await execute_run(
                self._repo,
                session,
                self._gateway,
                # 不传 registry：工具面依赖能力开关与 worldinfo 模式，
                # 两者都可能被用户改，必须每轮重新组装。
                # **来源要标注**（D148）：goal_manager 靠它分辨"剧情偏了是我推错了"
                # 和"用户主动把剧情带走了"。director 不读这个字段——两种输入在它
                # 眼里必须一模一样，否则它会对模拟输入区别对待。
                user_input=(
                    UserInput(text=item.text)
                    if item
                    else (
                        UserInput(text=simulated, source=InputSource.SIMULATED)
                        if simulated
                        else None
                    )
                ),
                # **父节点只有一个来源：出队那一刻的活跃叶**（D147）。
                # 普通输入挂在它下面（延长），重生成挂在它的父下面（开兄弟）。
                parent_id=_parent_for(self._repo, session.active_leaf_id, item),
                ask_user=self._ask_user,
                control_override=item.control_override if item else None,
                on_trace=self._on_trace,
                inbox=self._inbox,
                developer=self._developer,
                # 分片带上 run_id：前端要把它归到正确的那一轮名下（D160）。
                on_delta=self._emit_delta,
                run_id=run_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单个 run 失败不该拖垮 session
            # 失败后暂停自动续跑：再跑一次多半还是失败，空转只会烧钱。
            # 用户的下一条输入会重新打开它。
            self._autocontinue_armed = False
            self._emit(RunFailed(reason=str(exc)))
            return

        # 一轮正常结束——这才是「检查是否达标、未达标则开启下一轮」的触发点。
        self._autocontinue_armed = True

        self._emit(RunFinished(node=outcome.node, notes=outcome.notes))
        self._emit(
            UsageUpdate(
                tokens_in=outcome.usage.tokens_in,
                tokens_out=outcome.usage.tokens_out,
            )
        )
        self._emit(
            StateSnapshot(
                values=self._repo.state_at(outcome.node.id).values,
                leaf_id=outcome.node.id,
            )
        )

        await self._check_goal(outcome.node.id, outcome.usage)

    async def _open_goal(self, session: Session, run_id: str) -> str | None:
        """**目标刚设上（或刚恢复）时，先让它规划一次**（D168）。返回开场那条输入。

        不这么做的话，goal 驱动的第一轮是空手推的：**director 从来不读 goal**
        （它眼里模拟输入和真输入必须一模一样，D148），所以那一轮完全不知道有目标
        这回事，而它恰恰是最该定方向的一轮。goal_manager 要等它写完才第一次开口，
        然后照着一段随机开的剧情去拆阶段。

        记录挂在**即将开始的那一轮**名下（`run_id` 由调用方先生成），不是上一轮：
        这几次调用是为了那一轮才发生的。路径末端仍取当前活跃叶——它是"故事到
        这儿了"，和这一轮无关。
        """
        goal = session.meta.goal
        if goal is None:
            return None
        run = session.goal_run or GoalRun(started_at_leaf_id=session.active_leaf_id)
        if run.paused:
            return None

        step, usage = await self._goal_turn(session, goal, run, run_id, session.active_leaf_id)
        # **开场不算一轮**：还没有任何 run 被开出来。token 照算——它是真花的钱。
        self._settle_goal(goal, run, step, usage, count_run=False)
        return step.next_input

    async def _check_goal(self, leaf_id: str, usage: Usage) -> None:
        """goal_manager 在 **run 末**跑（D28/D40/D141/D148）——与控制权（轮内）
        作用点不同。

        它做的事都在那三个工具里（`plan` / `advance` / `finish`），这里只负责
        搭台：把这一轮的记录追加进对话、跑一轮、把结果记回 session。
        **它写的那条输入存在 `_goal_input` 里，下一次自动续跑拿去用**——
        这就是"模拟用户输入"落地的地方。

        ``usage`` 是**刚跑完那一轮**的开销。它得算进 goal 的预算里：D28 那条预算
        兜的是「自主运行成本」，而钱主要花在它开出来的那些 run 上（细纲 + 正文 +
        状态），不是它自己那一两次判定调用——只算后者的话预算兜住的是零头。
        暂停期间不算：那时候是用户自己在写，goal 没有在跑。
        """
        session = self._repo.load_session()
        goal = session.meta.goal
        if goal is None:
            return
        run = session.goal_run or GoalRun(
            # 走到这里还没有 `GoalRun`，说明 goal 不是经 `SetGoal` 设的（会话创建时
            # 就带着，或走 REST 改的）。那它是**这一轮之前**就有的，所以起点划在
            # 这一轮的父节点上（D150）。
            started_at_leaf_id=self._repo.load_node(leaf_id).parent_id
        )
        if run.paused:
            return

        # 判过一次就不用再开场了——开场要的那条输入它刚写好了。
        self._goal_opening = False

        step, spent = await self._goal_turn(session, goal, run, leaf_id, leaf_id)
        self._goal_input = step.next_input
        self._settle_goal(goal, run, step, spent + usage, count_run=True)

    async def _goal_turn(
        self,
        session: Session,
        goal: Goal,
        run: GoalRun,
        run_id: str,
        leaf_id: str | None,
    ) -> tuple[GoalStep, Usage]:
        """搭一次 goal_manager 调用的台：备好对话、跑一轮、把记录挂到 `run_id` 上。

        `run_id` 是这些记录**归属的那一轮**，`leaf_id` 是当前路径的末端。判定时
        两者是同一个（判的就是刚写完那一轮）；开场时不同——记录属于即将开始的
        那一轮，而路径还停在上一轮的末端。
        """
        # goal_manager 是真实的模型调用，因此它**必须和 run 一样可见、一样计入预算**：
        # 不推事件的话，goal 为什么迟迟不达标就完全无从查起。
        tracer = Tracer(
            self._repo,
            run_id,
            lambda event: self._emit(Trace(event=event)),
            persist=session.capabilities.trace or self._developer,
            developer=self._developer,
        )

        # 事件密度是 goal_manager 唯一读的控制维度：它决定阶段拆多细、
        # 一条输入推多远（D93 那条投影的第三面）。
        sync_conversation(
            session.meta,
            goal,
            run,
            self._repo.path_to(leaf_id) if leaf_id else [],
            density=session.control.expansion_density,
        )
        step = await advance_goal(
            self._gateway,
            tracer,
            goal,
            run,
            density=session.control.expansion_density,
            user_id=session.id,
            # **它的思考也要能看见**（D166）。分片和它的 trace 事件挂同一个
            # `run_id`——显式传而不是靠 `self._run_id` 刚好设对了。
            on_delta=(
                (lambda kind, text: self._emit_delta(Actor.GOAL_MANAGER.value, kind, text, run_id))
                if session.capabilities.stream
                else None
            ),
        )
        return step, tracer.usage

    def _settle_goal(
        self, goal: Goal, run: GoalRun, step: GoalStep, spent: Usage, *, count_run: bool
    ) -> None:
        """把这一次的产出记回去：花销、终态、预算、进度。"""
        # **无论成败都记一次 run**——否则持续失败的 run 会让自动续跑无限空转。
        # 开场那次不算：它还没开出任何一轮来。
        run.spent_runs += 1 if count_run else 0
        run.spent_tokens += spent.tokens_in + spent.tokens_out

        if step.finished is not None:
            self._finish_goal(*step.finished)
            return

        # 预算到顶**不是失败**，是暂停：用户可以把上限调大接着跑（D148）。
        # 对话原样留着，所以恢复时不用重新判一遍。
        exhausted = run.exhausted(goal)
        if exhausted is not None:
            run.paused, run.paused_reason = True, exhausted

        if not self._save_goal_run(run):
            # 判定期间用户放弃了目标。那这一次的结果连同它写的下一条输入一起
            # 作废——继续推进度只会让界面显示一个已经不存在的目标。
            self._goal_input = None
            return
        # **每一轮都推一次进度**（D161）：中间状态被一条条划掉是用户最想看的东西，
        # 而它此前只写在盘上，界面要等到 goal 结束才会动。
        self._emit(
            GoalProgress(
                subgoals=run.plan.subgoals if run.plan else [],
                spent_runs=run.spent_runs,
                spent_tokens=run.spent_tokens,
                max_runs=goal.max_runs,
                max_total_tokens=goal.max_total_tokens,
            )
        )
        if exhausted is not None:
            self._emit(GoalPaused(reason=exhausted))

    def _emit_delta(self, actor: str, kind: str, text: str, run_id: str | None = None) -> None:
        """把一段分片推出去。**能力关掉时根本走不到这里**——`ctx.stream_to`
        返回 `None`，那一路直接退回非流式（D160）。

        ``run_id`` 缺省用当前这一轮的。显式传是给 goal_manager 用的：它在 run
        结束**之后**跑，那时 `self._run_id` 还没被下一轮改掉，两者其实一样，
        但那是巧合而不是保证。
        """
        if kind not in _DELTA_KINDS:
            return
        self._emit(
            Delta(
                run_id=run_id or self._run_id,
                actor=actor,
                kind=cast(DeltaKind, kind),
                text=text,
            )
        )

    def _on_trace(self, event: TraceEvent) -> None:
        """每条 trace 都推给 UI，**顺带把 `RunStarted` 补出来**。

        `run_id` 是在 `execute_run` 内部生成的，外面拿不到——而生命周期消息
        （`RunStarted` / `RunFinished` / `RunFailed` / `RunInterrupted`）是一组，
        少一个前端就不知道一轮什么时候开始：进度指示不亮，上一轮的过程也不会清掉。
        这条消息在协议里定义了很久却从没被发出过。
        """
        # **宣告排在它自己那条 trace 之前。** 反过来的话，客户端多半会在收到
        # `run_started` 时清掉上一轮的过程记录——而这一轮的第一条已经先到了，
        # 于是它当场被自己的开场白抹掉。
        #
        # 已经宣告过就不再宣告：goal 开场时 `_execute` 提前宣告过一次（要让
        # 规划那几十秒在界面上有着落，D168），那和这里说的是同一轮。
        if event.type is TraceEventType.RUN_STARTED and event.run_id != self._run_id:
            self._run_id = event.run_id
            self._emit(RunStarted(run_id=event.run_id))
        self._emit(Trace(event=event))

    def _save_goal_run(self, run: GoalRun) -> bool:
        """写回运行时状态。**每次都重新载入 session**——判定期间可能有 REST
        请求改过它（元操作与生成并行，D24）。

        **目标没了就不写**：判定要跑几十秒，用户完全可能在这中间按了放弃。
        照写的话会留下一份没有主人的 `goal_run`——它带着几十万字的对话，而且
        下次设目标时那份旧进度会当成新目标的进度显示出来。返回值告诉调用方
        "这次判定已经作废了"。
        """
        session = self._repo.load_session()
        if session.meta.goal is None:
            return False
        session.goal_run = run
        self._repo.save_session(session)
        return True

    def _finish_goal(self, outcome: str, reason: str) -> None:
        """交回控制权。**两种终态都走这里，但用户必须知道是哪一种**：
        一个是达成了，一个是这个目标已经不可能了，后续动作完全不同（D148）。

        goal 与它的运行时状态一并清掉——**不留历史 goal**。
        """
        self._goal_input = None
        self._autocontinue_armed = False
        session = self._repo.load_session()
        if session.meta.goal is None:
            # 判定期间用户自己放弃了。**不报"目标达成"**——那个目标已经不在了，
            # 报出去只会让用户以为放弃没生效。
            return
        session.meta.goal = None
        session.goal_run = None
        self._repo.save_session(session)
        self._emit(GoalFinished(outcome=outcome, reason=reason))

    # -- ask_question -------------------------------------------------------

    async def _ask_user(self, question: str, options: list[str]) -> str:
        question_id = new_id("q")
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._questions[question_id] = future

        self._emit(Question(question_id=question_id, question=question, options=options))
        try:
            # 不设超时（D66）。这就是全部的挂起机制——suspend/resume，
            # 不是 timeout/retry；上下文不会过期，因为逻辑上什么都没发生。
            return await future
        finally:
            self._questions.pop(question_id, None)

    # -- 出站 ---------------------------------------------------------------

    def _emit(self, message: ServerMessage) -> None:
        """入队即返回。**同步**，因此从同步回调（如 trace）里发也保持顺序。"""
        self._out.put_nowait(message)

    async def _sender(self) -> None:
        while True:
            message = await self._out.get()
            with contextlib.suppress(Exception):
                # 连接断了不该拖垮 runner——它还要把当前 run 跑完并落盘。
                await self._broadcast(message)


def _parent_for(repo: SessionRepository, leaf_id: str | None, item: _Queued | None) -> str | None:
    """这一轮挂在哪个节点下。**分支只能在当前节点上开**（D147）。

    - 普通输入（含 goal 驱动的自动续跑）→ 挂在**当前叶**下，这是延长，不是分支。
    - 重生成 → 挂在**当前叶的父**下，成为它的兄弟，原分支保留（D46）。

    以前还有第三条路：提交时显式指定任意 `parent_id`，于是可以在历史上任何一点
    分叉。收掉它是因为**「当前节点」从此有唯一含义**——想在更早的地方分叉，
    先 `SwitchBranch` 切过去再动手。**切换不受限制，受限的只是创建**，
    所以任意分叉仍然可达，只是不能一步到位。
    """
    if item is None or not item.regenerate or leaf_id is None:
        return leaf_id
    return repo.load_node(leaf_id).parent_id
