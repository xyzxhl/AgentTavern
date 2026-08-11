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

from ..agents import budget_exhausted, final_verdict, plan_along, plan_subgoals, take_step
from ..llm import LLMGateway
from ..models import ControlOverride, GoalOutcome, GoalPlan, UserInput, new_id
from ..orchestration import execute_run
from ..storage import SessionRepository
from ..tools import Inbox
from ..tracing import Tracer
from .protocol import (
    GoalFinished,
    Question,
    QueueStatus,
    RunFailed,
    RunFinished,
    RunInterrupted,
    ServerMessage,
    StateSnapshot,
    Trace,
    UsageUpdate,
)

Broadcast = Callable[[ServerMessage], Awaitable[None]]


@dataclass
class _Queued:
    text: str
    control_override: ControlOverride | None = None
    parent_id: str | None = None
    """显式指定父节点，用于重生成（D46）。

    ``None`` 表示"出队时的活跃叶"，这是普通输入的语义。**重生成必须走这里，
    不能靠改 `session.active_leaf_id` 来传参**——那是共享状态，若此刻有 run 在飞，
    它结束时会把活跃叶覆盖成自己，重生成就挂到了错误的父节点上。
    """

    explicit_parent: bool = False
    """``parent_id`` 是否是显式指定的。因为 ``None`` 本身也是合法的父节点（根）。"""


