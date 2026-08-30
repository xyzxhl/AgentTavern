"""建索引：切块 → BM25 → 向量 →（可查）（S143 / S144）。

## 哪些失败算失败

**只有 BM25 那一路失败才算建失败。**

BM25 不依赖任何外部模型，它成了这份资料就能查——所以 embeddings 那一路挂掉时，
状态仍然是 `READY`，只是 `embedded=False`，检索退回纯 BM25（[S140]）。
**降级不是拒绝启用。**

把 embedding 失败当成建失败，后果是**一次网络抖动让整份资料变成不可用**，
而它本来有一半能用。

## 为什么要分批

一份三十万字的小说切出几千块，一次性发给 embeddings 端点会被拒（各家都有
每次的条数与总 token 上限）。分批还顺带给了进度：**一本书要建几分钟，
没有进度的等待和卡死分不出来**（S144）。

## 进程中途死掉

状态会永远停在 `BUILDING` 上。**那比失败更糟**——用户会一直等一个不会好的东西。
所以启动时扫一遍，把没人在建的 `BUILDING` 标成 `FAILED`（见 `sweep_stale`）。
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import LIMITS
from ..models.corpus import BuildState, BuildStatus, Corpus
from ..storage.corpora import CorpusRepository, CorpusStore
from .bm25 import BM25Index
from .chunking import chunk
from .search import EmbedderLike
from .vectors import VectorIndex

EMBED_BATCH = LIMITS.corpus.embed_batch

OnProgress = Callable[[Corpus], None]
"""每推进一步叫一次。**给 UI 用**——它要的是"到哪儿了"，不是"完了没有"。"""


async def build_index(
    repo: CorpusRepository,
    *,
    embedder: EmbedderLike | None = None,
    chunk_chars: int = LIMITS.corpus.chunk_chars,
    overlap: int = LIMITS.corpus.chunk_overlap,
    batch: int = EMBED_BATCH,
    on_progress: OnProgress | None = None,
) -> Corpus:
    """把一份资料建成可检索的。**失败也落盘**，不抛给调用方。

    返回最终的 `Corpus`——`build.state` 是 `READY` 或 `FAILED`。
    """

    def step(status: BuildStatus) -> Corpus:
        corpus = repo.set_build(status)
        if on_progress is not None:
            on_progress(corpus)
        return corpus

    step(BuildStatus(state=BuildState.BUILDING))

    try:
        text = repo.text()
        chunks = chunk(text, size=chunk_chars, overlap=overlap)
        repo.save_chunks(chunks)
        total = len(chunks)
        step(BuildStatus(state=BuildState.BUILDING, chunks_total=total))

        # BM25 是本地计算，**没有分批的必要，也没有失败的外部原因**。
        repo.save_bm25(BM25Index.build([c.text for c in chunks]))
    except Exception as exc:  # noqa: BLE001 —— 什么样的坏数据都算建失败
        # **失败要看得见**（S144）：静默停在 building 上的话，用户会一直等。
        return step(BuildStatus(state=BuildState.FAILED, error=_reason(exc)))

    if embedder is None:
        # 没配 embeddings 是常态，不是故障——纯 BM25 对专名与术语本来就有价值。
        return step(BuildStatus(state=BuildState.READY, chunks_total=total, chunks_done=total))

    texts = [c.text for c in chunks]
    vectors: list[list[float]] = []
    try:
        for start in range(0, total, max(1, batch)):
            vectors.extend(await embedder.embed(texts[start : start + batch]))
            step(
                BuildStatus(
                    state=BuildState.BUILDING,
                    chunks_total=total,
                    chunks_done=min(start + batch, total),
                )
            )
        VectorIndex.build(vectors).dump(repo.vectors_path)
    except Exception as exc:  # noqa: BLE001 —— 向量那一路挂掉**不算建失败**
        # **这份资料仍然可查**（S140）：BM25 已经建好了。把半截向量删掉——
        # 留着的话下次读到的是一个条数对不上的矩阵，而那会在检索时才炸。
        repo.vectors_path.unlink(missing_ok=True)
        return step(
            BuildStatus(
                state=BuildState.READY,
                chunks_total=total,
                chunks_done=total,
                embedded=False,
                # **降级理由留着给用户看**，不进模型的回执（S140）。
                error=f"向量索引没建成，这份资料只能按关键词查：{_reason(exc)}",
            )
        )

    return step(
        BuildStatus(state=BuildState.READY, chunks_total=total, chunks_done=total, embedded=True)
    )


def _reason(exc: Exception) -> str:
    """给人读的一句话，不是堆栈（`BuildStatus.error` 的约定）。"""
    text = str(exc).strip() or exc.__class__.__name__
    return text[:300]


def sweep_stale(store: CorpusStore) -> list[str]:
    """把**没人在建**的 `BUILDING` 标成 `FAILED`。返回改了哪几份。

    进程在建索引途中被杀（改代码重启、机器关机）时，状态会永远停在 `BUILDING`。
    **那比失败更糟**：失败至少看得见，而永远的"建索引中 47%"会让用户一直等
    一个不会好的东西（S144）。

    **启动时跑一次就够**：进程内正在建的那些，此刻还不存在。
    """
    fixed: list[str] = []
    for corpus in store.survey()[0]:
        if corpus.build.state is not BuildState.BUILDING:
            continue
        store.repo(corpus.corpus_id).set_build(
            corpus.build.model_copy(
                update={
                    "state": BuildState.FAILED,
                    "error": "上次建索引没跑完（服务重启了）。重建一次即可。",
                }
            )
        )
        fixed.append(corpus.corpus_id)
    return fixed


__all__ = ["build_index", "sweep_stale"]
