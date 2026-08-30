"""embeddings 客户端（S140）。

**独立的一组配置**（`EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL`），
不复用 chat 那一组：多数人的 chat 与 embedding 未必在同一家。

**没填模型名就是不启用向量那一路**——不去拿 chat 的模型名猜一个。猜错的后果
是每次检索都对着一个不存在的模型报错，而症状只是"检索总是查不到"。
"""

from __future__ import annotations

from openai import AsyncOpenAI

from ..config import Settings


class Embedder:
    """把文本变成向量。一个薄封装——它只做一件事。"""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> Embedder | None:
        """没配就返回 `None`。**这不是故障，是常态**——
        整条链路会走降级路径（纯 BM25），而不是拒绝启用。
        """
        if not settings.embed_model:
            return None
        client = AsyncOpenAI(
            base_url=settings.embed_base_url or settings.llm_base_url,
            # 部分本地端点不校验 key，但 SDK 要求非空。
            api_key=settings.embed_api_key or settings.llm_api_key or "not-needed",
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
        return cls(client, settings.embed_model)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """一批文本 → 一批向量，**顺序与入参一致**。

        端点返回的顺序不保证和入参一致（多数实现一致，但那是实现不是契约），
        所以按 `index` 归位——错位的后果是每块配错向量，
        而检索结果只是"变得莫名其妙"，不会报错。

        **对着真端点验过一次**（2026-08-30，DashScope `qwen3.7-text-embedding`）：
        批量拿到的第 i 条，与单独再要一次的那条自相似度为 1.0000，四条全对。
        这只说明**归位之后的结果是对的**——那一家本来就按序返回，
        这一句 `sorted` 在它身上是空操作。换一家仍然可能不是，所以它留着。
        """
        if not texts:
            return []
        response = await self._client.embeddings.create(model=self._model, input=texts)
        ordered = sorted(response.data, key=lambda d: d.index)
        return [list(d.embedding) for d in ordered]

    async def aclose(self) -> None:
        await self._client.close()