class SessionRunner:
    def __init__(
        self,
        repo: SessionRepository,
        gateway: LLMGateway,
        broadcast: Broadcast,
    ) -> None:
        self._repo = repo
        self._gateway = gateway
        self._broadcast = broadcast

        self._queue: asyncio.Queue[_Queued] = asyncio.Queue()
        self._pending: list[_Queued] = []
        self._questions: dict[str, asyncio.Future[str]] = {}
        self._current: asyncio.Task[None] | None = None
        self._inbox: Inbox | None = None
        """当前 run 的插话信箱。**一个 run 一个**——上一轮没人认领的话不该漏到下一轮。"""

        self._loop_task: asyncio.Task[None] | None = None

        # 所有出站消息走**同一条队列**，由单个 sender 顺序送出。
        # trace 是从同步回调里产生的，若各自 create_task 发送就会乱序——
        # UI 上会看到「本轮结束」排在「开始新的一轮」前面。
        self._out: asyncio.Queue[ServerMessage] = asyncio.Queue()
        self._sender_task: asyncio.Task[None] | None = None

        # goal 预算保底（D28）。进程内计数——重启会重置，v1 接受这个简化。
        self._goal_runs = 0
        self._goal_tokens = 0

        self._goal_input: str | None = None
        """goal_manager 替用户写的下一条输入（D141）。

        自动续跑时拿它当 `user_input`——**这就是「模拟用户输入」落地的地方**。
        它不是持久状态：进程内的一次传递，用完即清。
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
        parent_id: str | None = None,
        explicit_parent: bool = False,
    ) -> None:
        item = _Queued(
            text=text,
            control_override=override,
            parent_id=parent_id,
            explicit_parent=explicit_parent,
        )
        self._pending.append(item)
        await self._queue.put(item)
        self._emit(QueueStatus(pending=[q.text for q in self._pending]))

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

        writer 阶段还没有操作级的打断（它是一次不可分的生成），因此那里仍然靠
        取消整个 task——D60 的语义不变：保留已产出的正文片段。
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
        goal = self._repo.load_session().meta.goal
        if goal is None:
            return False
        return budget_exhausted(goal, self._goal_runs, self._goal_tokens) is None

    async def _execute(self, item: _Queued | None) -> None:
        session = self._repo.load_session()

        # 无论成败都记一次——否则持续失败的 run 会让 goal 驱动的自动续跑无限空转。
        self._goal_runs += 1

        # 自动续跑时用 goal_manager 写的那条输入（D141）。**用完即清**：它只对紧接着
        # 的这一轮有效，留着会在用户插话之后被错误地重放。真的用户输入优先（D27），
        # 所以只在 `item is None` 这条路上取。
        simulated = self._goal_input if item is None else None
        self._goal_input = None

        try:
            outcome = await execute_run(
                self._repo,
                session,
                self._gateway,
                # 不传 registry：工具面依赖能力开关与 worldinfo 模式，
                # 两者都可能被用户改，必须每轮重新组装。
                user_input=(
                    UserInput(text=item.text)
                    if item
                    else (UserInput(text=simulated) if simulated else None)
                ),
                # 重生成显式带来父节点；普通输入用出队时的活跃叶。
                parent_id=(
                    item.parent_id
                    if item is not None and item.explicit_parent
                    else session.active_leaf_id
                ),
                ask_user=self._ask_user,
                control_override=item.control_override if item else None,
                on_trace=lambda event: self._emit(Trace(event=event)),
                inbox=self._inbox,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单个 run 失败不该拖垮 session
            # 失败后暂停自动续跑：再跑一次多半还是失败，空转只会烧钱。
            # 用户的下一条输入会重新打开它。
            self._autocontinue_armed = False
            self._emit(RunFailed(reason=str(exc)))
            return

        self._goal_tokens += outcome.usage.tokens_in + outcome.usage.tokens_out
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

        await self._check_goal(outcome.node.id)

    async def _check_goal(self, leaf_id: str) -> None:
        """goal_manager 在 **run 末**跑（D28/D40/D141）——与控制权（轮内）作用点不同。

        一轮之后它做三件事里的一到两件：核对阶段进度并写出下一条输入；若阶段全部
        走完，再判目标的去向（完成 / 完全偏离 / 重新规划）。**它写的那条输入存在
        `_goal_input` 里，下一次自动续跑拿去用**——这就是"模拟用户输入"落地的地方。
        """
        session = self._repo.load_session()
        goal = session.meta.goal
        if goal is None:
            return

        # goal_manager 是真实的模型调用，因此它**必须和 run 一样可见、一样计入预算**：
        # 不推事件的话，goal 为什么迟迟不达标就完全无从查起；不计 token 的话，
        # D28 那个用来兜住自主运行成本的预算就漏掉了每轮都发生的调用。
        tracer = Tracer(
            self._repo,
            leaf_id,
            lambda event: self._emit(Trace(event=event)),
            persist=session.capabilities.trace,
        )
        path = self._repo.path_to(leaf_id)
        # 事件密度是 goal_manager 唯一读的控制维度：它决定阶段拆多细、
        # 一条输入推多远（D93 那条投影的第三面）。
        density = session.control.expansion_density
        sid = session.id

        async def replan(previous: GoalPlan | None) -> GoalPlan:
            return await plan_subgoals(
                self._gateway,
                tracer,
                goal,
                path,
                previous=previous,
                density=density,
                user_id=sid,
            )

        async def step_on(current: GoalPlan) -> GoalPlan:
            """核对进度并写下一条输入。**每次换计划之后都要走一遍**——
            否则那一轮的自动续跑会没有输入，等于白跑（D141）。
            """
            step = await take_step(
                self._gateway, tracer, goal, path, current, density=density, user_id=sid
            )
            self._goal_input = step.user_text
            return step.plan

        # 还没有计划就先拆，紧接着照常推一步：新计划的第一条输入就在这一轮写好，
        # 下一轮直接能用。
        plan = await step_on(plan_along(path) or await replan(None))

        if plan.all_done:
            outcome, reason = await final_verdict(
                self._gateway, tracer, goal, path, plan, density=density, user_id=sid
            )
            if outcome is GoalOutcome.REPLAN and plan.revision >= goal.max_revisions:
                # 重新规划这条路本身没有出口——每次重规划都会给出新的阶段。
                # 到顶就当作走不通，交回控制权（D141）。
                outcome = GoalOutcome.DERAILED
                reason = f"已达重规划上限（{goal.max_revisions}）"

            if outcome is GoalOutcome.REPLAN:
                # 刚才那条输入是冲着旧计划写的，旧计划已经走完了——换新的。
                plan = await step_on(await replan(plan))
            else:
                self._finish_goal(outcome, reason)

        self._goal_tokens += tracer.usage.tokens_in + tracer.usage.tokens_out

        # 计划存在**这一轮的节点**上，跟着路径走（D48）——换分支自然拿到那条
        # 分支自己的进度，不必额外做隔离。
        node = self._repo.load_node(leaf_id)
        node.goal_plan = plan
        self._repo.save_node(node)

    def _finish_goal(self, outcome: GoalOutcome, reason: str) -> None:
        """交回控制权。**完成与完全偏离都走这里，但用户必须知道是哪一种。**"""
        self._goal_runs = 0
        self._goal_tokens = 0
        self._goal_input = None
        self._autocontinue_armed = False
        # 重新载入：判定期间可能有 REST 请求改过 session（元操作与生成并行，D24）。
        session = self._repo.load_session()
        session.meta.goal = None
        self._repo.save_session(session)
        self._emit(GoalFinished(outcome=outcome.value, reason=reason))

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
