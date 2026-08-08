"""RunContext：一个 run 期间工具能碰到的一切。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..llm import LLMGateway
from ..models import Capabilities, Session, StateDelta, WorldState
from ..storage import SessionRepository
from .inbox import Inbox

AskUser = Callable[[str, list[str]], Awaitable[str]]
"""向用户提问并等待选择。

**只考虑逻辑时间，不设超时**（D66）——等待期间逻辑上什么都没发生，
director 的上下文不会过期，从中断点续跑即可（suspend/resume，非 timeout/retry）。
在实现上它就是 ``await`` 一个由 WS 层解决的 future，因此 loop 不需要任何
特殊的挂起状态机。
"""


@dataclass
class RunContext:
    run_id: str
    repo: SessionRepository
    session: Session
    gateway: LLMGateway

    parent_id: str | None = None
    """本 run 挂在哪个节点下。历史与状态沿这条路径读取（D48）。"""

    ask_user: AskUser | None = None

    inbox: Inbox | None = None
    """跑到一半插进来的用户输入（F3b / D119）。``None`` 表示这一 run 不接受插话。

    **每个 run 一个**，由编排层建立——它的生命周期就是这一 run 的生命周期。
    """

    @property
    def capabilities(self) -> Capabilities:
        return self.session.capabilities

    staged_delta: StateDelta = field(default_factory=StateDelta)
    """``state_write`` 写入这里，run 完整结束时才落盘（D56）。

    否则 director 先写、writer 后跑，中途失败会造成"增量已落盘但正文不存在"。
    """

    state_write_count: int = 0
    """E0 契约：``submit_outline`` 前必须至少调用过一次 ``state_write``。

    强制这一次是为了区分"确实没有变化"与"模型忘了提交"——后者才是 D19 要防的
    失败模式（漏提交导致数据与文本分叉，几百 run 后才爆且查不出源头）。
    """

    outline: str | None = None
    """由 ``submit_outline`` 填入，终止 loop。"""

    def base_state(self) -> WorldState:
        """本 run 开始时的状态：沿父路径重放的结果。"""
        return self.repo.state_at(self.parent_id)

    def current_state(self) -> WorldState:
        """叠加本 run 已暂存的增量后的状态。"""
        return self.base_state().apply(self.staged_delta)
