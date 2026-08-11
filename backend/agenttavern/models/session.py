"""Session：一次会话的顶层配置。落盘为 ``session.json``。"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .control import Capabilities, ControlDimensions, GoalRun, SessionMeta, SessionModes
from .node import new_id


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sess"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    meta: SessionMeta = Field(default_factory=SessionMeta)
    """元指令：这个 session 要什么体验。创建时设定，很少改。"""

    control: ControlDimensions = Field(default_factory=ControlDimensions)
    """控制维度的常驻取值。单轮可被 ControlOverride 覆盖。"""

    modes: SessionModes = Field(default_factory=SessionModes)

    capabilities: Capabilities = Field(default_factory=Capabilities)
    """能力开关。关闭的能力其工具**不注册**——模型看不到。"""

    active_leaf_id: str | None = None
    """活跃路径的叶节点。路径 = 根 → 此节点的一条链；state 沿该路径重放（D48）。"""

    goal_run: GoalRun | None = None
    """当前 goal 的运行时状态（D148）：对话、计划与进度、花销、暂停与否。

    **和 `meta.goal` 分开**：那个是用户配置（目标是什么、给多少预算），这个是
    跑起来之后的东西。混在一起的话"用户改了什么"和"系统跑出了什么"就分不开，
    而前者要禁改、后者每轮都在变。

    goal 结束（`done` / `failed`）时连同 `meta.goal` 一起清掉——**不留历史 goal**。
    """
