"""测试用的假 LLM。

按脚本依次返回响应，并记录每次请求，便于断言"给了谁什么上下文"——
拓扑的正确性大半就落在这上面（谁能看到正文、谁能看到文风）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx2
import pytest

from agenttavern.config import Settings
from agenttavern.llm import LLMGateway
from agenttavern.prompting import PromptSetup, load, prompts_as

FOLLOWUP_MARK = "本轮已经提交"
"""生成后追问的标志串（S22）。"""

STATE_MARK = "你负责维护一份"
"""state_manager 开场白的头几个字（S7）。用来认出是它在问。"""

GOAL_MARK = "用户设置的目标"
"""goal_manager 越狱词里的一句（S92）。用来在请求流水里认出是它在问。"""

WRITER_MARK = "你收到的细纲描述的是"
"""writer 输入契约的头一句。**认头一句而不是标签**——装配器不再自动包
`<本轮细纲>` 那样的标记了（R31：所见即所得），而「本轮细纲」这四个字在
director 那侧也出现（`submit_outline` 的描述、催促词），只认字面会把 director
一起认进去。这一句只属于 writer。
"""


class ScriptedLLM:
    def __init__(
        self,
        *replies: dict[str, Any],
        auto_close: int | None = 0,
        state: dict[str, Any] | list[dict[str, Any]] | None = None,
        auto_state: bool = True,
        on_goal_request: Callable[[], None] | None = None,
    ) -> None:
        """``auto_close`` 让**生成后追问**自动应答，不消耗脚本。

        追问每轮都发生（S58：判定在生成时），但绝大多数测试与归段无关——
        让它们各自在脚本里插一条应答，只会把测试写得更吵、更容易错位。
        测归段的测试传 ``auto_close=None``，自己把每一轮的答案排进脚本。

        **答案是一次工具调用，不是一个数字**：`0` = 不调用 `close_segment`
        （先别断），`k` = 调用它、`after=k`。以前是让模型回一串数字，
        代码从自由文本里抓——抓错了不出声，而标记一打就不可逆。

        **state_manager 同理**（S7）：它每轮跑一次，跑在 writer 之后。
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

        self.on_writer_request: Callable[[], None] | None = None
        """在**应答 writer 那次请求的同一刻**被调用。

        它抢的是「tool loop 已经结束、这一轮还没落地」那个窗口——信箱此时已经
        关了，而 `drain()` 还没发生。**从外面靠轮询抢不到**：脚本化的模型没有
        真实延迟，两次轮询之间整轮就跑完了。公开属性而非构造参数，是因为回调
        通常要引用 runner，而 runner 得先拿到这个 llm 才能建出来。
        """

    def _reply(self, message: dict[str, Any], *, streaming: bool = False) -> httpx2.Response:
        body = {
            "choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        }
        return _sse(body) if streaming else httpx2.Response(200, json=body)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        messages = payload["messages"]
        # **流式默认是开的**（S118），所以脚本必须两种都会应答——否则整套测试
        # 走的都是非流式那条路，而产品跑的是另一条。
        streaming = bool(payload.get("stream"))

        if self._on_goal_request and GOAL_MARK in messages[0].get("content", ""):
            self._on_goal_request()

        if (
            self.on_writer_request
            and STATE_MARK not in messages[0].get("content", "")
            and WRITER_MARK in str(messages)
        ):
            self.on_writer_request()

        if self._auto_close is not None and FOLLOWUP_MARK in messages[-1].get("content", ""):
            return self._reply(close_segment(self._auto_close), streaming=streaming)

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
        return _sse(body) if streaming else httpx2.Response(200, json=body)

    def gateway(self) -> LLMGateway:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))
        return LLMGateway(Settings(), http_client=client)

    @property
    def system_prompts(self) -> list[str]:
        return [r["messages"][0]["content"] for r in self.requests]

    def messages_of(self, index: int) -> list[dict[str, Any]]:
        return self.requests[index]["messages"]


def _sse(body: dict[str, Any]) -> httpx2.Response:
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
    return httpx2.Response(200, text=body_text, headers={"content-type": "text/event-stream"})


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


def close_segment(after: int) -> dict[str, Any]:
    """生成后追问的一个答案（S58）。

    **`0` 是"不调用"**，不是"调用并传 0"：`close_segment.after` 有 `ge=1`，
    0 没有含义——"先别断"的表达方式就是这一次什么工具都不调。
    """
    return text("先不断。") if after <= 0 else tool("close_segment", after=after)


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

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        messages = json.loads(request.content)["messages"]
        if STATE_MARK not in messages[0].get("content", "") and WRITER_MARK in str(messages):
            raise asyncio.CancelledError
        return super()._handle(request)


@pytest.fixture
def scripted():
    return ScriptedLLM


# -- 测试不许读开发者的 .env --------------------------------------------------


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """`Settings()` 在测试里**只看显式传进去的那几个字段**。

    `SettingsConfigDict(env_file=".env")` 是 **CWD 相对**的，而 pytest 就在
    `backend/` 跑——于是 `Settings()` 会把开发者 `.env` 里那一整套
    端点、key、模型全吸进来。

    **后果是测试的成败取决于这台机器配了什么。** 实测撞过：往 `.env` 里填了
    `RERANK_PROVIDER=qwen` 之后，`test_no_provider_configured_means_no_reranker`
    当场变红——而 CI 上没有 `.env`，它在那边一直是绿的。
    **本地红、CI 绿**比反过来更难查：没人会去怀疑一条 CI 通过的测试。

    环境变量也一并清掉：它和 `.env` 是同一个入口的两半，只堵一半等于没堵。
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


# -- 提示词配置：**取值住在 YAML 里**（R31/S80）-------------------------------
#
# `Capabilities` / `ControlDimensions` 都没了，所以测试要换一档、开关一个工具，
# 走的是和 bench 消融同一条路：**在一份副本上改，跑完删**（`prompts_as`）。
# 那也是唯一一条路——不为测试开第二个取值入口（S80）。


def option(agent: str, group: str, name: str) -> str:
    """某个 `choice` 某一档的正文。断言"哪一档进去了"时用它，不钉措辞。"""
    for item in load(agent).get("sections") or []:
        if isinstance(item, dict) and item.get("type") == "choice" and item.get("name") == group:
            return str((item.get("options") or {}).get(name) or "").strip()
    raise KeyError(f"{agent}.yaml 里没有 `{group}` 这个选项组")


def tool_option(agent: str, tool: str, name: str) -> str:
    """工具描述那个 `choice` 的某一档（`ask_question` 是唯一一个）。"""
    spec = (load(agent).get("tools") or {}).get(tool) or {}
    return str((spec.get("options") or {}).get(name) or "").strip()


def special_body(agent: str, key: str) -> str:
    """某条附属提示词的正文。"""
    spec = (load(agent).get("specials") or {}).get(key) or {}
    return "\n".join(str(s.get("body") or "") for s in spec.get("sections") or []).strip()


@contextmanager
def using(**kwargs: Any) -> Iterator[None]:
    """在这段代码里换一份提示词配置。用法同 `PromptSetup` 的三个字段。"""
    with prompts_as(PromptSetup(**kwargs)):
        yield


HISTORY_ON = {"tools": {"director": {"history_read": True}}}
"""检索**默认关**（P14：能力开关默认关）。要测它就得显式打开——第一次跑长会话
场景时忘了开，于是"12 轮里检索工具一次都没被调用"这个观察什么也说明不了：
**工具当时不存在**。"""
