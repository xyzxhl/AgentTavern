"""专业 reranker 的统一接口（S141）。

**不打真实网络**——四家各用一个 `MockTransport` 替身，同 gateway 那侧。

这一份钉的是**每一家形状上的那点不同**：URL、请求体、结果在响应的哪一层。
那三处是这个接口存在的全部理由，写错了不会报错——只会返回空结果，
而空结果读起来就是「这次没找到」（同 S139 那条罪）。
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest

from agenttavern.config import Settings
from agenttavern.corpus.rerankers import (
    PROVIDERS,
    CohereReranker,
    HttpReranker,
    JinaReranker,
    QwenReranker,
    RerankError,
    Scored,
    VoyageReranker,
    reranker_from_settings,
)

DOCS = ["尚书省掌管六部政令。", "中书省负责起草诏令。", "门下省负责审核封驳。"]

ITEMS = [{"index": 2, "relevance_score": 0.91}, {"index": 0, "relevance_score": 0.42}]
"""结果项**四家一致**，所以只写一份——下面那张表的差别全在外面那层壳上。"""

SHAPES: dict[str, dict[str, Any]] = {
    "cohere": {"results": ITEMS},
    "jina": {"results": ITEMS},
    "voyage": {"data": ITEMS},
    # DashScope 还多包一层，外加两个我们用不上的字段。
    "qwen": {"output": {"results": ITEMS}, "usage": {"total_tokens": 42}, "request_id": "r1"},
}


class Recorder:
    """记下它收到的那一个请求，并按那一家的形状回话。"""

    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.url = ""
        self.body: dict[str, Any] = {}
        self.headers: dict[str, str] = {}

    def client(self) -> httpx2.AsyncClient:
        import json

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.url = str(request.url)
            self.headers = dict(request.headers)
            self.body = json.loads(request.content)
            return httpx2.Response(self.status, json=self.payload)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def build(name: str, recorder: Recorder, **kw: Any) -> HttpReranker:
    return PROVIDERS[name](api_key="sk-test", model="m", http_client=recorder.client(), **kw)


# -- 四家共同的那一面 ----------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_every_provider_returns_the_same_shape(name: str) -> None:
    """**四家出来的东西必须一模一样**——这就是这个接口存在的理由。

    调用方（`lookup.py`）不该知道自己接的是哪一家；知道了就会在某处写一个
    `if provider == ...`，而那正是接口要消掉的东西。
    """
    rec = Recorder(SHAPES[name])
    out = await build(name, rec).rerank("尚书省", DOCS, top_n=2)

    assert [s.index for s in out] == [2, 0]
    assert [round(s.score, 2) for s in out] == [0.91, 0.42]
    assert all(isinstance(s, Scored) for s in out)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_every_provider_stamps_who_scored_it(name: str) -> None:
    """**分数带着是谁给的**（S148 的教训换个地方又来了）。

    Cohere 的 0.5 和 Voyage 的 0.5 不是一回事：各家的标定与训练目标都不同。
    任何阈值都必须绑着 `provider`——换一家而阈值照抄，整条链路的松紧就变了，
    **而没有任何地方会报错**。
    """
    rec = Recorder(SHAPES[name])
    out = await build(name, rec).rerank("尚书省", DOCS, top_n=2)
    assert {s.provider for s in out} == {name}


@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_every_provider_authenticates_the_same_way(name: str) -> None:
    """四家都是 `Authorization: Bearer`——这一点上没有分歧，所以在基类里。"""
    rec = Recorder(SHAPES[name])
    await build(name, rec).rerank("q", DOCS, top_n=2)
    assert rec.headers["authorization"] == "Bearer sk-test"


@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_no_documents_makes_no_request(name: str) -> None:
    """一条候选都没有时**不发这次调用**——它要花钱，而排序空列表没有意义。"""
    rec = Recorder(SHAPES[name])
    assert await build(name, rec).rerank("q", [], top_n=5) == []
    assert rec.url == "", "不该发出请求"


# -- 每一家形状上的那点不同 ----------------------------------------------------


async def test_cohere_and_jina_share_the_flat_shape() -> None:
    """Jina 的接口**是照着 Cohere 做的**，所以除了 URL 什么都不用改。

    这条把「它们真的一样」钉住：哪天某一家改了形状，这里会红，
    而不是悄悄返回空结果。
    """
    co, ji = Recorder(SHAPES["cohere"]), Recorder(SHAPES["jina"])
    await CohereReranker(api_key="k", model="rerank-v3.5", http_client=co.client()).rerank(
        "尚书省", DOCS, top_n=2
    )
    await JinaReranker(api_key="k", model="jina-reranker-v2", http_client=ji.client()).rerank(
        "尚书省", DOCS, top_n=2
    )

    assert co.url == "https://api.cohere.com/v2/rerank"
    assert ji.url == "https://api.jina.ai/v1/rerank"
    for body in (co.body, ji.body):
        assert body["query"] == "尚书省"
        assert body["documents"] == DOCS
        assert body["top_n"] == 2, "两家都是 top_n"


async def test_voyage_says_top_k_not_top_n() -> None:
    """**Voyage 是 `top_k`，结果在 `data` 里。**

    传成 `top_n` 它不报错，只是把这个参数当没看见，于是每次都返回全部候选——
    症状是"变慢了"，不是"出错了"（同 P29）。
    """
    rec = Recorder(SHAPES["voyage"])
    out = await VoyageReranker(api_key="k", model="rerank-2", http_client=rec.client()).rerank(
        "尚书省", DOCS, top_n=2
    )

    assert rec.url == "https://api.voyageai.com/v1/rerank"
    assert rec.body["top_k"] == 2
    assert "top_n" not in rec.body
    assert [s.index for s in out] == [2, 0], "结果在 data 里，不是 results"


async def test_qwen_nests_input_and_parameters() -> None:
    """**DashScope 的原生形状**：`input` / `parameters` 两层，结果在 `output.results`。

    它**不在 `compatible-mode` 那条路上**——rerank 没有 OpenAI 兼容面，
    所以 base_url 和 embeddings 那条不一样。
    """
    rec = Recorder(SHAPES["qwen"])
    out = await QwenReranker(api_key="k", model="gte-rerank-v2", http_client=rec.client()).rerank(
        "尚书省", DOCS, top_n=2
    )

    assert rec.url.endswith("/api/v1/services/rerank/text-rerank/text-rerank")
    assert "compatible-mode" not in rec.url
    assert rec.body["input"] == {"query": "尚书省", "documents": DOCS}
    assert rec.body["parameters"]["top_n"] == 2
    # 原文我们手上已经有了，让它回传一遍只是把响应体撑大一倍。
    assert rec.body["parameters"]["return_documents"] is False
    assert [s.index for s in out] == [2, 0]


# -- 坏答案 --------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROVIDERS))
async def test_a_non_200_raises_rerank_error(name: str) -> None:
    """**报错要说清是哪一家、什么码。**

    大区填错、模型名写错，症状都长这样，而这两样是排查时唯一有用的信息。
    抛 `RerankError` 而不是返回空：空会被读成"这次没找到"，
    而这次根本没查成（同 S139）。
    """
    rec = Recorder({"error": "Incorrect API key"}, status=401)
    with pytest.raises(RerankError) as caught:
        await build(name, rec).rerank("q", DOCS, top_n=2)
    assert name in str(caught.value) and "401" in str(caught.value)


async def test_a_network_failure_raises_rerank_error() -> None:
    """连不上同样是 `RerankError`——调用方只需要认一种异常。"""

    def handler(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("端点不可达")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    reranker = CohereReranker(api_key="k", model="m", http_client=client)
    with pytest.raises(RerankError):
        await reranker.rerank("q", DOCS, top_n=2)


async def test_an_out_of_range_index_is_dropped_not_renumbered() -> None:
    """越界的下标**丢掉，不补位**。

    丢而不是报错：一条坏记录不该让整次检索失败。但凭空造一个下标出来会让
    引用指向不相干的段落——**而那种错不会报错，只会让引文对不上原文**。
    """
    rec = Recorder(
        {"results": [{"index": 99, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.5}]}
    )
    out = await build("cohere", rec).rerank("q", DOCS, top_n=3)
    assert [s.index for s in out] == [1]


async def test_results_are_sorted_by_score_even_if_the_endpoint_did_not() -> None:
    """**自己再排一次。**

    各家都说按分数降序返回，但那是它们的实现不是契约。顺序错了的后果是
    "最相关的那段排在第三条"，不会报错。
    """
    rec = Recorder(
        {"results": [{"index": 0, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.8}]}
    )
    out = await build("cohere", rec).rerank("q", DOCS, top_n=3)
    assert [s.index for s in out] == [1, 0]


async def test_a_garbled_item_is_skipped_not_fatal() -> None:
    """少字段、类型不对的那一条跳过，其余照用。"""
    rec = Recorder(
        {
            "results": [
                {"index": 0},  # 少了分数
                {"relevance_score": 0.5},  # 少了下标
                {"index": 1, "relevance_score": "高"},  # 分数不是数
                {"index": 2, "relevance_score": 0.7},  # 好的那一条
            ]
        }
    )
    out = await build("cohere", rec).rerank("q", DOCS, top_n=3)
    assert [s.index for s in out] == [2]


# -- 从 .env 建 ----------------------------------------------------------------


def test_no_provider_configured_means_no_reranker() -> None:
    """**没配就是 `None`，不是故障**（同 embeddings 那条）。

    那时链路**根本不 rerank**：按融合次序取前几条，不发任何模型调用（S141）。
    """
    assert reranker_from_settings(Settings(llm_model="m")) is None
    # 只填了家数没填模型，同样不启用：**不去猜一个模型名**。
    assert reranker_from_settings(Settings(llm_model="m", rerank_provider="cohere")) is None


def test_an_unknown_provider_is_refused_at_once() -> None:
    """名字写错**当场报错，不猜也不回落**。

    回落的后果是每次检索都对着错的端点报错，而症状只是"检索总是查不到"——
    一个没生效的配置不该悄悄没生效（P29）。
    """
    settings = Settings(llm_model="m", rerank_provider="cohre", rerank_model="m")
    with pytest.raises(ValueError, match="cohre"):
        reranker_from_settings(settings)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_each_provider_builds_from_settings(name: str) -> None:
    built = reranker_from_settings(
        Settings(llm_model="m", rerank_provider=name, rerank_model="rr", rerank_api_key="k")
    )
    assert isinstance(built, PROVIDERS[name])
    assert built.model == "rr"


def test_a_custom_base_url_wins_over_the_official_one() -> None:
    """自建与代理要能接——同 `llm_base_url` 那条。"""
    built = reranker_from_settings(
        Settings(
            llm_model="m",
            rerank_provider="qwen",
            rerank_model="rr",
            rerank_base_url="http://127.0.0.1:8000/",
        )
    )
    assert built is not None
    assert built._base == "http://127.0.0.1:8000", "末尾的斜杠要去掉，否则拼出双斜杠"
