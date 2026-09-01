"""专业 reranker 端点的统一接口（S141）。

OpenAI SDK **没有 rerank 这个面**，各家自己定的形状，所以这里自己定一个小接口。

## 四家的差别只有三处

结果项的形状**四家完全一致**：`{"index": i, "relevance_score": s}`。
不一样的只有外面那层壳——

| | 请求体 | 结果在哪 |
|---|---|---|
| **Cohere** `/v2/rerank` | 平铺，`top_n` | `results` |
| **Jina** `/v1/rerank` | 平铺，`top_n` | `results` |
| **Voyage** `/v1/rerank` | 平铺，**`top_k`** | **`data`** |
| **Qwen** `/api/v1/services/rerank/…` | **`input`/`parameters` 两层** | **`output.results`** |

所以基类管 HTTP、鉴权、报错、解析，每家只覆盖三个钩子，各自五行。

## 为什么这不违反 P10

[P10] 说「模型的开放判断一律降级为受约束的选择」，S141 据此判过
**「给它候选让它排序，不问这一段几分」**——那条针对的是**拿 chat 模型当
reranker**：让一个通用模型自由打分，分数从哪来无从追究，是典型的开放判断。

**cross-encoder 的分数不是那种东西。** 它是一个专门训练来估相关度的模型的
输出，是**测量**，不是它临场的意见。所以这里收下分数，而分数正是那件
排序做不到的事：**它让「一条都不合适」成为可能**。

## 分数**不可跨家比较**

这是 [S148] 已经吃过一次的亏（BM25 原始分不能当阈值），换个地方又出现：
Cohere 的 0.5 和 Voyage 的 0.5 不是一回事——各家的标定、训练目标、
分数域都不同，有的还随 query 长度漂。

所以 `Scored` 带着 `provider`：**任何阈值都必须绑着它是谁给的**，
换一家而阈值照抄，整条链路的松紧就变了，**而没有任何地方会报错**。
"""

from __future__ import annotations

from typing import Any, Protocol

import httpx2
from pydantic import BaseModel

from ..config import Settings


class RerankError(RuntimeError):
    """这次 rerank 没做成。

    **调用方应当降级，不是让整条检索失败**——rerank 是锦上添花的一步，
    它坏掉时链路该退化成"没 rerank"（同 S141）。
    """


class Scored(BaseModel):
    """一条候选的相关度。"""

    index: int
    """在**传进去那一批**里的下标。不是块序号——调用方自己映射回去。"""

    score: float
    """相关度。**只在同一家、同一个模型之内可比**（见模块头）。"""

    provider: str = ""
    """哪一家给的分。**阈值必须绑着它**：换一家而阈值照抄，
    松紧就变了，且没有任何地方会报错。
    """


class Reranker(Protocol):
    """一次 rerank。**四家共同的最小面。**"""

    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[Scored]:
        """按相关度**从高到低**返回，最多 `top_n` 条。

        返回的条数可以少于 `top_n`（各家都可能自己截断），但**不会多**。
        """
        ...

    async def aclose(self) -> None: ...


