"""LLM gateway：openai SDK 的 chat completions。

**用官方 SDK，但不用任何 agent SDK / 编排框架。** 区别是：SDK 只负责一次调用
（重试、错误分类、超时、解析），**tool loop 完全由我们自己写**——本项目对它的控制
粒度要求很高：run 内暂存区、``submit_outline`` 的终止语义、契约校验、按阶段打断
（S49）、无超时挂起（S24）。框架会挡在中间，SDK 不会。

只用 chat completions 这一个面意味着任何兼容端点都能接：OpenAI、OpenRouter、
DeepSeek、本地 llama.cpp / vLLM。配置见 ``.env``。

**默认流式**（S118）。P21「流式让步于 agent 设计」反对的是为流式改结构；
作为输出通道加回来不动任何结构——上层拿到的 `ModelResponse` 两种路径完全一样。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx2
from openai import APIError, AsyncOpenAI

from ..config import Settings
from .types import Call, LLMError, Message, ModelResponse, ToolCall, ToolSpec, Usage

logger = logging.getLogger(__name__)

_COMPLETE_REASONS = {"stop", "tool_calls", "function_call"}

OnDelta = Callable[[str, str], None]
"""``(kind, text)``——``kind`` 是 ``"content"`` 或 ``"reasoning"``。

