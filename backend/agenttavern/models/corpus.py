"""外部资料：一份别人写的参考素材（S136）。

**它和 worldbook 是两回事**，三点上全都不同——谁写（用户 vs 别人）、
多大（一条几百字 vs 一份几十万字）、什么时候进上下文（可能每轮在场 vs
**只在被查到时**）。塞进 worldbook 会撑爆「条目正文 → worldinfo 每轮注入」
那条投影，得为它开特例。

**这一层是尽力而为的，而且是原理上如此**（S139）：用户给的资料无法保证有结构，
装不下一份完整的目录；能不能找到取决于模型这一次的 query 问得对不对。
所以每一处返回都必须**说出自己不全**——静默的缺失比缺失本身更糟（P34）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from .node import new_id


class BuildState(StrEnum):
    """索引建到哪一步了（S144）。

    **三种状态都要能被说出来**：查一份还在建的资料，回执得写「还在建」，
    不能返回空列表——空会被读成「资料里没有」。
    """

    PENDING = "pending"
    """刚导入，还没开始建。"""

    BUILDING = "building"
    """正在建。带进度——一本三十万字的小说要花分钟级的时间。"""

    READY = "ready"
    """可以查了。"""

    FAILED = "failed"
    """建失败。**失败要看得见，不能静默停在 building 上**——
    那样用户会一直等一个永远不会好的东西。
    """


class BuildStatus(BaseModel):
    state: BuildState = BuildState.PENDING

    chunks_done: int = 0
    chunks_total: int = 0
    """进度。`total` 在切块之后才知道，所以两个都从 0 开始。"""

    embedded: bool = False
    """向量那一路建成了没有。

    **它可以为假而状态仍是 READY**——embeddings 没配或调用失败时整条链路
    退回纯 BM25（S140）。降级是有损的，所以检索回执里要说明；
    但**降级不是拒绝启用**：纯 BM25 对专名与术语本来就有相当一部分价值。
    """

    error: str = ""
    """**哪里不对劲**，给人读的一句话，不是堆栈。**严重程度看 `state`，不看这里**：

    | `state` | 有 `error` 意味着 |
    |---|---|
    | `FAILED` | 建砸了，这份资料查不了 |
    | `READY` | **能查，但降级了**——向量那一路没建成，只能按关键词查（S140） |

    两种情况共用一个字段是有意的：它们要说的是同一件事——「这份资料哪里不
    完全正常」，而界面按 `state` 决定标红还是标黄。分成两个近乎同义的字段，
    只会让人写代码时挑错那一个。
    """

    @property
    def searchable(self) -> bool:
        return self.state is BuildState.READY


class Chunk(BaseModel):
    """切出来的一块，**记着它在原文里的位置**（S142）。

    位置不是可选的：检索结果要指得回原文，用户才判得出这条链路准不准（R42）。
    """

    index: int
    """在这份资料里的序号。它就是块的 id——按需寻址靠它。"""

    start: int
    end: int
    """在原文里的字符区间，左闭右开。`text[start:end]` 必须还原出 `text`。"""

    text: str


class Corpus(BaseModel):
    """一份资料的元信息。**正文不在这里**——它单独落一个纯文本文件（S143）。"""

    corpus_id: str = Field(default_factory=lambda: new_id("cp"))
    name: str = "未命名资料"

    note: str = ""
    """给人看的一句话：这是什么、从哪来。不进模型上下文。"""

    chars: int = 0
    """原文多少字。**给用户看的**：列表里一份资料有多大，导入建索引要等多久。

    **它不进检索回执**（S139）。曾经要求每次都报「从 32 万字里挑出这 5 段」，
    理由是"模型据此判断自己拿到的东西全不全"——**那个理由站不住**：模型拿这个
    数做不了任何事，它既不能因此改写 query，也不能因此少写一段。
    而 S139 定死了回执只有两种形状，多出来的每一句都在跟正事抢注意力（P32）。
    """

    build: BuildStatus = Field(default_factory=BuildStatus)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
