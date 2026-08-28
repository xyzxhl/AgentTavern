"""检索的判定层（S148）。

**这一层的职责是替模型做完那个判断。**

不给模型附一段「这可能不全，仅供参考」——那是免责声明，不是机制：
模型会忽略它，它每次都占上下文，而且它把判断推回给了模型，
正是 P10 说的反面（**凡是模型的开放判断，都降级成受约束的选择**）。

系统能算出「这次匹配够不够强」，就该算完再给结论。回执只有两种：
**找到了**（那几段 + 位置），或**这次没找到**。
"""

from __future__ import annotations

from pydantic import BaseModel

from .bm25 import BM25Index


class Hit(BaseModel):
    """一条命中。"""

    chunk_index: int
    score: float
    coverage: float
    """query 的词有多大比例落在这一块里。**它是判定用的那个数**——
    不是 BM25 原始分。
    """


def search_corpus(
    index: BM25Index,
    query: str,
    *,
    limit: int,
    min_coverage: float,
) -> list[Hit]:
    """按相关度取前 `limit` 条，**覆盖率不够的一律丢掉**。

    丢掉而不是端上来，是因为**勉强沾边的比没有更糟**：模型拿到几段不相关的
    资料会当成相关的用，写进剧情的就是一段有出处、但出处对不上的东西。
    没找到，它至少知道自己不知道。

    全被丢掉时返回空列表——**由调用方说「这次检索没找到」**，
    而不是「资料里没有」：后者是对资料的断言，这条链路没有资格下。
    """
    out: list[Hit] = []
    for chunk_index, score in index.search(query, limit=limit):
        coverage = index.coverage(query, chunk_index)
        if coverage < min_coverage:
            continue
        out.append(Hit(chunk_index=chunk_index, score=score, coverage=coverage))
    return out
