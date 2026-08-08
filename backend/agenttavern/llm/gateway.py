"""LLM gateway：openai SDK 的 chat completions。

**用官方 SDK，但不用任何 agent SDK / 编排框架。** 区别是：SDK 只负责一次调用
（重试、错误分类、超时、解析），**tool loop 完全由我们自己写**——本项目对它的控制
粒度要求很高：run 内暂存区、``submit_outline`` 的终止语义、契约校验、分阶段打断
（D60）、无超时挂起（D66）。框架会挡在中间，SDK 不会。

只用 chat completions 这一个面意味着任何兼容端点都能接：OpenAI、OpenRouter、
DeepSeek、本地 llama.cpp / vLLM。配置见 ``.env``。

**非流式**（D7：流式让步于 agent 设计）。
"""

from __future__ import annotations

from typing import Any

import httpx
from openai import APIError, AsyncOpenAI

from ..config import Settings
from .types import LLMError, Message, ModelResponse, ToolCall, ToolSpec, Usage


class LLMGateway:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            # 部分本地端点不校验 key，但 SDK 要求非空。
            api_key=settings.llm_api_key or "not-needed",
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            http_client=http_client,
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> LLMGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        thinking: bool | None = None,
        user_id: str | None = None,
    ) -> ModelResponse:
        """一次非流式补全。

        ``model`` 可覆盖默认值——director / writer / goal 判定将来可能用不同模型
        （goal 判定是一次极短的窄调用，用便宜的就够）。

        ``thinking`` 显式开关思考模式。**DeepSeek 上它默认开启（effort=high），
        而开启时 `temperature` / `top_p` / `presence_penalty` / `frequency_penalty`
        全部被静默忽略**——不报错，就是不生效。实测：默认配置下同一个 prompt 连发
        三次、`temperature=0`，三次输出都不同；关掉思考后三次完全相同。

        所以传 `temperature` 而不管思考模式，等于什么都没设。**要确定性就必须
        同时关掉思考**，而那是个有代价的决定（思考本身在很多任务上是有用的），
        因此这里只提供开关，不替调用方选。

        ``user_id`` **按业务侧身份隔离 KVCache**（DeepSeek 的字段名就是 `user_id`，
        不是 OpenAI 标准的 `user`——传后者会被静默忽略）。实测：同一份前缀，
        `alice` 建好缓存后 `bob` 命中为 0，回到 `alice` 又是满命中，不传则是第三个
        独立空间。

        不传的后果不是"没缓存"，而是**所有会话挤在同一个空间里**：同一个场景
        连跑两次会互相命中，于是 `cache_rate` 里混进了跨会话的旧缓存，
        而 D114 要量的是**同一段对话内的前缀复用**。传上会话 id，这个数才是干净的。
        """
        payload: dict[str, Any] = {
            "model": model or self._settings.llm_model,
            "messages": [m.to_wire() for m in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = [t.to_wire() for t in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # 两个都不在 OpenAI 的 schema 里，走 extra_body。
        extra: dict[str, Any] = {}
        if thinking is not None:
            extra["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if user_id is not None:
            extra["user_id"] = user_id
        if extra:
            payload["extra_body"] = extra

        try:
            response = await self._client.chat.completions.create(**payload)
        except APIError as exc:
            # SDK 已按其策略重试过；到这里就是最终失败。包成本项目的错误类型，
            # 让上层不必认识 SDK 的异常层次。
            status = getattr(exc, "status_code", None)
            raise LLMError(str(exc), status=status) from exc

        return _parse(response)


def _parse(response: Any) -> ModelResponse:
    if not response.choices:
        # 部分端点在内容被过滤/拒绝时返回 200 + 空 choices 而非错误码。
        # 不在这里拦住的话，下游会拿到空响应然后在莫名其妙的地方炸。
        raise LLMError(f"响应缺少 choices：{str(response)[:300]}")

    choice = response.choices[0]
    message = choice.message

    tool_calls = [
        ToolCall.from_raw(
            call_id=raw.id or "",
            name=raw.function.name or "",
            arguments_raw=raw.function.arguments or "{}",
        )
        for raw in (message.tool_calls or [])
    ]

    usage = Usage()
    if response.usage is not None:
        extra = getattr(response.usage, "model_extra", None) or {}
        usage = Usage(
            tokens_in=response.usage.prompt_tokens or 0,
            tokens_out=response.usage.completion_tokens or 0,
            # 也不在 OpenAI 的 schema 里，同 reasoning_content 走 `model_extra`。
            cache_hit=int(extra.get("prompt_cache_hit_tokens") or 0),
            cache_miss=int(extra.get("prompt_cache_miss_tokens") or 0),
        )

    return ModelResponse(
        content=message.content or "",
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=choice.finish_reason,
        reasoning_content=_reasoning_of(message),
    )


def _reasoning_of(message: Any) -> str | None:
    """取思考型模型的推理段（D120）。

    **它不在 OpenAI 的 schema 里**，SDK 把它丢进 `model_extra`。所以既要看
    `model_extra`，也要 `getattr` 兜一手——不同端点、不同 SDK 版本落点不一样，
    取不到时只是静默地少带一段，不该炸。
    """
    extra = getattr(message, "model_extra", None) or {}
    value = extra.get("reasoning_content") or getattr(message, "reasoning_content", None)
    return value if isinstance(value, str) and value else None
