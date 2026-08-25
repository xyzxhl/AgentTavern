"""trace 记录。

两条约束（S111 / S47）：trace 完整、机器可读、**不筛选**；UI 是它的投影。
每条事件必须带**人类可读的语义标签**——UI 要能渲染成"确认了她现在在哪"，
而不是 ``state_query(location)``。这部分事后补代价很高。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .llm import Message, ModelResponse, ToolSpec, Usage
from .models import Actor, TraceEvent, TraceEventType
from .storage import SessionRepository

OnEvent = Callable[[TraceEvent], None]


class Tracer:
    """一个 run 的 trace 记录器，顺带累计 token 消耗。"""

    def __init__(
        self,
        repo: SessionRepository,
        run_id: str,
        on_event: OnEvent | None = None,
        *,
        persist: bool = False,
        developer: bool = False,
    ) -> None:
        self._repo = repo
        self._run_id = run_id
        self._seq = 0
        self._on_event = on_event
        self._persist = persist
        """trace 落盘。

        trace 服务于 eval，不是产品功能。关闭时事件仍推给 UI——"呈现 agent
        工作过程"依赖它——只是不写文件。这正是 S111 要求 trace 与 UI 呈现分两层
        的好处：两者可以独立开关。
        """
        self._developer = developer
        """**开发者模式：额外记录每次模型往返的完整输入输出**（S113）。

        它是**服务级**的（启动时选，`Settings.developer`），不是 session 级——
        「这台服务是拿来查问题的还是拿来玩的」不该每个故事各答一遍。

        默认关闭的理由是体量：一次往返的 messages 动辄几万字，四个 agent 加起来
        一轮上十万。开着时 `persist` 也强制打开——记了不落盘，下次就查不了了。
        """
        self.usage = Usage()

    @property
    def developer(self) -> bool:
        return self._developer

    def exchange(
        self,
        actor: Actor,
        messages: list[Message],
        response: ModelResponse,
        *,
        tools: list[ToolSpec] | None = None,
    ) -> None:
        """记一次**完整的**模型往返。开发者模式之外什么都不做。

        **这是唯一能看到 agent 真实收到什么的地方。** 别处的 trace 事件都是
        投影：`CONTEXT_ASSEMBLED` 只有条数和字数，`MODEL_RESPONSE` 只有工具名。
        投影足够回答"它做了什么"，回答不了"它为什么这么做"——后者要看原文，
        而调 prompt 时要看的恰恰是后者。

        用的是 `MODEL_REQUEST`：这个事件类型一直定义着却从没被发出过。
        """
        if not self._developer:
            return
        self.emit(
            TraceEventType.MODEL_REQUEST,
            actor,
            f"{actor.value} 的一次完整往返",
            {
                "messages": [m.model_dump(mode="json", exclude_defaults=True) for m in messages],
                "tools": [t.name for t in tools or []],
                "response": response.model_dump(mode="json", exclude_defaults=True),
            },
        )

    def emit(
        self,
        event_type: TraceEventType,
        actor: Actor,
        label: str,
        payload: dict[str, Any] | None = None,
        usage: Usage | None = None,
    ) -> TraceEvent:
        if usage is not None:
            self.usage = self.usage + usage

        event = TraceEvent(
            run_id=self._run_id,
            seq=self._seq,
            type=event_type,
            actor=actor,
            label=label,
            payload=payload or {},
            tokens_in=usage.tokens_in if usage else None,
            tokens_out=usage.tokens_out if usage else None,
            cache_hit=usage.cache_hit if usage else None,
        )
        self._seq += 1
        if self._persist:
            self._repo.append_trace(event)

        # UI 是 trace 的**投影**（S111）：这里只是把完整事件递出去，
        # 筛选与渲染规则属 UI 层，不得反过来污染 trace schema。
        if self._on_event is not None:
            self._on_event(event)
        return event
