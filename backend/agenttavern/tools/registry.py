"""工具注册表与调度。

**关掉的工具不注册**——模型根本看不到它。这比「注册了但拒绝调用」强：
后者仍然占 prompt 体量、仍会被尝试、失败还要消耗一轮往返。工具面越小，
模型的决策空间越小（分配原则 P9）。

**开关住在各 agent 的 YAML 里**（S80）：`tools.<名字>.enabled`。它们以前是
`Capabilities` 上的一排布尔值——**但它们从来不是独立的配置，是某个工具的属性**，
而工具描述本来就在那份文件里（R31）。开关和它管的那段字住在一起，
改一个工具的一切（描述、档位、开不开）只在一处。
"""

from __future__ import annotations

from typing import Any

from ..llm import ToolCall, ToolSpec
from ..models import WorldbookEntry, WorldbookMode
from ..models.corpus import Corpus
from ..prompting import enabled, tool_enabled
from .base import Tool, ToolResult, error
from .builtin import (
    ask_question_tool,
    history_tools,
    state_query_tool,
    state_write_tool,
    submit_tool,
    summarize_tool,
    worldinfo_tools,
)
from .corpus import corpus_tool
from .goal import goal_tools


def build_tools(
    worldinfo: list[WorldbookEntry] | None = None,
    corpora: list[Corpus] | None = None,
) -> list[Tool[Any]]:
    """director 的工具面。

    ``submit_outline`` 永远存在——它是 loop 的唯一终止点（S4）。

    **`state_write` 不在这里**（S7）：状态维护后置成了独立的 state_manager，
    见 `build_state_tools`。director 只读状态。
    """
    tools: list[Tool[Any]] = [submit_tool()]

    # `state_query` 是**按需展开**，可以单独关。关掉时 state 每轮全量注入——
    # 没有展开的路还隐藏长值，就成了有损筛选（P13）。
    # state_manager 整个关掉时它也没意义：查一份永远是空的状态。
    if enabled("state_manager") and tool_enabled("director", "state_query"):
        tools.append(state_query_tool("director"))

    if tool_enabled("director", "history_search"):
        tools += history_tools()

    # 全部条目都是常驻注入、或按需展开被关掉时不注册——
    # 没有任何东西可展开，留着只是白占 prompt 体量。
    if tool_enabled("director", "worldinfo_read") and any(
        e.mode is WorldbookMode.ON_DEMAND and e.is_lore for e in worldinfo or []
    ):
        tools += worldinfo_tools()

    # **一份资料都没挂就不注册**（S145）：同上一条，注册一个没东西可查的工具
    # 白占提示词体量、还会被试着调一次。还在建索引的那几份**算挂着**——
    # 那时工具要在，才说得出「还在建索引」（S144）；不注册的话模型只会觉得
    # 这局故事根本没有资料可查。
    if corpora and tool_enabled("director", "corpus_search"):
        tools.append(corpus_tool())

    if tool_enabled("director", "summarize"):
        tools.append(summarize_tool())

    if tool_enabled("director", "ask_question"):
        # 控制权那一档直接写进它的描述——「越高越少问」讲的是这个工具的调用
        # 积极性，不是创作要求（S10②），所以它是那条描述自己的 `choice`。
        tools.append(ask_question_tool())

    return tools


def build_state_tools() -> list[Tool[Any]]:
    """state_manager 的工具面：**只有 state 那两个**（S7）。

    它不查历史、不展开设定、不问用户、也没有 `submit_outline`——它的活是把
    这一轮的细纲和正文折成几个键值，需要的东西都已经在上下文里。

    **没有终止工具是有意的**：loop 的出口是"这一次不再调用任何工具"。
    director 需要 `submit_outline` 是因为它要交付一件**产物**，交付这个动作本身
    得有个地方承接；state_manager 的产物是那几次写入本身，写完就没事了。
    """
    tools: list[Tool[Any]] = [state_write_tool()]
    if tool_enabled("state_manager", "state_query"):
        tools.append(state_query_tool("state_manager"))
    return tools


class ToolRegistry:
    def __init__(self, tools: list[Tool[Any]] | None = None) -> None:
        self._tools = {t.name: t for t in (build_tools() if tools is None else tools)}

    @classmethod
    def for_session(
        cls,
        worldinfo: list[WorldbookEntry] | None = None,
        corpora: list[Corpus] | None = None,
    ) -> ToolRegistry:
        return cls(build_tools(worldinfo, corpora))

    @classmethod
    def for_state_manager(cls) -> ToolRegistry:
        return cls(build_state_tools())

    @classmethod
    def for_goal_manager(cls) -> ToolRegistry:
        """goal_manager 的工具面：`plan` / `check` / `steer` / `finish`（S95/S94）。

        **不吃开关**——goal 要么在跑要么不在，没有中间档；这四个是它全部的
        表达方式，缺一个就有话说不出来。
        """
        return cls(goal_tools())

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    async def dispatch(self, ctx: Any, call: ToolCall) -> ToolResult:
        """执行一次工具调用。

        ``ctx`` 放宽成 `Any`：run 级的工具吃 `RunContext`，goal 的三个吃
        `GoalContext`（它跨 run 活着，S92）。**两套工具调度必然漂移**，
        不如让一套认两种 context——`Tool.handler` 本来就是这么写的。

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
