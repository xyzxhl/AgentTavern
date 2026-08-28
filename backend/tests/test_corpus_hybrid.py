"""混合检索：向量那一路、合并去重、降级（S140 / S143 / S148）。

**不打真实网络**——embeddings 用一个替身注入。这和 gateway 那侧用
`MockTransport` 是同一条理由：产品跑的那条路要被测到，但不能靠联网。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttavern.config import Settings
from agenttavern.corpus.bm25 import BM25Index
from agenttavern.corpus.embed import Embedder
from agenttavern.corpus.search import hybrid_search
from agenttavern.corpus.vectors import VectorIndex

# -- 替身 ---------------------------------------------------------------------


class FakeEmbedder:
    """按关键词造向量：含某个词就在对应维度上打 1。

    **不是随机数**——随机向量下"相似"没有含义，测出来的东西也就没有含义。
    """

    def __init__(self, axes: list[str], *, fails: bool = False) -> None:
        self.axes = axes
        self.fails = fails
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fails:
            raise RuntimeError("端点不可用")
        return [[1.0 if a in t else 0.0 for a in self.axes] for t in texts]


# -- S143 向量落盘 -------------------------------------------------------------


def test_vectors_survive_a_round_trip_through_disk(tmp_path: Path) -> None:
    """向量要能存下来再读回去，否则每次起服务都得重新 embed 一遍——那是要花钱的。"""
    index = VectorIndex.build([[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]])
    path = tmp_path / "vectors.npy"
    index.dump(path)

    again = VectorIndex.load(path)
    assert again.count == 3
    assert again.search([1.0, 0.0], limit=1)[0][0] == 0


def test_cosine_is_normalised_so_length_does_not_win(tmp_path: Path) -> None:
    """**用余弦不用点积**：点积会让长向量无脑胜出，而长度和相关性无关。"""
    index = VectorIndex.build([[10.0, 0.0], [0.0, 1.0]])
    ranked = dict(index.search([0.0, 1.0], limit=2))
    assert ranked[1] == pytest.approx(1.0), "方向一致就该是 1，不管长度"
    assert ranked[0] == pytest.approx(0.0)


def test_an_empty_index_returns_nothing_rather_than_raising(tmp_path: Path) -> None:
    """没建向量的资料要能被查（走降级），不能在这里炸掉整条链路。"""
    assert VectorIndex.build([]).search([1.0], limit=3) == []


# -- S140 混合与去重 -----------------------------------------------------------


@pytest.mark.asyncio
async def test_both_paths_contribute_and_duplicates_collapse() -> None:
    """两路各出一份候选，**按块序号合并去重**（S140）。

    两者的强项互不重叠：BM25 抓专名，向量抓「说法不同但意思相近」。
    """
    texts = [
        "尚书省左仆射掌管吏部铨选。",  # BM25 能命中「尚书省」
        "朝廷的人事任免由这个衙门负责。",  # 只有向量能命中
        "今日天气晴朗。",
    ]
    embedder = FakeEmbedder(["尚书省", "人事任免"])
    vectors = VectorIndex.build(await embedder.embed(texts))

    result = await hybrid_search(
        query="尚书省 人事任免",
        bm25=BM25Index.build(texts),
        vectors=vectors,
        embedder=embedder,
        candidates=5,
        limit=5,
        min_coverage=0.0,
        min_similarity=0.1,
    )
    got = {h.chunk_index for h in result.hits}
    assert got == {0, 1}, f"两路的命中都要在，且不重复：{got}"
    assert len(result.hits) == len(got), "同一块被两路都命中时只能出现一次"


@pytest.mark.asyncio
async def test_a_chunk_found_by_both_paths_ranks_above_one_found_by_one() -> None:
    """两路都命中是**比任何单路都强的信号**——合并时要体现出来。"""
    texts = ["尚书省掌管吏部。", "尚书省。"]
    embedder = FakeEmbedder(["吏部"])
    result = await hybrid_search(
        query="尚书省 吏部",
        bm25=BM25Index.build(texts),
        vectors=VectorIndex.build(await embedder.embed(texts)),
        embedder=embedder,
        candidates=5,
        limit=5,
        min_coverage=0.0,
        min_similarity=0.1,
    )
    assert result.hits[0].chunk_index == 0, f"两路都命中的那块该排前面：{result.hits}"


# -- S140 降级 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_it_falls_back_to_bm25_when_embeddings_are_unavailable() -> None:
    """embeddings 调用失败 → **退回纯 BM25，不是整条链路报错**（S140）。

    只配了 chat 端点的部署照样能用上这个功能——纯 BM25 对专名与术语本来就
    有相当一部分价值。
    """
    texts = ["尚书省左仆射掌管吏部铨选。", "今日天气晴朗。"]
    result = await hybrid_search(
        query="尚书省 吏部",
        bm25=BM25Index.build(texts),
        vectors=VectorIndex.build([[1.0], [0.0]]),
        embedder=FakeEmbedder(["x"], fails=True),
        candidates=5,
        limit=5,
        min_coverage=0.5,
        min_similarity=0.1,
    )
    assert [h.chunk_index for h in result.hits] == [0]
    assert result.degraded and "端点不可用" in result.degrade_reason


@pytest.mark.asyncio
async def test_no_embedder_configured_is_degradation_not_an_error() -> None:
    """没配 embeddings 是**常态**，不是故障：`embedder=None` 直接走纯 BM25。"""
    result = await hybrid_search(
        query="尚书省",
        bm25=BM25Index.build(["尚书省掌管吏部。", "天气晴朗。"]),
        vectors=None,
        embedder=None,
        candidates=5,
        limit=5,
        min_coverage=0.5,
        min_similarity=0.1,
    )
    assert [h.chunk_index for h in result.hits] == [0]
    assert result.degraded


@pytest.mark.asyncio
async def test_the_degradation_never_reaches_the_model() -> None:
    """**降级报给用户，不报给模型**（S140）。

    「这次只用了关键词匹配」对模型没有可操作性——它改变不了模型下一步做什么，
    只会多占一份上下文。而用户看到它就知道该去配 embeddings 了。
    """
    result = await hybrid_search(
        query="尚书省",
        bm25=BM25Index.build(["尚书省掌管吏部。"]),
        vectors=None,
        embedder=None,
        candidates=5,
        limit=5,
        min_coverage=0.5,
        min_similarity=0.1,
    )
    assert result.degraded, "降级这件事本身要记下来"
    assert not any("降级" in h.text or "关键词" in h.text for h in result.hits), (
        "降级说明不能混进给模型的那几段里"
    )


# -- S148 向量那一侧的阈值 -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_low_similarity_hit_is_dropped() -> None:
    """向量侧同样有阈值：**方向差得远的不算命中**（S148）。"""
    texts = ["甲", "乙"]
    embedder = FakeEmbedder(["甲", "乙"])
    result = await hybrid_search(
        query="丙",  # 造出来的向量是零向量，和谁都不像
        bm25=BM25Index.build(texts),
        vectors=VectorIndex.build(await embedder.embed(texts)),
        embedder=embedder,
        candidates=5,
        limit=5,
        min_coverage=1.0,
        min_similarity=0.3,
    )
    assert result.hits == [], "两个阈值都不过，就该判为没找到"


# -- 配置：独立的一组 ----------------------------------------------------------


def test_embeddings_have_their_own_endpoint_config() -> None:
    """**embeddings 走独立的一组配置**（S140）——多数人的 chat 与 embedding
    未必在同一家。
    """
    s = Settings(
        llm_base_url="https://chat.example/v1",
        embed_base_url="https://embed.example/v1",
        embed_model="bge-m3",
    )
    assert s.embed_base_url != s.llm_base_url
    assert Embedder.from_settings(s) is not None


def test_no_embed_model_means_no_embedder() -> None:
    """**没填模型名就是不启用向量那一路**——不是拿 chat 的模型名去猜。"""
    assert Embedder.from_settings(Settings(embed_model="")) is None
