"""测试用的假 LLM。

按脚本依次返回响应，并记录每次请求，便于断言"给了谁什么上下文"——
拓扑的正确性大半就落在这上面（谁能看到正文、谁能看到文风）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agenttavern.config import Settings
from agenttavern.llm import LLMGateway

FOLLOWUP_MARK = "本轮已经提交"
"""生成后追问的标志串（D112）。"""

STATE_MARK = "你负责维护一份"
"""state_manager 开场白的头几个字（D143）。用来认出是它在问。"""

GOAL_MARK = "用户设置的目标"
"""goal_manager 越狱词里的一句（D148）。用来在请求流水里认出是它在问。"""

WRITER_MARK = "<本轮细纲>"
"""writer 提示词里那一节的**标签**，尖括号不能省——「本轮细纲」这四个字在
director 那侧也出现（`submit_outline` 的描述、催促词），只认字面会把 director
一起认进去。state_manager 有同名的一节，所以认 writer 时还要先排除掉它。
"""


class ScriptedLLM:
    def __init__(
        self,
        *replies: dict[str, Any],
        auto_close: str | None = "0",
        state: dict[str, Any] | list[dict[str, Any]] | None = None,
        auto_state: bool = True,
        on_goal_request: Callable[[], None] | None = None,
    ) -> None:
        """``auto_close`` 让**生成后追问**自动应答，不消耗脚本。

        追问每轮都发生（D113：判定在生成时），但绝大多数测试与归段无关——
        让它们各自在脚本里插一条应答，只会把测试写得更吵、更容易错位。
        测归段的测试传 ``auto_close=None``，自己把每一轮的答案排进脚本。

        **state_manager 同理**（D143）：它每轮跑一次，跑在 writer 之后。
        把这一轮该记的东西传 ``state=``（形状就是 `state_write` 的参数，
        如 ``state={"set": {"地点": "客厅"}}``），它会写一次然后收口；不传就空手
        回来，等于"这一轮没什么要改的"。跨多个 run 的测试传一个**列表**，
        一个 run 用掉一条，用完之后各轮都空手。

        **这样写是为了让脚本不再编码调用顺序。** 状态维护的位置已经搬过一次
        （从 director 之内搬到 writer 之后），编码顺序的脚本那次全断了。
        要测 state_manager 的 loop 本身，传 ``auto_state=False`` 自己排脚本。

        ``on_goal_request`` 在**应答 goal_manager 那次请求的同一刻**被调用，
        用来模拟"判定跑到一半时用户动了手"（放弃、暂停、改预算）。那个窗口从
        WS 那侧抢不到：消息会在判定开始之前就被处理掉，于是根本走不到写回那步。
        """
        self._replies = list(replies)
        self._auto_close = auto_close
        self._states = [state] if isinstance(state, dict) else list(state or [])
        self._auto_state = auto_state
        self._on_goal_request = on_goal_request
        self.requests: list[dict[str, Any]] = []

    def _reply(self, message: dict[str, Any], *, streaming: bool = False) -> httpx.Response:
        body = {
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        }
        return _sse(body) if streaming else httpx.Response(200, json=body)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        messages = payload["messages"]
        # **流式默认是开的**（D160），所以脚本必须两种都会应答——否则整套测试
        # 走的都是非流式那条路，而产品跑的是另一条。
        streaming = bool(payload.get("stream"))

        if self._on_goal_request and GOAL_MARK in messages[0].get("content", ""):
            self._on_goal_request()

        if self._auto_close and FOLLOWUP_MARK in messages[-1].get("content", ""):
            return self._reply(text(self._auto_close), streaming=streaming)

        if self._auto_state and STATE_MARK in messages[0].get("content", ""):
            # 已经写过一次（上一条是工具结果）就收口——**出口是"不再调用工具"**。
            if self._states and messages[-1].get("role") != "tool":
                return self._reply(tool("state_write", **self._states.pop(0)), streaming=streaming)
            return self._reply(text("无变化。"), streaming=streaming)

        # 脚本用尽后重复最后一条：单 run 的测试只关心前几步，不必把结尾写全。
        # 需要跨多个 run 的测试必须把每一轮的回复都排好。
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        body = {
            "choices": [{"message": reply, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }
        return _sse(body) if streaming else httpx.Response(200, json=body)

    def gateway(self) -> LLMGateway:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        return LLMGateway(Settings(llm_model="test-model"), http_client=client)

    @property
    def system_prompts(self) -> list[str]:
        return [r["messages"][0]["content"] for r in self.requests]

    def messages_of(self, index: int) -> list[dict[str, Any]]:
        return self.requests[index]["messages"]


def _sse(body: dict[str, Any]) -> httpx.Response:
    """把一个整块响应拆成 SSE 分片。

    **正文按字拆**，好让"边写边推"这件事在测试里真的发生；工具调用整块给——
    产品那侧也不推工具分片（半截 JSON 对界面没用），拆了反而测的不是同一件事。
    """
    message = body["choices"][0]["message"]
    chunks: list[dict[str, Any]] = []

    if message.get("tool_calls"):
        chunks.append(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": i,
                                    "id": c["id"],
                                    "type": "function",
                                    "function": {
                                        "name": c["function"]["name"],
                                        "arguments": c["function"]["arguments"],
                                    },
                                }
                                for i, c in enumerate(message["tool_calls"])
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
    for piece in message.get("content") or "":
        chunks.append(
            {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
        )

    chunks.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    chunks.append({"choices": [], "usage": body["usage"]})

    body_text = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body_text, headers={"content-type": "text/event-stream"})


def text(content: str) -> dict[str, Any]:
    return {"content": content}


def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


def tools(*calls: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """一次响应里发起多个工具调用。"""
    return {
        "content": None,
        "tool_calls": [
            {
                "id": f"call_{i}_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for i, (name, args) in enumerate(calls)
        ],
    }


class CancelWriter(ScriptedLLM):
    """**只掐掉 writer 那一次调用**，前后的照常应答。

    按调用序号数不管用了：一轮现在有三次调用（细纲 / 正文 / 状态），而
    director 用了几轮取决于它查了几次东西。认提示词特征才稳。
    """

    def _handle(self, request: httpx.Request) -> httpx.Response:
        messages = json.loads(request.content)["messages"]
        if STATE_MARK not in messages[0].get("content", "") and WRITER_MARK in str(messages):
            raise asyncio.CancelledError
        return super()._handle(request)


@pytest.fixture
def scripted():
    return ScriptedLLM
