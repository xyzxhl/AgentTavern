"""录制与指标——采集侧的共用件。

**在 gateway 层录制**，因此存的是模型真正收到的 messages 与真正返回的内容，
不是我们以为它收到的——这个区别在调 prompt 时至关重要。

指标全部从 trace 里算，不需要额外埋点。它们都是 program grader 能安全做的那一类
（可数的量、契约形状），**不判断质量**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..llm import LLMGateway, Message, ModelResponse, ToolSpec
from ..models import TraceEvent


@dataclass
class Exchange:
    messages: list[Message]
    tools: list[ToolSpec] | None
    response: ModelResponse | None = None
    error: str | None = None


class RecordingGateway(LLMGateway):
    """透明代理，顺手把每次往返存下来。"""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.exchanges: list[Exchange] = []

    async def complete(self, messages: list[Message], **kwargs: Any) -> ModelResponse:
        record = Exchange(messages=list(messages), tools=kwargs.get("tools"))
        self.exchanges.append(record)
        try:
            record.response = await super().complete(messages, **kwargs)
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            raise
        return record.response


@dataclass
class Metrics:
    rounds: int = 0
    nudges: int = 0
    """模型只回文本、没调工具的次数。弱模型上这是主要失败模式。"""

    e0_pushbacks: int = 0
    """先调 submit_outline 被契约退回的次数。"""

    parse_errors: int = 0
    """工具参数不是合法 JSON 的次数。"""

    tool_errors: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cache_hit: int = 0
    """命中前缀缓存的输入 token。占 `tokens_in` 的比例才是 D114 该看的数。"""

    questions: list[tuple[int, bool]] = field(default_factory=list)
    """问过的问题：``(选项个数, 用户是否自由作答)``（D155）。

    选项个数要记，是因为「恰好三个」是参数契约，而契约有没有被守住只有实跑看得出来。
    """

    segments: list[tuple[int, int, int]] = field(default_factory=list)
    """这一轮归掉的段：``(轮数, 压缩前字数, 压缩后字数)``。

    **只统计不约束**（D154）——摘要没有字数上限了，所以"压到多少"是唯一还能看的
    东西，而它只有和"从多少压下来的"放在一起才有意义。
    """

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "nudges": self.nudges,
            "e0_pushbacks": self.e0_pushbacks,
            "parse_errors": self.parse_errors,
            "tool_errors": self.tool_errors,
            "tool_calls": self.tool_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_hit": self.cache_hit,
            "questions": self.questions,
            "segments": self.segments,
        }


def collect(events: list[TraceEvent]) -> Metrics:
    m = Metrics()
    for e in events:
        if e.type.value == "model_response" and e.actor.value == "director":
            m.rounds += 1
            if not e.payload.get("tool_calls"):
                m.nudges += 1
        if e.tokens_in:
            m.tokens_in += e.tokens_in
        if e.tokens_out:
            m.tokens_out += e.tokens_out
        if e.cache_hit:
            m.cache_hit += e.cache_hit

        tool = e.payload.get("tool")
        if tool:
            m.tool_calls[tool] = m.tool_calls.get(tool, 0) + 1
            if e.payload.get("error"):
                m.tool_errors += 1
        if "缺少状态增量" in e.label:
            m.e0_pushbacks += 1
        if "参数解析失败" in e.label or "参数错误" in e.label:
            m.parse_errors += 1
        if tool == "ask_question" and "answer" in e.payload:
            m.questions.append((len(e.payload["options"]), bool(e.payload["free_form"])))
        if "chars_in" in e.payload:
            m.segments.append((e.payload["span"], e.payload["chars_in"], e.payload["chars_out"]))
    return m
