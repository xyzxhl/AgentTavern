"""Session：一次会话的顶层配置。落盘为 ``session.json``。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from .control import GoalRun, SessionMeta
from .node import new_id


class Session(BaseModel):
    id: str = Field(default_factory=lambda: new_id("sess"))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    worldbook_id: str = ""
    """在哪份设定上开的（S63）。**worldbook 是一级资产，一份能开出好几个 session。**

    存 id 而不是内嵌一份拷贝：拷贝的话「改一次、所有故事都跟着」就没了，而那正是
    把它独立出来要买的东西。代价是设定会在故事跑着的时候被别处改动——那件事的
    答案见 `initial_state`（已经发生过的剧情所依赖的事实**冻结**，静态设定跟着改）。

    空串是**旧数据**：升级时会给它建一份自己的 worldbook（`Store.migrate`）。
    """

    opening_id: str | None = None
    """创建故事时选中的开场白条目。``None`` 表示不用开场白（S168）。

    id 只用于说明来源；运行时使用下面冻结的 ``opening``，不会回头现读 worldbook。
    """

    opening: str = ""
    """创建 session 时冻结的开场正文。它显示在第一个 turn 之前，但不是节点。

    开场一旦展示就已经是故事历史的一部分；若继续按 worldbook 现读，后来编辑一条
    开场白会让既有故事的开头凭空变化。缺字段的旧数据自然得到空串，不需要迁移。
    """

    corpus_ids: list[str] = Field(default_factory=list)
    """这局故事挂了哪几份外部资料（S136）。**零到多份。**

    同 `worldbook_id`，存 id 不存拷贝——一份资料要能被很多局故事挂。
    但和它不同的是**这里是列表且可以为空**：设定是一局故事的地基，资料是可选的
    增援，没有资料的故事是完全正常的（那时 `corpus_search` 根本不注册，S145）。

    **旧数据不需要迁移**：字段缺席时就是空列表，而空列表的语义正是"没挂"。
    """

    meta: SessionMeta = Field(default_factory=SessionMeta)
    """元指令：**这局故事**要什么。创建时设定，很少改。

    世界那一半是 worldbook 的条目（S63/S64）。
    """

    # `control` 与 `capabilities` **已删除**（R31/S80）：控制维度与能力开关
    # 整体上调到了提示词层——它们全都在改「模型收到什么」，就该和提示词住在
    # 同一份文件里。**session 上从此只有内容，没有配置。**

    initial_state: dict[str, Any] | None = None
    """**冻结下来的初始状态**（S67）。``None`` = 故事还没开始，现算。

    第一轮开始时由 `freeze_initial_state()` 定下来。之后再改 worldbook 的
    `STATE` 条目就只影响新会话，不再回头改写既有的每一轮——那些轮次已经依赖
    当时的事实写出去了。

    早先它一直是现算的，说法是"改了影响所有分支，同改角色卡，是对的"。
    **那个类比不成立**：改角色卡改的是设定，而这里改的是**已经发生过的剧情
    所依赖的事实**——第五轮写着"她掏出钥匙"，而钥匙是初始状态给的；把它删掉，
    那一轮就凭空多了一把不存在的钥匙，且没有任何地方会报错。
    """

    active_leaf_id: str | None = None
    """活跃路径的叶节点。路径 = 根 → 此节点的一条链；state 沿该路径重放（S42）。"""

    input_revision: int = 0
    """已进入系统的顶层输入序号。输入一到达就递增，用来关闭上一轮编辑窗口。"""

    editable_node_id: str | None = None
    """当前唯一可编辑的上一轮节点。run 执行/挂起时为空；整轮失败时回到上一叶。"""

    goal_run: GoalRun | None = None
    """当前 goal 的运行时状态（S92）：产品记录、计划与进度、花销、暂停与否。

    **和 `meta.goal` 分开**：那个是用户配置（目标是什么、给多少预算），这个是
    跑起来之后的东西。混在一起的话"用户改了什么"和"系统跑出了什么"就分不开，
    而前者要禁改、后者每轮都在变。

    goal 结束（`done` / `failed`）时连同 `meta.goal` 一起清掉——**不留历史 goal**。
    """
