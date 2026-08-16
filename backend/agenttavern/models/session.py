"""Session：一次会话的顶层配置。落盘为 ``session.json``。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from .control import Capabilities, ControlDimensions, GoalRun, SessionMeta, SessionModes
from .node import new_id


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sess"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    worldbook_id: str = ""
    """在哪份设定上开的（D170）。**worldbook 是一级资产，一份能开出好几个 session。**

    存 id 而不是内嵌一份拷贝：拷贝的话「改一次、所有故事都跟着」就没了，而那正是
    把它独立出来要买的东西。代价是设定会在故事跑着的时候被别处改动——那件事的
    答案见 `initial_state`（已经发生过的剧情所依赖的事实**冻结**，静态设定跟着改）。

    空串是**旧数据**：升级时会给它建一份自己的 worldbook（`Store.migrate`）。
    """

    meta: SessionMeta = Field(default_factory=SessionMeta)
    """元指令：**这局故事**要什么。创建时设定，很少改。

    世界那一半在 `WorldMeta`，跟着 worldbook 走（D170）。
    """

    control: ControlDimensions = Field(default_factory=ControlDimensions)
    """控制维度的常驻取值。单轮可被 ControlOverride 覆盖。"""

    modes: SessionModes = Field(default_factory=SessionModes)

    capabilities: Capabilities = Field(default_factory=Capabilities)
    """能力开关。关闭的能力其工具**不注册**——模型看不到。"""

    initial_state: dict[str, Any] | None = None
    """**冻结下来的初始状态**（D158）。``None`` = 故事还没开始，现算。

    第一轮开始时由 `freeze_initial_state()` 定下来。之后再改 worldbook 的
    `STATE` 条目就只影响新会话，不再回头改写既有的每一轮——那些轮次已经依赖
    当时的事实写出去了。

    早先它一直是现算的，说法是"改了影响所有分支，同改角色卡，是对的"。
    **那个类比不成立**：改角色卡改的是设定，而这里改的是**已经发生过的剧情
    所依赖的事实**——第五轮写着"她掏出钥匙"，而钥匙是初始状态给的；把它删掉，
    那一轮就凭空多了一把不存在的钥匙，且没有任何地方会报错。
    """

    active_leaf_id: str | None = None
    """活跃路径的叶节点。路径 = 根 → 此节点的一条链；state 沿该路径重放（D48）。"""

    goal_run: GoalRun | None = None
    """当前 goal 的运行时状态（D148）：对话、计划与进度、花销、暂停与否。

    **和 `meta.goal` 分开**：那个是用户配置（目标是什么、给多少预算），这个是
    跑起来之后的东西。混在一起的话"用户改了什么"和"系统跑出了什么"就分不开，
    而前者要禁改、后者每轮都在变。

    goal 结束（`done` / `failed`）时连同 `meta.goal` 一起清掉——**不留历史 goal**。
    """
