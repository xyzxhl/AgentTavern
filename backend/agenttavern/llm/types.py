"""LLM 调用的规范化类型。

用自己的类型而非直接暴露 SDK 的，理由只有一个但很实在：``ToolCall.from_raw``
**把解析失败记录下来而不是抛出**——参数错误是模型可恢复的错误，应作为结构化的
tool result 回给它重试，不能让整个 run 崩掉。SDK 的类型表达不了这个区分。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """模型发起的一次工具调用。

    ``arguments`` 在 OpenAI 格式里是 **JSON 字符串**，而模型时常输出不合法的 JSON。
    这里把解析结果和解析错误都留下——**参数错误是模型可恢复的错误**，应作为结构化
    的 tool result 回给它重试，而不是让整个 run 崩掉。
    """

    id: str
    name: str
    arguments_raw: str = "{}"

    arguments: dict[str, Any] | None = None
    parse_error: str | None = None

    @classmethod
    def from_raw(cls, call_id: str, name: str, arguments_raw: str) -> ToolCall:
        try:
            parsed = json.loads(arguments_raw or "{}")
        except json.JSONDecodeError as exc:
            return cls(id=call_id, name=name, arguments_raw=arguments_raw, parse_error=str(exc))
        if not isinstance(parsed, dict):
            return cls(
                id=call_id,
                name=name,
                arguments_raw=arguments_raw,
                parse_error="arguments 必须是 JSON 对象",
            )
        return cls(id=call_id, name=name, arguments_raw=arguments_raw, arguments=parsed)


class Message(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    """``role=tool`` 时必填，指向被回应的那次调用。"""

    reasoning_content: str | None = None
    """思考型模型的推理段，**带工具调用时必须原样回传**（D120）。

    DeepSeek 的文档写得很直接：带 tool call 的中间 assistant 消息，其
    `reasoning_content` 必须参与后续所有轮次的上下文拼接，否则返回 400。
    实测 `deepseek-v4-flash` 目前不回传也能过，但那是端点当下宽容，不是契约变了。

    没有工具调用的那一轮不必带（文档明说），所以 `as_assistant_message` 只在有
    工具调用时挂上它——持续对话里那些"模型只回了文本、被推回去"的轮次会永远留在
    上下文里，白背一段推理不划算。
    """

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role.value}
        if self.content is not None:
            payload["content"] = self.content
        if self.reasoning_content is not None:
            payload["reasoning_content"] = self.reasoning_content
        if self.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments_raw},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        return payload


class ToolSpec(BaseModel):
    """工具定义。`parameters` 是 JSON Schema，由 Pydantic 模型导出。"""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Usage(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0

    cache_hit: int = 0
    cache_miss: int = 0
    """输入里命中/未命中前缀缓存的部分（DeepSeek 的 `prompt_cache_hit_tokens`）。

    **D114 的全部理由就是缓存命中，而我们此前从没测过它。** 这两个字段一直在
    usage 里，被直接扔掉了——于是"前缀稳定"是个没有证据的说法。

    缓存的命中条件很硬：**从第 0 个 token 起完全相同的前缀**才算，中间对上不算；
    最小粒度 64 token；不用就会在几小时到几天内被清掉。所以任何动到 system
    prompt 或早期消息的改动，都会让整条前缀作废——这正是 D114 说"每收一段就
    换一段等于每隔几轮动一次前缀"的那件事，现在它可以被量出来了。
    """

    @property
    def cache_rate(self) -> float:
        total = self.cache_hit + self.cache_miss
        return self.cache_hit / total if total else 0.0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cache_hit=self.cache_hit + other.cache_hit,
            cache_miss=self.cache_miss + other.cache_miss,
        )


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None

    reasoning_content: str | None = None
    """见 ``Message.reasoning_content``。"""

    def as_assistant_message(self) -> Message:
        return Message(
            role=Role.ASSISTANT,
            content=self.content or None,
            tool_calls=self.tool_calls,
            # **只在带工具调用时挂上**：那是契约要求的场合，也是唯一有用的场合。
            reasoning_content=self.reasoning_content if self.tool_calls else None,
        )


class LLMError(RuntimeError):
    """调用失败，且 SDK 已按其策略重试过。

    区别于**模型可恢复的错误**——后者（参数不合法、工具不存在）不抛异常，
    走 tool result 回给模型重试。这里是真正的故障。

    存在的意义是让上层不必认识 SDK 的异常层次。
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
