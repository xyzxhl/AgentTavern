"""Session：一次会话的顶层配置。落盘为 ``session.json``。"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .control import Capabilities, ControlDimensions, SessionMeta, SessionModes
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