class HttpReranker:
    """四家共用的 HTTP 壳：鉴权、超时、报错、把结果项解析成 `Scored`。

    子类只覆盖三样：`path` / `_body` / `_results`。
    """

    provider = ""
    path = ""
    default_base_url = ""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "",
        timeout: float = 30.0,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._key = api_key
        self._base = (base_url or self.default_base_url).rstrip("/")
        # `http_client` 是**测试注入网络替身的唯一入口**（`MockTransport`），
        # 同 `LLMGateway`。类型跟着 openai 3.x 走：`httpx2`，不是 `httpx`。
        self._owned = http_client is None
        self._client = http_client or httpx2.AsyncClient(timeout=timeout)

    # -- 三个钩子 -----------------------------------------------------------

    def _body(self, query: str, documents: list[str], top_n: int) -> dict[str, Any]:
        """平铺、`top_n`、`documents`——Cohere 与 Jina 的形状，也是默认形状。"""
        return {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }

    def _results(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """结果列表在响应的哪一层。"""
        return list(payload.get("results") or [])

    # -- 共用的那一半 -------------------------------------------------------

    async def rerank(self, query: str, documents: list[str], *, top_n: int) -> list[Scored]:
        if not documents:
            return []
        try:
            response = await self._client.post(
                f"{self._base}{self.path}",
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
                json=self._body(query, documents, top_n),
            )
        except Exception as exc:  # noqa: BLE001 —— 网络层什么都可能抛
            raise RerankError(f"{self.provider} rerank 请求失败：{exc}") from exc

        if response.status_code != 200:
            # **正文截断后带上**：各家的报错正文差别很大，而"哪一家、什么码"
            # 是排查时唯一有用的两样（大区不对、模型名不对，都长这样）。
            raise RerankError(
                f"{self.provider} rerank 回了 {response.status_code}：{response.text[:200]}"
            )

        return self._parse(response.json(), len(documents))

    def _parse(self, payload: dict[str, Any], total: int) -> list[Scored]:
        """把结果项解析成 `Scored`。**越界的下标直接丢掉。**

        丢而不是报错：一条坏记录不该让整次检索失败。但**不静默补位**——
        少一条就是少一条，凭空造一个下标出来会让引用指向不相干的段落。
        """
        out: list[Scored] = []
        for item in self._results(payload):
            index = item.get("index")
            score = item.get("relevance_score")
            if not isinstance(index, int) or not isinstance(score, int | float):
                continue
            if 0 <= index < total:
                out.append(Scored(index=index, score=float(score), provider=self.provider))
        # **自己再排一次**：各家都说按分数降序返回，但那是它们的实现。
        # 顺序错了的后果是"最相关的那段排在第三条"，不会报错。
        out.sort(key=lambda s: -s.score)
        return out

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()


class CohereReranker(HttpReranker):
    provider = "cohere"
    path = "/v2/rerank"
    default_base_url = "https://api.cohere.com"


class JinaReranker(HttpReranker):
    """Jina 的形状**是照着 Cohere 做的**，所以除了 URL 什么都不用改。"""

    provider = "jina"
    path = "/v1/rerank"
    default_base_url = "https://api.jina.ai"


class VoyageReranker(HttpReranker):
    provider = "voyage"
    path = "/v1/rerank"
    default_base_url = "https://api.voyageai.com"

    def _body(self, query: str, documents: list[str], top_n: int) -> dict[str, Any]:
        # **`top_k`，不是 `top_n`。** 传错的话它不报错，只是把这个参数当没看见，
        # 于是每次都返回全部候选——症状是"变慢了"，不是"出错了"（同 P29）。
        return {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_k": top_n,
        }

    def _results(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return list(payload.get("data") or [])


class QwenReranker(HttpReranker):
    """DashScope 的原生形状。**它不在 `compatible-mode` 那条路上**——
    rerank 没有 OpenAI 兼容面，所以 base_url 也和 embeddings 那条不一样。
    """

    provider = "qwen"
    path = "/api/v1/services/rerank/text-rerank/text-rerank"
    default_base_url = "https://dashscope.aliyuncs.com"

    def _body(self, query: str, documents: list[str], top_n: int) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            # `return_documents: false` —— 原文我们手上已经有了，
            # 让它回传一遍只是把响应体撑大一倍。
            "parameters": {"top_n": top_n, "return_documents": False},
        }

    def _results(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return list((payload.get("output") or {}).get("results") or [])


PROVIDERS: dict[str, type[HttpReranker]] = {
    CohereReranker.provider: CohereReranker,
    JinaReranker.provider: JinaReranker,
    VoyageReranker.provider: VoyageReranker,
    QwenReranker.provider: QwenReranker,
}
"""认得哪几家。**加一家 = 加一个类 + 这里一行。**"""


def reranker_from_settings(
    settings: Settings, *, http_client: httpx2.AsyncClient | None = None
) -> HttpReranker | None:
    """按 `.env` 建一个。**没配就返回 `None`**——那不是故障。

    没配时**根本不 rerank**：按混合检索的融合次序取前几条，不发任何模型调用。
    同 embeddings 那条：**降级不是拒绝启用**（S140）。

    （这里曾经退回一份 chat 模型排序，那条路已经删掉——它只能排序不能否决，
    而这条链路最危险的一类正是"同领域但答不上"。）

    家数名写错**当场报错**，不猜也不回落：猜错的后果是每次检索都对着错的
    端点报错，而症状只是"检索变慢了"（同 P29）。
    """
    name = settings.rerank_provider.strip().lower()
    if not name or not settings.rerank_model:
        return None
    if name not in PROVIDERS:
        raise ValueError(f"不认得的 rerank 家数 `{name}`，可选：{'、'.join(sorted(PROVIDERS))}")
    return PROVIDERS[name](
        api_key=settings.rerank_api_key,
        model=settings.rerank_model,
        base_url=settings.rerank_base_url,
        timeout=settings.llm_timeout_seconds,
        http_client=http_client,
    )


__all__ = [
    "CohereReranker",
    "HttpReranker",
    "JinaReranker",
    "PROVIDERS",
    "QwenReranker",
    "RerankError",
    "Reranker",
    "Scored",
    "VoyageReranker",
    "reranker_from_settings",
]
