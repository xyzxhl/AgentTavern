"""一次完整的检索：多份资料 → 两路候选 → rerank → 几段原文（S139/S140/S146）。

**判定在这一层做完。** 调用方拿到的只有两种结果：几段原文，或者空——
「这次没找到」那句话由它去说，但**它不需要再判一次**（S148 已经判过了）。

## 多份资料是合起来排的，不是各排各的

一局故事可以挂好几份资料（S136）。每一份各自跑两路检索，**候选汇到一起再
rerank 一次**——分别 rerank 再拼起来的话，每一份都会贡献它自己的前几名，
于是「三份资料」自动变成「三倍的结果」，而其中两份可能根本不相关。

**先不给各份资料分权重**：没有任何数据支持某一种权重（同 P27）。合并之后
它们在 rerank 眼里一视同仁。

## 块序号只在一份资料之内唯一

所以合并之后每条候选都得带上 `corpus_id`——两份资料的第 7 块是两段完全
不同的话，丢了这个字段就再也指不回原文（R42 要的正是"指得回去"）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ..config import LIMITS
from ..models.corpus import Chunk
from .bm25 import BM25Index
from .rerank import GatewayLike, rerank_passages
from .search import EmbedderLike, Hit, hybrid_search
from .vectors import VectorIndex

CANDIDATES = LIMITS.corpus.candidates
TOP_K = LIMITS.corpus.top_k
MIN_COVERAGE = LIMITS.corpus.min_coverage
MIN_SIMILARITY = LIMITS.corpus.min_similarity
RRF_K = LIMITS.corpus.rrf_k


@dataclass(frozen=True)
class Source:
    """一份**已经建好索引、可以查**的资料。

    这一层不碰磁盘：怎么从盘上把它读出来是 `storage/corpora.py` 的事，
    「还在建索引」也在那一侧判——检索这一层只认已经能查的东西。
    """

    corpus_id: str
    name: str
    chunks: list[Chunk]
    bm25: BM25Index
    vectors: VectorIndex | None = None


class Passage(BaseModel):
    """回给模型的一段原文，**带着它在资料里的位置**（R42/S139）。"""

    corpus_id: str
    corpus_name: str
    chunk_index: int
    start: int
    end: int
    text: str


class Lookup(BaseModel):
    """一次检索的结果。**空的 `passages` 就是「这次没找到」。**"""

    query: str
    passages: list[Passage] = Field(default_factory=list)

    degraded: list[str] = Field(default_factory=list)
    """降级理由，一份资料一条。**报给用户，不报给模型**（S140）——
    「这次只用了关键词匹配」改变不了模型下一步做什么，只会多占一份上下文；
    而用户看到它就知道该去配 embeddings 了。
    """

    stages: dict[str, Any] = Field(default_factory=dict)
    """两个阶段的全过程，进 trace（S146）。

    **rerank 前后的次序都要记**：它到底有没有起作用，是这条链路第一个要量的东西。
    """


@dataclass
class _Candidate:
    hit: Hit
    source: Source
    chunk: Chunk


async def look_up(
    sources: Sequence[Source],
    *,
    query: str,
    gateway: GatewayLike,
    embedder: EmbedderLike | None = None,
    candidates: int = CANDIDATES,
    top_k: int = TOP_K,
    min_coverage: float = MIN_COVERAGE,
    min_similarity: float = MIN_SIMILARITY,
    rrf_k: int = RRF_K,
) -> Lookup:
    """查一遍。**没有一条达标就返回空的 `passages`**，不返回勉强沾边的。"""
    pool: list[_Candidate] = []
    degraded: list[str] = []
    paths: list[dict[str, Any]] = []

    for source in sources:
        texts = [c.text for c in source.chunks]
        retrieval = await hybrid_search(
            query=query,
            bm25=source.bm25,
            vectors=source.vectors,
            embedder=embedder,
            # **这一步取 `candidates` 条，不是 `top_k`**：截断留给 rerank。
            # 在这里就砍到 5 条的话，rerank 只能在已经排好的 5 条里换顺序，
            # 它本来要做的事（把第 12 条提到第 1 位）就永远发生不了。
            candidates=candidates,
            limit=candidates,
            min_coverage=min_coverage,
            min_similarity=min_similarity,
            rrf_k=rrf_k,
            texts=texts,
        )
        if retrieval.degraded:
            degraded.append(f"「{source.name}」：{retrieval.degrade_reason}")
        paths.append(
            {
                "corpus_id": source.corpus_id,
                "name": source.name,
                "lexical": retrieval.lexical,
                "semantic": retrieval.semantic,
                "degraded": retrieval.degraded,
                "reason": retrieval.degrade_reason,
            }
        )
        for hit in retrieval.hits:
            pool.append(
                _Candidate(
                    hit=hit.model_copy(update={"corpus_id": source.corpus_id}),
                    source=source,
                    chunk=source.chunks[hit.chunk_index],
                )
            )

    stages: dict[str, Any] = {"query": query, "paths": paths}

    if not pool:
        stages["before_rerank"] = []
        stages["after_rerank"] = []
        return Lookup(query=query, degraded=degraded, stages=stages)

    # 合起来按融合分排一次，rerank 拿到的就是这个顺序——**记下来**，
    # 才比得出 rerank 改动了什么（S146）。
    pool.sort(key=lambda c: (-c.hit.rrf, c.source.corpus_id, c.hit.chunk_index))
    stages["before_rerank"] = [_where(c) for c in pool]

    ordered = await rerank_passages(
        gateway, query=query, candidates=[c.hit for c in pool], limit=top_k
    )
    # **按 (资料, 块序号) 认回来，不按对象身份。** rerank 现在返回的就是传进去
    # 的那几个对象，但那是它的实现，不是它的契约——哪天它改成返回副本，
    # 按 `id()` 认就会当场认不出来。
    by_key = {(c.hit.corpus_id, c.hit.chunk_index): c for c in pool}
    picked = [by_key[(h.corpus_id, h.chunk_index)] for h in ordered]
    stages["after_rerank"] = [_where(c) for c in picked]

    return Lookup(
        query=query,
        passages=[
            Passage(
                corpus_id=c.source.corpus_id,
                corpus_name=c.source.name,
                chunk_index=c.chunk.index,
                start=c.chunk.start,
                end=c.chunk.end,
                text=c.chunk.text,
            )
            for c in picked
        ],
        degraded=degraded,
        stages=stages,
    )


def _where(c: _Candidate) -> list[Any]:
    return [c.source.corpus_id, c.hit.chunk_index]


__all__ = ["Lookup", "Passage", "Source", "look_up"]
