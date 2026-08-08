"""测试用的假 LLM。

按脚本依次返回响应，并记录每次请求，便于断言"给了谁什么上下文"——
拓扑的正确性大半就落在这上面（谁能看到正文、谁能看到文风）。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agenttavern.config import Settings
from agenttavern.llm import LLMGateway

FOLLOWUP_MARK = "本轮已经提交"
"""生成后追问的标志串（D112）。"""


class ScriptedLLM:
    def __init__(self, *replies: dict[str, Any], auto_close: str | None = "0") -> None:
        """``auto_close`` 让**生成后追问**自动应答，不消耗脚本。

        追问每轮都发生（D113：判定在生成时），但绝大多数测试与归段无关——
        让它们各自在脚本里插一条应答，只会把测试写得更吵、更容易错位。
        测归段的测试传 ``auto_close=None``，自己把每一轮的答案排进脚本。
        """
        self._replies = list(replies)
        self._auto_close = auto_close
        self.requests: list[dict[str, Any]] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)

        if self._auto_close and FOLLOWUP_MARK in payload["messages"][-1].get("content", ""):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": self._auto_close}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 1},
                },
            )

        # 脚本用尽后重复最后一条：单 run 的测试只关心前几步，不必把结尾写全。
        # 需要跨多个 run 的测试必须把每一轮的回复都排好。
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": reply, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            },
        )

    def gateway(self) -> LLMGateway:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        return LLMGateway(Settings(llm_model="test-model"), http_client=client)

    @property
    def system_prompts(self) -> list[str]:
        return [r["messages"][0]["content"] for r in self.requests]

    def messages_of(self, index: int) -> list[dict[str, Any]]:
        return self.requests[index]["messages"]


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


@pytest.fixture
def scripted():
    return ScriptedLLM
