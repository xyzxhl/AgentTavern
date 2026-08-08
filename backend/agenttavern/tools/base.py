"""工具的基础类型。

一条贯穿的原则：**参数错误是模型可恢复的错误**，返回结构化的 tool result 让它重试；
只有真正的故障（存储损坏之类）才让 run 失败。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from ..llm import ToolSpec

P = TypeVar("P", bound=BaseModel)


class ToolOutcome(StrEnum):
    CONTINUE = "continue"

    TERMINATE = "terminate"
    """仅 ``submit_outline``。它是特殊的：调用即结束 loop，并把细纲交付给 writer。"""


class ToolResult(BaseModel):
    content: str
    """回给模型的内容。"""

    outcome: ToolOutcome = ToolOutcome.CONTINUE
    is_error: bool = False

    label: str = ""
    """**人类可读的语义标签**，供 UI 投影（D25）。

    例："确认了她现在在哪"，而不是 ``state_query(location)``。这是"呈现 agent
    工作过程"这一体验的架构侧前提，事后补代价很高。
    """

    payload: dict[str, Any] = {}
    """机器可读的完整细节，进 trace，不做筛选。"""


def error(message: str, *, label: str = "") -> ToolResult:
    return ToolResult(content=message, is_error=True, label=label or message)


class Tool(BaseModel, Generic[P]):
    """一个工具：名字、描述、参数模型、处理函数。

    参数模型用 Pydantic 定义，JSON Schema 由它导出——工具签名与校验共用一份定义，
    不会漂移。
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    description: str
    params_type: type[P]
    handler: Callable[[Any, P], Awaitable[ToolResult]]

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.params_type.model_json_schema(),
        )

    async def invoke(self, ctx: Any, raw_arguments: dict[str, Any]) -> ToolResult:
        try:
            params = self.params_type.model_validate(raw_arguments)
        except ValidationError as exc:
            return error(
                f"参数不合法：{exc.errors(include_url=False)}",
                label=f"{self.name} 参数错误",
            )
        return await self.handler(ctx, params)
