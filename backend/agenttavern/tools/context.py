"""RunContext：一个 run 期间工具能碰到的一切。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..llm import LLMGateway, OnDelta
from ..models import Actor, Capabilities, Session, StateDelta, WorldState
from ..storage import SessionRepository
from .inbox import Inbox

AskUser = Callable[[str, list[str]], Awaitable[str]]
"""向用户提问并等待选择。

**只考虑逻辑时间，不设超时**（D66）——等待期间逻辑上什么都没发生，
director 的上下文不会过期，从中断点续跑即可（suspend/resume，非 timeout/retry）。
在实现上它就是 ``await`` 一个由 WS 层解决的 future，因此 loop 不需要任何
特殊的挂起状态机。
"""


RunDelta = Callable[[str, str, str], None]
"""``(actor, kind, text)``。比 `OnDelta` 多一个来源——**分片本身不带来源**，
而前端要靠它决定这段字往正文区还是过程区放（D160）。"""


@dataclass
class RunContext:
    run_id: str
    repo: SessionRepository
    session: Session
    gateway: LLMGateway

    parent_id: str | None = None
    """本 run 挂在哪个节点下。历史与状态沿这条路径读取（D48）。"""

    ask_user: AskUser | None = None

    on_delta: RunDelta | None = None
    """流式分片的出口（D160）。``None`` = 这一轮不流式。

    **它按 agent 换一层包装**（见 `stream_to`）：分片本身不带来源，而前端要靠
    来源决定这段字往正文区还是过程区放。
    """

    inbox: Inbox | None = None
    """跑到一半插进来的用户输入（F3b / D119）。``None`` 表示这一 run 不接受插话。

    **每个 run 一个**，由编排层建立——它的生命周期就是这一 run 的生命周期。
    """

    @property
    def capabilities(self) -> Capabilities:
        return self.session.capabilities

    def stream_to(self, actor: Actor) -> OnDelta | None:
        """给某个 agent 的分片出口。能力关掉或没有出口时返回 ``None``。

        **返回 `None` 时调用方走非流式那条路**——上层不必写两份逻辑，把它原样
        传给 `complete(on_delta=...)` 即可。
        """
        if not self.capabilities.stream or self.on_delta is None:
            return None
        sink = self.on_delta
        return lambda kind, text: sink(actor.value, kind, text)

    staged_delta: StateDelta = field(default_factory=StateDelta)
    """``state_write`` 写入这里，run 完整结束时才落盘（D56）。

    **填它的人是 state_manager，不再是 director**（D143）——状态维护后置到
    writer 之后，读着细纲和正文去记，而不是边推演边记。它仍然只暂存不落盘：
    节点是原子的，细纲、正文、增量必须同时抵达（D48/D56）。
    """

    state_write_count: int = 0
    """``state_write`` 调用了几次。**只用于 trace 与统计，不再是契约。**

    它原先撑着 E0：`submit_outline` 前必须至少写一次，为的是区分"确实没变化"
    与"模型忘了记"。那条契约随 D143 作废——director 不写状态了，而 state_manager
    唯一的任务就是记状态，它空手回来就是"没变化"，为消这个歧义再花一轮往返不值。
    """

    outline: str | None = None
    """由 ``submit_outline`` 填入，终止 loop。"""

    def base_state(self) -> WorldState:
        """本 run 开始时的状态：沿父路径重放的结果。"""
        return self.repo.state_at(self.parent_id)

    def current_state(self) -> WorldState:
        """叠加本 run 已暂存的增量后的状态。"""
        return self.base_state().apply(self.staged_delta)
