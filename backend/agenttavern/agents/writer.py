"""writer：纯渲染，单次调用。

它是从 director 身上拆下来的一个负担——**只承担文风**，不是流水线下游的规划环节
（D52）。拿不到 state、世界设定、用户原始指令：那些都已经在细纲里了。

**它的输入里没有任何 director 的工具，也没有回流通道**——director 产出细纲即结束，
编排层接手调用它。它写完之后 state_manager 会读这份正文去更新状态（D143），
但那是**下一个阶段**的事，与它无关：这里没有任何东西回到 director 手上。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import Usage
from ..models import Actor, Node, TraceEventType
from ..prompting import upstream_runs, writer_prompt
from ..tools import RunContext
from ..tracing import Tracer

DEVIATION_MARKER = "---偏离---"

# 注入几轮正文上文用于衔接，**由 `Capabilities.upstream_runs` 决定（默认 3）**。
# writer 只需要接住语气，不需要长程记忆——那是 director 的事，它读细纲历史。
# 这个数以前是这里的一个常量，改它要动代码；它其实是每个故事都该自己定的东西
# （节奏慢的故事上文要多给几轮），所以搬进了能力开关。


@dataclass
class WriterResult:
    prose: str
    deviation: str | None
    usage: Usage


async def run_writer(ctx: RunContext, tracer: Tracer, outline: str) -> WriterResult:
    upstream = _recent_prose(ctx)
    messages = writer_prompt(
        outline=outline,
        upstream=upstream,
        # 角色的语言习惯挂在角色条目上（D159），所以 writer 也要看到设定条目。
        # **它仍然不读设定正文**——`render_voices` 只取 `voice` 那一项。
        worldinfo=ctx.repo.worldbook(),
    ).messages

    tracer.emit(
        TraceEventType.CONTEXT_ASSEMBLED,
        Actor.ORCHESTRATOR,
        "装配了 writer 的上下文",
        {"outline_chars": len(outline), "upstream_chars": len(upstream)},
    )

    # **正文是流式最值得的地方**（D160）：它是一次调用、纯文本、而且是用户真正
    # 在等的东西。`stream_to` 在编排层没给出口时返回 None，那条路就退回非流式。
    response = await ctx.gateway.complete(
        messages, user_id=ctx.session.id, on_delta=ctx.stream_to(Actor.WRITER)
    )
    tracer.exchange(Actor.WRITER, messages, response)
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
    nodes: list[Node] = ctx.repo.path_to(ctx.parent_id)[-upstream_runs() :]
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
