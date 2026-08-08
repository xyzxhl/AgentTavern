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


class WorldbookEntry(BaseModel):
    id: str = Field(default_factory=lambda: new_id("wi"))
    name: str
    content: str = ""

    mode: WorldbookMode = WorldbookMode.ALWAYS
    """**若全部条目都是 ALWAYS，worldinfo 工具不注册**——没有任何东西可展开，
    留着工具只是白占 prompt 体量。
    """

    seeds: dict[str, Any] = Field(default_factory=dict)
    """该条目附带的**初始 state**，session 开始时由代码迁移进 state。

    设定条目常常是混的：角色的性格、说话习惯、背景是**静态的**（属 worldinfo，
    不因剧情改变）；而它现在在哪、和谁什么关系、知道什么，是**会变的**（属 state）。
    没有这个字段时，模型只能把后者从设定里**抄**进 state——而副本会在设定被改动后
    与之分叉。

    有了种子，诱因本身就消失了：这些键一开始就在 state 默认包里，模型看得见，
    自然只会去**改**它而不是新造一个。**移除诱因比禁止更可靠。**

    附带好处：种子键是命名锚点，能压住键名漂移。
    """

    summary: str | None = None
    """由 ``summarize`` 工具懒惰生成并缓存（D69）。

    ``None`` 表示尚未生成——索引回退到"前 N 字 + 字数"，仍满足 D62：
    模型知道有这个东西存在、知道它的体量，只是精度低一点。
    """

    def ref_hint(self) -> str:
        return self.id

    def summary_or_head(self, head_chars: int = 120) -> str:
        if self.summary:
            return self.summary
        head = self.content[:head_chars]
        return head + "…" if len(self.content) > head_chars else head