**按调用传，不挂在 gateway 上**（S118）：一个 gateway 被所有 session 共用，
挂实例字段等于让并发的两轮互相串台。
"""


class LLMGateway:
    # `http_client` 是**测试注入真实网络替身的唯一入口**（`MockTransport`）。
    # 类型必须跟着 SDK 走：openai 3.x 用的是 `httpx2`，不是 `httpx`。
    def __init__(self, settings: Settings, http_client: httpx2.AsyncClient | None = None) -> None:
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
        call: Call,
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_id: str | None = None,
        on_delta: OnDelta | None = None,
    ) -> ModelResponse:
        """一次补全。

        ``call`` 带着这一次调用的三个旋钮：**用哪个模型、流不流、额外塞什么**
        （S159）。它是必填的——**没有全局兜底**：以前 `model` 留空就回落到
        `.env` 里那个，于是某份 YAML 忘了写模型，它照样跑得好好的、用着别人的
        那个，而没有任何地方会说一声。

        ``call.extra`` **原样并进 `extra_body`**，一个字都不解释。思考模式那类
        参数走这里——各家拼法根本不同（`thinking: {type}` / `reasoning_effort`
        / `budget_tokens`），定一套可移植的三档就要按家数映射，而猜错的症状是
        **不报错、只是没生效**。

        > 关于思考模式有一条实测值得记：**DeepSeek 上它默认开启**，而开启时
        > `temperature` / `top_p` / `presence_penalty` / `frequency_penalty`
        > 全部被**静默忽略**——不报错，就是不生效。同一个 prompt 连发三次、
        > `temperature=0`，三次输出都不同；关掉思考后三次完全相同。
        > 所以传 `temperature` 而不管思考模式，等于什么都没设。

        ``user_id`` **按业务侧身份隔离 KVCache**（DeepSeek 的字段名就是 `user_id`，
        不是 OpenAI 标准的 `user`——传后者会被静默忽略）。实测：同一份前缀，
        `alice` 建好缓存后 `bob` 命中为 0，回到 `alice` 又是满命中，不传则是第三个
        独立空间。

        不传的后果不是"没缓存"，而是**所有会话挤在同一个空间里**：同一个场景
        连跑两次会互相命中，于是 `cache_rate` 里混进了跨会话的旧缓存，
        而 S60 要量的是**同一段对话内的前缀复用**。传上会话 id，这个数才是干净的。
        """
        if not call.model:
            raise LLMError("这次调用没有模型名——那份 YAML 顶上的 `model` 是空的。")

        payload: dict[str, Any] = {
            "model": call.model,
            "messages": [m.to_wire() for m in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = [t.to_wire() for t in tools]
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # 都不在 OpenAI 的 schema 里，走 extra_body。**`user_id` 摆在后面**：
        # 它是缓存隔离的命脉（见下），不能被 YAML 里随手写的一个同名键盖掉。
        extra: dict[str, Any] = {**call.extra}
        if user_id is not None:
            extra["user_id"] = user_id
        if extra:
            payload["extra_body"] = extra

        # **两个条件都要成立**：编排层给了出口，而且这个 agent 设了要流。
        if on_delta is not None and call.stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        else:
            on_delta = None

        try:
            if on_delta is None:
                response = await self._client.chat.completions.create(**payload)
                parsed = _parse(response)
            else:
                parsed = await self._consume(payload, on_delta)
        except APIError as exc:
            # SDK 已按其策略重试过；到这里就是最终失败。包成本项目的错误类型，
            # 让上层不必认识 SDK 的异常层次。
            status = getattr(exc, "status_code", None)
            logger.exception(
                "llm_request_failed model=%s stream=%s status=%s",
                call.model,
                on_delta is not None,
                status,
            )
            raise LLMError(str(exc), status=status) from exc
        except Exception:
            logger.exception(
                "llm_response_failed model=%s stream=%s", call.model, on_delta is not None
            )
            raise

        try:
            _require_complete(parsed)
        except LLMError:
            logger.warning(
                "llm_response_incomplete model=%s stream=%s finish_reason=%r "
                "content_chars=%s reasoning_chars=%s tool_calls=%s tokens_in=%s tokens_out=%s",
                call.model,
                on_delta is not None,
                parsed.finish_reason,
                len(parsed.content),
                len(parsed.reasoning_content or ""),
                len(parsed.tool_calls),
                parsed.usage.tokens_in,
                parsed.usage.tokens_out,
            )
            raise

        logger.info(
            "llm_response_complete model=%s stream=%s finish_reason=%s content_chars=%s "
            "reasoning_chars=%s tool_calls=%s tokens_in=%s tokens_out=%s",
            call.model,
            on_delta is not None,
            parsed.finish_reason,
            len(parsed.content),
            len(parsed.reasoning_content or ""),
            len(parsed.tool_calls),
            parsed.usage.tokens_in,
            parsed.usage.tokens_out,
        )
        return parsed

    async def _consume(self, payload: dict[str, Any], on_delta: OnDelta) -> ModelResponse:
        """把流式分片攒回一个完整的 `ModelResponse`，边攒边把文字推出去。

        **上层拿到的东西和非流式完全一样**——这是流式不扭曲结构的关键（S118）：
        agent 那侧不知道自己是不是在流，工具循环、契约、重放全都不用改。
        流式只是多了一条**旁路**：正文与思考的分片顺手推给 UI。

        **工具调用不推分片。** 它们的 `arguments` 是逐字拼出来的 JSON，半截
        JSON 对界面毫无用处，只会逼前端去猜；等它完整了再一次性给。
        """
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish_reason = ""
        usage = Usage()

        stream = await self._client.chat.completions.create(**payload)
        async for chunk in stream:
            if chunk.usage is not None:
                usage = _usage_of(chunk.usage)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason

            delta = choice.delta
            if delta is None:
                continue

            think = getattr(delta, "reasoning_content", None)
            if think:
                reasoning.append(think)
                on_delta("reasoning", think)
            if delta.content:
                content.append(delta.content)
                on_delta("content", delta.content)

            for raw in delta.tool_calls or []:
                slot = calls.setdefault(raw.index, {"id": "", "name": "", "arguments": ""})
                if raw.id:
                    slot["id"] = raw.id
                if raw.function and raw.function.name:
                    slot["name"] = raw.function.name
                if raw.function and raw.function.arguments:
                    slot["arguments"] += raw.function.arguments

        return ModelResponse(
            content="".join(content),
            reasoning_content="".join(reasoning) or None,
            tool_calls=[
                ToolCall.from_raw(
                    call_id=slot["id"], name=slot["name"], arguments_raw=slot["arguments"] or "{}"
                )
                for _, slot in sorted(calls.items())
            ],
            finish_reason=finish_reason,
            usage=usage,
        )


def _usage_of(raw: Any) -> Usage:
    extra = getattr(raw, "model_extra", None) or {}
    return Usage(
        tokens_in=raw.prompt_tokens or 0,
        tokens_out=raw.completion_tokens or 0,
        # 不在 OpenAI 的 schema 里，同 reasoning_content 走 `model_extra`。
        cache_hit=int(extra.get("prompt_cache_hit_tokens") or 0),
        cache_miss=int(extra.get("prompt_cache_miss_tokens") or 0),
    )


def _require_complete(response: ModelResponse) -> None:
    """拒绝把被截断或无结束标记的半份响应交给 agent。

    OpenAI 兼容协议的完整终点只有正常停止或完整工具调用。尤其流式连接若在
    reasoning 之后直接断开，会没有最终 ``finish_reason``；此前它会被当成一份
    合法的空回答，真正原因因此丢失。
    """
    reason = response.finish_reason
    if reason in _COMPLETE_REASONS:
        return
    if not reason:
        raise LLMError("模型响应在没有 finish_reason 的情况下结束，可能是 provider 中途断流")
    raise LLMError(f"模型响应未完整结束（finish_reason={reason}）")


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

    usage = _usage_of(response.usage) if response.usage is not None else Usage()

    return ModelResponse(
        content=message.content or "",
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=choice.finish_reason,
        reasoning_content=_reasoning_of(message),
    )


def _reasoning_of(message: Any) -> str | None:
    """取思考型模型的推理段（S62）。

    **它不在 OpenAI 的 schema 里**，SDK 把它丢进 `model_extra`。所以既要看
    `model_extra`，也要 `getattr` 兜一手——不同端点、不同 SDK 版本落点不一样，
    取不到时只是静默地少带一段，不该炸。
    """
    extra = getattr(message, "model_extra", None) or {}
    value = extra.get("reasoning_content") or getattr(message, "reasoning_content", None)
    return value if isinstance(value, str) and value else None
