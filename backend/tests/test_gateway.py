"""gateway：OpenAI 兼容格式的请求构造、响应解析、重试。

用 httpx 的 MockTransport 拦截，不打真实网络。
"""

import json
from typing import Any

import httpx
import pytest

from agenttavern.config import Settings
from agenttavern.llm import LLMError, LLMGateway, Message, Role, ToolSpec


def settings(**kw: Any) -> Settings:
    base = {
        "llm_base_url": "https://example.test/v1",
        "llm_api_key": "sk-test",
        "llm_model": "test-model",
        "llm_max_retries": 2,
    }
    return Settings(**{**base, **kw})


def gateway_returning(*responses: httpx.Response, capture: list[dict[str, Any]] | None = None):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(json.loads(request.content))
        return queue.pop(0) if len(queue) > 1 else queue[0]

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMGateway(settings(), http_client=client)


def ok(message: dict[str, Any], usage: dict[str, int] | None = None) -> httpx.Response:
    body = {
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
    }
    return httpx.Response(200, json=body)


async def test_hits_the_configured_base_url() -> None:
    """只用 chat completions 这一个面，任何兼容端点都能接——base_url 决定去哪。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return ok({"content": "好"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = LLMGateway(settings(), http_client=client)

    await gw.complete([Message(role=Role.USER, content="hi")])
    assert str(captured[0].url) == "https://example.test/v1/chat/completions"
    assert captured[0].headers["Authorization"] == "Bearer sk-test"


async def test_blank_api_key_is_accepted() -> None:
    """本地端点（llama.cpp / vLLM）常常不校验 key，但 SDK 要求非空。"""
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: ok({"content": "好"})))
    gw = LLMGateway(settings(llm_api_key=""), http_client=client)
    assert (await gw.complete([Message(role=Role.USER, content="hi")])).content == "好"


async def test_request_shape_is_openai_format() -> None:
    sent: list[dict[str, Any]] = []
    gw = gateway_returning(ok({"content": "好"}), capture=sent)

    tool = ToolSpec(
        name="state_query",
        description="点查",
        parameters={"type": "object", "properties": {"dimension": {"type": "string"}}},
    )
    await gw.complete([Message(role=Role.USER, content="hi")], tools=[tool], temperature=0.8)

    payload = sent[0]
    assert payload["model"] == "test-model"
    assert payload["stream"] is False  # D7：非流式
    assert payload["temperature"] == 0.8
    assert payload["tools"][0]["function"]["name"] == "state_query"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]


async def test_parses_tool_calls() -> None:
    gw = gateway_returning(
        ok(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "state_write",
                            "arguments": '{"free": {"心情": "紧绷"}}',
                        },
                    }
                ],
            },
            usage={"prompt_tokens": 120, "completion_tokens": 30},
        )
    )

    result = await gw.complete([Message(role=Role.USER, content="hi")])
    call = result.tool_calls[0]
    assert call.name == "state_write"
    assert call.arguments == {"free": {"心情": "紧绷"}}
    assert call.parse_error is None
    assert (result.usage.tokens_in, result.usage.tokens_out) == (120, 30)


async def test_malformed_tool_arguments_are_captured_not_raised() -> None:
    """参数错误是**模型可恢复的错误**——要作为结构化 tool result 回给它重试，
    不能让整个 run 崩掉。所以这里只记录，不抛异常。
    """
    gw = gateway_returning(
        ok(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "state_write", "arguments": "{不是合法JSON"},
                    }
                ],
            }
        )
    )

    call = (await gw.complete([Message(role=Role.USER, content="hi")])).tool_calls[0]
    assert call.arguments is None
    assert call.parse_error
    assert call.arguments_raw == "{不是合法JSON"


async def test_tool_result_message_roundtrip() -> None:
    """tool 角色的消息要带 tool_call_id，否则端点会拒绝。"""
    sent: list[dict[str, Any]] = []
    gw = gateway_returning(ok({"content": "好"}), capture=sent)

    await gw.complete(
        [
            Message(role=Role.USER, content="hi"),
            Message(role=Role.TOOL, content="厨房", tool_call_id="call_1"),
        ]
    )
    assert sent[0]["messages"][1] == {
        "role": "tool",
        "content": "厨房",
        "tool_call_id": "call_1",
    }


async def test_retries_transient_then_succeeds() -> None:
    gw = gateway_returning(httpx.Response(503, text="busy"), ok({"content": "好"}))
    assert (await gw.complete([Message(role=Role.USER, content="hi")])).content == "好"


async def test_does_not_retry_client_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="bad key")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gw = LLMGateway(settings(), http_client=client)

    with pytest.raises(LLMError) as exc:
        await gw.complete([Message(role=Role.USER, content="hi")])
    assert exc.value.status == 401
    assert calls == 1


async def test_empty_choices_raises() -> None:
    """部分端点在被过滤/拒绝时返回空 choices 而非错误码。"""
    gw = gateway_returning(httpx.Response(200, json={"choices": []}))
    with pytest.raises(LLMError, match="choices"):
        await gw.complete([Message(role=Role.USER, content="hi")])


# -- reasoning_content（D120） -------------------------------------------------


def _tool_reply(reasoning: str | None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "state_query", "arguments": '{"keys": ["地点"]}'},
            }
        ],
    }
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return message


async def test_reasoning_content_is_captured() -> None:
    """它**不在 OpenAI 的 schema 里**，SDK 丢进 `model_extra`——不主动去取就没了。"""
    gw = gateway_returning(ok(_tool_reply("先查地点再决定。")))
    result = await gw.complete([Message(role=Role.USER, content="hi")])
    assert result.reasoning_content == "先查地点再决定。"


async def test_reasoning_content_rides_along_with_tool_calls() -> None:
    """带工具调用的 assistant 消息**必须原样回传** `reasoning_content`。

    DeepSeek 的文档说不传返回 400。实测 `deepseek-v4-flash` 目前不传也能过——
    **那是端点当下宽容，不是契约变了**，换个思考型模型就会炸，而且炸在下一轮。
    """
    sent: list[dict[str, Any]] = []
    gw = gateway_returning(ok(_tool_reply("先查地点再决定。")), ok({"content": "好"}), capture=sent)

    first = await gw.complete([Message(role=Role.USER, content="hi")])
    await gw.complete(
        [
            Message(role=Role.USER, content="hi"),
            first.as_assistant_message(),
            Message(role=Role.TOOL, content="旧货店", tool_call_id="call_1"),
        ]
    )

    assistant = sent[1]["messages"][1]
    assert assistant["reasoning_content"] == "先查地点再决定。"


async def test_reasoning_content_is_dropped_when_there_is_no_tool_call() -> None:
    """没有工具调用的那一轮不必带（文档明说）。

    持续对话里"模型只回了文本、被推回去"的轮次会**永远**留在上下文里，
    白背一段推理不划算。
    """
    gw = gateway_returning(ok({"content": "我想想", "reasoning_content": "在想。"}))
    result = await gw.complete([Message(role=Role.USER, content="hi")])

    assert result.reasoning_content == "在想。"
    assert result.as_assistant_message().reasoning_content is None


async def test_a_response_without_reasoning_sends_no_such_field() -> None:
    """不是思考型模型时不能凭空塞一个空字段进去。"""
    sent: list[dict[str, Any]] = []
    gw = gateway_returning(ok(_tool_reply(None)), ok({"content": "好"}), capture=sent)

    first = await gw.complete([Message(role=Role.USER, content="hi")])
    assert first.reasoning_content is None

    await gw.complete([Message(role=Role.USER, content="hi"), first.as_assistant_message()])
    assert "reasoning_content" not in sent[1]["messages"][1]
