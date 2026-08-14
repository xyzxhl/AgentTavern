"""worldinfo：静态设定。由**用户**改变，区别于 state（剧情推进改变）与 history（只增不改）。

检索模式与 history 相同：**全量索引 + 按需展开**（D63）。索引覆盖**全部**条目，
不做任何预筛选——按 D62，筛选子集式的规则（如 lorebook 的关键词触发）不合格：
漏命中时条目完全缺失，而模型根本不知道它存在。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .node import new_id


class WorldbookMode(StrEnum):
    ALWAYS = "always"
    """全量注入上下文。适合体量小、几乎每轮都相关的设定（主角、世界基调）。"""

    ON_DEMAND = "on_demand"
    """只进索引，由模型按需展开（D63）。适合体量大、偶尔相关的条目。"""


class EntryKind(StrEnum):
    """条目分三类（D158 / D159）。

    早先是一条条目**同时**装静态设定和初始状态、共用一个 `mode`。拆开的理由是
    可见性天然不同步：一个角色的背景可能几百字、该按需展开，而他「在不在场」
    必须每轮都在眼前——共用一个开关时只能迁就其中一个。
    """

    WORLD = "world"
    """世界设定：**一段不受限制的文本**。地方、组织、规矩、历史。

    它没有额外字段，因为世界里能有什么东西根本枚举不完——加字段只是把猜测挪深
    一层（P14）。
    """

    CHARACTER = "character"
    """角色设定：一段正文 + **角色语言习惯**（`voice`）。

    和世界设定分开，是因为角色比别的东西多一样**只进 writer** 的属性：他怎么说话。
    那一项此前挂在 `SessionMeta.character_voices` 这个 `名字 → 说话方式` 的字典
    里，于是「这个角色是谁」和「他怎么说话」分在两个地方，改了一处另一处不动，
    还得靠名字对上——改个名字就悄悄断了（同 D158 对 `user_character` 的处理）。
    """

    STATE = "state"
    """初始状态：一棵键值树。会变的东西——在哪、知道什么、和谁什么关系。"""


class WorldbookEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("wi"))
    name: str

    kind: EntryKind = EntryKind.WORLD

    content: str = ""
    """`WORLD` / `CHARACTER` 的正文。`STATE` 条目不用它。"""

    voice: str = ""
    """角色的语言习惯。**只有 `CHARACTER` 用，而且只进 writer**（D33 那把刀）。

    它不参与检索，参与每一个 token——所以它和文风一样归 writer，不进 director。
    """

    is_player: bool = False
    """这个角色**是用户扮演的**（D159）。

    **用户扮演的角色是一般角色的子类**，不是第四种条目：凡是对角色成立的
    （正文、语言习惯、可展开），对它一样成立，它只是多回答一个「用户是谁」。
    做成独立的一类，就得在每一处 `CHARACTER` 分支旁边记得再写一个 `PLAYER`——
    而那种「记得」迟早会漏。

    一份 worldbook 里至多一条为真。`SessionMeta.user_character` 从它取名字。
    """

    values: dict[str, Any] = Field(default_factory=dict)
    """`STATE` 条目的初始状态，一棵嵌套的键值树（D157）。

    **它是「初始状态」，不再叫「种子」**（D158）：那个词暗示"发芽之后就与它无关"，
    而实际语义恰恰相反——它就是这局故事第一帧的状态，是一切分支共同的起点。

    为什么初始状态要和世界信息分开：设定条目常常是混的，角色的性格、背景是静态的
    （不因剧情改变），而他现在在哪、知道什么是会变的。不分开的话，模型只能把后者
    从设定正文里**抄**进 state——而副本会在设定被改动后与之分叉。
    分开之后诱因就消失了：这些位置一开始就在 state 里，模型看得见，自然只会去
    **改**它而不是新造一个。**移除诱因比禁止更可靠。**

    附带好处：这些路径是命名锚点，能压住键名漂移。
    """

    mode: WorldbookMode = WorldbookMode.ALWAYS
    """是否可展开。**两类条目都有**（D158）。

    `LORE`：`ALWAYS` 全文进上下文，`ON_DEMAND` 只进索引、由模型 `worldinfo_read`。
    **若全部 `LORE` 条目都是 `ALWAYS`，worldinfo 工具不注册**——没有东西可展开，
    留着工具只是白占体量。

    `STATE`：`ALWAYS` 不折叠，`ON_DEMAND` 按 `depth` 折叠、深处由 `state_query` 展开。
    """

    depth: int = 0
    """`STATE` 条目的**展开层级**（D157/D158）：深过这一层的子树只给条数。

    `0` 表示不折叠。只在 `mode` 是 `ON_DEMAND` 时才起作用——常驻的东西没有
    "按需"可言。

    它按条目设而不是全局设，因为同一份 worldbook 里深浅差别很大：一个角色的属性
    树值得给到第二层，而一份地理志给到第一层就够。
    """

    summary: str | None = None
    """由 ``summarize`` 工具懒惰生成并缓存（D69）。

    ``None`` 表示尚未生成——索引回退到"前 N 字 + 字数"，仍满足 D62：
    模型知道有这个东西存在、知道它的体量，只是精度低一点。
    """

    @property
    def is_lore(self) -> bool:
        """进 worldinfo 的那两类：世界设定与角色设定。"""
        return self.kind in (EntryKind.WORLD, EntryKind.CHARACTER)

    def ref_hint(self) -> str:
        return self.id

    def summary_or_head(self, head_chars: int = 120) -> str:
        if self.summary:
            return self.summary
        head = self.content[:head_chars]
        return head + "…" if len(self.content) > head_chars else head
