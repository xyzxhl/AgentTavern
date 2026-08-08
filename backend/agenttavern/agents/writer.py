"""writer：纯渲染，单次调用。

它是从 director 身上拆下来的一个负担——**只承担文风**，不是流水线下游的规划环节
（D52）。拿不到 state、世界设定、用户原始指令：那些都已经在细纲里了。

**它的输入里没有任何 director 的工具，也没有回流通道**——director 产出细纲即结束，
编排层接手调用它，正文不回流（D54）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import Message, Role, Usage
from ..models import Actor, Node, TraceEventType
from ..tools import RunContext
from ..tracing import Tracer
from .prompts import writer_system_prompt

DEVIATION_MARKER = "---偏离---"

RECENT_PROSE = 2
"""注入几轮正文上文用于衔接。writer 只需要接住语气，不需要长程记忆——
那是 director 的事，它读细纲历史。
"""


@dataclass
class WriterResult:
    prose: str
    deviation: str | None
    usage: Usage


async def run_writer(ctx: RunContext, tracer: Tracer, outline: str) -> WriterResult:
    system = writer_system_prompt(ctx.session.meta, ctx.session.control)

    messages = [Message(role=Role.SYSTEM, content=system)]

    upstream = _recent_prose(ctx)
    if upstream:
        messages.append(Message(role=Role.USER, content=f"## 上文\n{upstream}"))

    messages.append(Message(role=Role.USER, content=f"## 本轮细纲\n{outline}"))

    tracer.emit(
        TraceEventType.CONTEXT_ASSEMBLED,
        Actor.ORCHESTRATOR,
        "装配了 writer 的上下文",
        {"outline_chars": len(outline), "upstream_chars": len(upstream)},
    )

    response = await ctx.gateway.complete(messages, user_id=ctx.session.id)
    prose, deviation = _split_deviation(response.content)

    tracer.emit(
        TraceEventType.PROSE_PRODUCED,
        Actor.WRITER,
        "正文完成" if deviation is None else "正文完成（有偏离）",
        {"chars": len(prose), "deviation": deviation},
        usage=response.usage,
    )

    return WriterResult(prose=prose, deviation=deviation, usage=response.usage)


def _recent_prose(ctx: RunContext) -> str:
    if ctx.parent_id is None:
        return ""
    nodes: list[Node] = ctx.repo.path_to(ctx.parent_id)[-RECENT_PROSE:]
    return "\n\n".join(n.prose for n in nodes if n.prose)


def _split_deviation(raw: str) -> tuple[str, str | None]:
    """拆出偏离报告（D57）。

    writer 不改写细纲，只报告实质增删；编排层记录在节点上，下一 run 的 director
    读细纲历史时可见。这是**兜底与归因信号**，不是常规通路——writer 本就不该
    增删实质内容。
    """
    if DEVIATION_MARKER not in raw:
        return raw.strip(), None
    prose, _, report = raw.partition(DEVIATION_MARKER)
    note = report.strip()
    return prose.strip(), note or None
