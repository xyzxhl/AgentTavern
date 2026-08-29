"""检索的判定层（S140 / S148）。

**这一层的职责是替模型做完那个判断。**

不给模型附一段「这可能不全，仅供参考」——那是免责声明，不是机制：
模型会忽略它，它每次都占上下文，而且它把判断推回给了模型，
正是 P10 说的反面（**凡是模型的开放判断，都降级成受约束的选择**）。

系统能算出「这次匹配够不够强」，就该算完再给结论。给模型的回执只有两种：
**找到了**（那几段 + 位置），或**这次没找到**。

降级（向量那一路没用上）**只报给用户**，不进回执——它对模型没有可操作性，
而用户看到它就知道该去配 embeddings 了。
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel, Field

from .bm25 import BM25Index
from .vectors import VectorIndex

K = TypeVar("K")
"""RRF 只按名次融合，键是什么类型它不关心——块序号、ref、都行。"""


class EmbedderLike(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class Hit(BaseModel):
    """一条命中。"""

    chunk_index: int
    text: str = ""

    coverage: float = 0.0
    """query 的词有多大比例落在这一块里。**BM25 侧的判定用它**——不是原始分。"""

    similarity: float = 0.0
    """余弦相似度。向量侧的判定用它。没走向量那一路时是 0。"""

    rrf: float = 0.0
    """RRF 融合分。**排序只看它。**

    早先这里是 `max(coverage, similarity)` 外加一个「两路都命中」的加成——
    那是**把两个不可比的刻度放在一起比**，正是 S148 反对 BM25 原始分当阈值的
    同一条罪。RRF 只看名次，从根上没有这个问题。
    """

    @property
    def both(self) -> bool:
        """两路都命中。**只用于 trace，不参与排序**——RRF 已经把它算进去了
        （两个倒数相加），另设加成等于凭感觉再定一次权重。
        """
        return self.coverage > 0 and self.similarity > 0


class Retrieval(BaseModel):
    """一次检索的全部结果。"""

    hits: list[Hit] = Field(default_factory=list)
    """达标的那几段。**空列表就是「这次没找到」**——由调用方说那句话。"""

    degraded: bool = False
    degrade_reason: str = ""
    """向量那一路没用上，以及为什么。**这两样进 trace 与界面，不进工具回执**（S140）。"""


def rrf(*ranked: dict[K, int], k: int = 60) -> dict[K, float]:
    """Reciprocal Rank Fusion：`score = Σ 1 / (k + 名次)`，名次从 1 起。

    **只看名次，不看分数。** 这是选它的全部理由——BM25 原始分与余弦相似度
    尺度完全不可比，任何加权求和都要先把两者归一化到同一个刻度，
    **而那个刻度没有依据，换一份资料就变**。

    `k` 压平头部：k 越大，第一名相对第二名的优势越小。60 是这类融合的通用取值，
    **这个项目还没有数据支持任何别的值**（P27：先量方差，再改东西）。
    """
    out: dict[K, float] = {}
    for ranking in ranked:
        for key, rank in ranking.items():
            out[key] = out.get(key, 0.0) + 1.0 / (k + rank)
    return out


def search_bm25(
    index: BM25Index, query: str, *, limit: int, min_coverage: float
) -> dict[int, float]:
    """BM25 侧的候选：`{块序号: 覆盖率}`，**覆盖率不够的已经丢掉**。"""
    out: dict[int, float] = {}
    for chunk_index, _score in index.search(query, limit=limit):
        coverage = index.coverage(query, chunk_index)
        if coverage >= min_coverage:
            out[chunk_index] = coverage
    return out


def search_corpus(index: BM25Index, query: str, *, limit: int, min_coverage: float) -> list[Hit]:
    """只走 BM25 的检索。**降级路径就是它**，也用于不需要向量的场合。"""
    return [
        Hit(chunk_index=i, coverage=c)
        for i, c in sorted(
            search_bm25(index, query, limit=limit, min_coverage=min_coverage).items(),
            key=lambda kv: -kv[1],
        )
    ]


async def hybrid_search(
    *,
    query: str,
    bm25: BM25Index,
    vectors: VectorIndex | None,
    embedder: EmbedderLike | None,
    candidates: int,
    limit: int,
    min_coverage: float,
    min_similarity: float,
    rrf_k: int = 60,
    texts: list[str] | None = None,
) -> Retrieval:
    """两路各取候选，按块序号合并去重，**两个阈值都不过的一律丢掉**。

    丢掉而不是端上来，是因为**勉强沾边的比没有更糟**：模型拿到几段不相关的
    资料会当成相关的用，写进剧情的就是一段有出处、但出处对不上的东西。
    没找到，它至少知道自己不知道。
    """
    lexical = search_bm25(bm25, query, limit=candidates, min_coverage=min_coverage)

    semantic: dict[int, float] = {}
    degraded = False
    reason = ""

    if embedder is None or vectors is None or not vectors.count:
        # **没配是常态，不是故障。** 纯 BM25 对专名与术语本来就有相当一部分价值。
        degraded = True
        reason = "没有配置 embeddings，这次只用了关键词匹配"
    else:
        try:
            [vector] = await embedder.embed([query])
            semantic = {
                i: sim
                for i, sim in vectors.search(vector, limit=candidates)
                if sim >= min_similarity
            }
        except Exception as exc:  # noqa: BLE001 — 降级是设计的一部分，不是意外
            # **失败就降级，不是让整条链路报错**：只配了 chat 端点的部署
            # 照样能用上这个功能。
            degraded = True
            reason = f"embeddings 调用失败（{exc}），这次只用了关键词匹配"

    # 每一路各自的名次（各自已经过了自己的阈值）。**名次从 1 起。**
    lex_rank = {i: n for n, i in enumerate(sorted(lexical, key=lambda i: -lexical[i]), 1)}
    sem_rank = {i: n for n, i in enumerate(sorted(semantic, key=lambda i: -semantic[i]), 1)}
    fused = rrf(lex_rank, sem_rank, k=rrf_k)

    merged = [
        Hit(
            chunk_index=i,
            coverage=lexical.get(i, 0.0),
            similarity=semantic.get(i, 0.0),
            rrf=score,
        )
        for i, score in fused.items()
    ]
    # 同分时按块序号——**排序必须是确定的**，否则同一次查询两次跑出不同顺序，
    # 而那种不确定性会混进任何一次前后对照里（P28）。
    hits = sorted(merged, key=lambda h: (-h.rrf, h.chunk_index))
    if texts is not None:
        hits = [
            h.model_copy(update={"text": texts[h.chunk_index]})
            for h in hits
            if 0 <= h.chunk_index < len(texts)
        ]
    return Retrieval(hits=hits[:limit], degraded=degraded, degrade_reason=reason)
