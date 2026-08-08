"""trace 记录。

两条约束（D25 / D50）：trace 完整、机器可读、**不筛选**；UI 是它的投影。
每条事件必须带**人类可读的语义标签**——UI 要能渲染成"确认了她现在在哪"，
而不是 ``state_query(location)``。这部分事后补代价很高。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .llm import Usage
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
    ) -> None:
        self._repo = repo
        self._run_id = run_id
        self._seq = 0
        self._on_event = on_event
        self._persist = persist
        """**落盘只在开发者模式下发生**（Capabilities.trace，默认关闭）。

        trace 服务于 eval，不是产品功能。关闭时事件仍推给 UI——"呈现 agent
        工作过程"依赖它——只是不写文件。这正是 D25 要求 trace 与 UI 呈现分两层
        的好处：两者可以独立开关。
        """
        self.usage = Usage()

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

        # UI 是 trace 的**投影**（D25）：这里只是把完整事件递出去，
        # 筛选与渲染规则属 UI 层，不得反过来污染 trace schema。
        if self._on_event is not None:
            self._on_event(event)
        return event
