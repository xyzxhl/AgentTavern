"""工具注册表与调度。

**能力关闭时对应工具不注册**——模型根本看不到它。这比「注册了但拒绝调用」强：
后者仍然占 prompt 体量、仍会被尝试、失败还要消耗一轮往返。工具面越小，
模型的决策空间越小（分配原则 P9）。
"""

from __future__ import annotations

from typing import Any

from ..llm import ToolCall, ToolSpec
from ..models import (
    Capabilities,
    ControlDimensions,
    SummarizeMode,
    WorldbookEntry,
    WorldbookMode,
)
from .base import Tool, ToolResult, error
from .builtin import (
    ask_question_tool,
    history_tools,
    state_tools,
    submit_tool,
    summarize_tool,
    worldinfo_tools,
)
from .context import RunContext


def build_tools(
    capabilities: Capabilities,
    worldinfo: list[WorldbookEntry] | None = None,
    control: ControlDimensions | None = None,
) -> list[Tool[Any]]:
    """按能力开关组装工具面。

    ``submit_outline`` 永远存在——它是 loop 的唯一终止点（D53）。
    """
    tools: list[Tool[Any]] = [submit_tool(require_state_write=capabilities.state)]

    if capabilities.state:
        # `state_query` 是**按需展开**，可以单独关。关掉时 state 每轮全量注入——
        # 没有展开的路还截断长值，就成了有损筛选（D62）。
        tools += state_tools(query=capabilities.state_query)

    if capabilities.history_search:
        tools += history_tools()

    # 全部条目都是常驻注入、或按需展开被关掉时不注册——
    # 没有任何东西可展开，留着只是白占 prompt 体量。
    if capabilities.worldinfo_read and any(
        e.mode is WorldbookMode.ON_DEMAND for e in worldinfo or []
    ):
        tools += worldinfo_tools()

    if capabilities.summarize is SummarizeMode.AUTO:
        tools.append(summarize_tool())

    if capabilities.ask_question:
        # 控制权那一档直接写进它的描述——「越高越少问」讲的是这个工具的调用
        # 积极性，不是创作要求（D140）。
        tools.append(ask_question_tool((control or ControlDimensions()).model_authority))

    return tools


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any]] | None = None) -> None:
        if tools is None:
            tools = build_tools(Capabilities())
        self._tools = {t.name: t for t in tools}

    @classmethod
    def for_session(
        cls,
        capabilities: Capabilities,
        worldinfo: list[WorldbookEntry] | None = None,
        control: ControlDimensions | None = None,
    ) -> ToolRegistry:
        return cls(build_tools(capabilities, worldinfo, control))

    def render(self) -> str:
        """工具面的**索引**：谁在场，各管什么事。

        全文描述随 `tools` 参数一起发出去，这里只列名字和头一句——**它是占位**，
        为的是让「有哪些工具」在提示词的顺序里看得见、位置可编排（D140）。
        内容仍然归工具模块所有：一个工具怎么描述自己，跟它的参数、错误信息、
        注册条件是同一件事，拆开写两处必然对不上。
        """
        return "\n".join(
            f"- `{t.name}`：{(t.description or '').splitlines()[0]}" for t in self._tools.values()
        )

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    async def dispatch(self, ctx: RunContext, call: ToolCall) -> ToolResult:
        """执行一次工具调用。

        参数解析失败、工具不存在、参数不合法——**全都是模型可恢复的错误**，
        返回结构化的 tool result 让它重试，而不是让整个 run 崩掉。
        """
        if call.parse_error is not None:
            return error(
                f"参数不是合法 JSON：{call.parse_error}。请重新调用。",
                label=f"{call.name} 参数解析失败",
            )

        tool = self._tools.get(call.name)
        if tool is None:
            return error(
                f"没有名为 {call.name} 的工具。可用：{', '.join(self.names)}",
                label=f"调用了不存在的工具 {call.name}",
            )

        return await tool.invoke(ctx, call.arguments or {})
