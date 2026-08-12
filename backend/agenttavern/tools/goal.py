"""goal_manager 的三个工具（D148）。

**它们是 session 级的，不是 run 级的**——改的是 `GoalRun`，而 `GoalRun` 跨 run
活着。所以它们吃 `GoalContext` 而不是 `RunContext`；`Tool.handler` 本来就写成
`Callable[[Any, P], ...]`，`ToolRegistry.dispatch` 也随之放宽到 `Any`。
两套工具调度必然漂移，不如让一套认两种 context。

## 为什么是三个

goal_manager 以前把同一件事拆成三次干净调用（`plan` / `step` / `verdict`），
用一个选项组切任务。它们要看的东西完全一样，而真正的判据是连续的：
「哪些达成了」和「剩下的还成不成立」是同一次判断的两面。现在合成一份长对话，
模型自己决定这一轮该调哪几个：

- 第一轮 `plan` + `advance`
- 平常 `advance`
- 剧情偏了 `plan`（就地重拆）+ `advance`
- 该收了 `advance` + `finish`

**收口条件是「这一次不再调用任何工具」**（同 state_manager）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ..models import Goal, GoalOutcome, GoalPlan, GoalRun, SubGoal
from .base import Tool, ToolResult, error


@dataclass
class GoalContext:
    """goal_manager 的工具能碰到的一切。**没有 repo、没有 gateway**——
    它不读存储也不调模型，只改自己那份运行时状态。
    """

    goal: Goal
    run: GoalRun

    next_input: str | None = None
    """这一轮 `advance` 写下的模拟用户输入，由编排层取走喂给下一轮。"""

    finished: tuple[GoalOutcome, str] | None = None
    """`finish` 填的终态。填了就该交回控制权。"""

    labels: list[str] = field(default_factory=list)
    """这一轮做了些什么，给 trace 用。"""


# --------------------------------------------------------------------------


class PlanParams(BaseModel):
    subgoals: list[str] = Field(
        min_length=1,
        description="若干中间状态，每个一句话。\n"
        "**它们描述状态，不是步骤**：「他终于肯开口说那天的事」是中间状态，"
        "「让林昭拿出照片」是步骤。**顺序不构成要求**——可以被跳过、"
        "以不同顺序实现、同时实现，也可能因剧情变化而不再必要。\n"
        "唯一的硬要求是**达成与否能被看出来**：读完剧情能明确回答它成立了没有。\n"
        "数量按目标体量定。不要为了凑数把一件事切成两半，"
        "也不要把整个目标压成一条——那样等于没拆。",
    )


_PLAN_DESC = """给出（或**重新给出**）目标与当前剧情之间的中间状态。

**它们是给你自己用的脚手架，不是必须逐项兑现的合同。** 存在的目的是逼你在动手
之前先想清楚差距在哪。**顺序不构成要求**：可以跳过、乱序、同时达成。

**某一条不再必要，或者剧情已经走到别处，就地重调这个工具重给一版。**
已经成立的状态不要再列，**从当前局面往下看**。

重给不算失败，也不限次数。**但会清空进度**：新的一版一律从未达成算起——
「已达成」不能跨着两版混在一起。"""


async def _plan(ctx: GoalContext, params: PlanParams) -> ToolResult:
    ctx.run.plan = GoalPlan(subgoals=[SubGoal(description=d) for d in params.subgoals])
    ctx.labels.append(f"给出 {len(params.subgoals)} 个中间状态")
    return ToolResult(
        content="中间状态已更新（进度重置为全部未成立）。",
        label=f"给出 {len(params.subgoals)} 个中间状态",
        payload={"subgoals": params.subgoals},
    )


class AdvanceParams(BaseModel):
    reached: list[int] = Field(
        default_factory=list,
        description="本轮**新**成立的中间状态序号（从 1 数）。没有就给空列表。\n"
        "判定从严：**剧情里真的发生了**才算，「正在朝那个方向走」「达成条件已经"
        "备好」都不算。一轮可能同时成立两条，也可能推好几轮一条都不成立——"
        "不要为了显得有进展而放宽标准。",
    )
    next_input: str = Field(
        default="",
        description="替用户写的下一条输入。\n"
        "**只施加最小必要干预**：优先创造信息、选择、机会、冲突、条件、动机，"
        "或可供角色响应的情境；避免直接制造目标要求的最终结果。\n"
        "不要提中间状态、目标、进度这些词——用户不知道有这些东西；"
        "顺着已经写出来的剧情接。\n"
        "**目标已经成立时留空**，然后调用 `finish`。",
    )


async def _advance(ctx: GoalContext, params: AdvanceParams) -> ToolResult:
    plan = ctx.run.plan
    if plan is None:
        return error("还没有中间状态。先调用 `plan` 给一版。", label="没有可核对的中间状态")

    hit = [i for i in params.reached if 1 <= i <= len(plan.subgoals)]
    for index in hit:
        plan.subgoals[index - 1].done = True

    ctx.next_input = params.next_input.strip() or None

    # **回执列的是「还剩哪些」，不是「下一个是哪个」**（D150）：中间状态之间
    # 没有先后依赖，挑出一个说"当前这个"等于凭空造了个顺序。
    left = [f"{i + 1}. {s.description}" for i, s in enumerate(plan.subgoals) if not s.done]
    ctx.labels.append(f"成立 {len(hit)} 条" if hit else "本轮无新成立")
    return ToolResult(
        content=(
            ("尚未成立：\n" + "\n".join(left) if left else "全部中间状态都已成立。")
            + ("" if ctx.next_input else "\n（没有写下一条输入。）")
        ),
        label=(f"成立 {'、'.join(map(str, hit))}" if hit else "核对进度：无新成立"),
        payload={"reached": hit, "next_input": ctx.next_input, "all_done": plan.all_done},
    )


class FinishParams(BaseModel):
    outcome: GoalOutcome = Field(
        description="`done`——目标剧情状态的每一项都已在剧情里成立。\n"
        "`failed`——**当前局面下再也走不到目标了**。"
        "这一条要慎用：只是这一版中间状态没走通的话，重调 `plan` 重给一版即可。"
    )
    reason: str = Field(
        min_length=1,
        description="`done` 要**逐条**对着目标核对一遍写出来，不要只写一句"
        "「已完成」；`failed` 要说明为什么现在这个局面下再也走不到。",
    )


async def _finish(ctx: GoalContext, params: FinishParams) -> ToolResult:
    plan = ctx.run.plan
    if params.outcome is GoalOutcome.DONE and not (plan and plan.all_done):
        # **硬前提**：宣布完成之前，必须用**当前的**那一版重核一遍（D148）。
        # 它挡的不是"剧情没按中间状态走"（那种情况该重给一版），而是模型的自我
        # 确认倾向——维持对话之后它读的是自己给的状态、自己判的进度，
        # 比干净上下文更容易往"已经达标"的方向读。
        left = [s.description for s in (plan.subgoals if plan else []) if not s.done]
        return error(
            "还有中间状态没标为成立，不能判完成：" + ("、".join(left) or "（还没有拆过）") + "\n"
            "它们确实已经不必要了，就先调用 `plan` 按当前局面重给一版，再核对。",
            label="尚有未成立的中间状态，退回",
        )

    ctx.finished = (params.outcome, params.reason)
    ctx.labels.append("目标完成" if params.outcome is GoalOutcome.DONE else "目标失败")
    return ToolResult(
        content="已记录。",
        label="目标完成" if params.outcome is GoalOutcome.DONE else "目标失败",
        payload={"outcome": params.outcome.value, "reason": params.reason},
    )


def _tool(name: str, description: str, params: type[BaseModel], handler: Any) -> Tool[Any]:
    return Tool(name=name, description=description, params_type=params, handler=handler)


def goal_tools() -> list[Tool[Any]]:
    return [
        _tool("plan", _PLAN_DESC, PlanParams, _plan),
        _tool(
            "advance",
            "核对中间状态，并替用户写出下一条输入。**每一轮都要调用一次。**",
            AdvanceParams,
            _advance,
        ),
        _tool(
            "finish",
            "结束这个目标，交回控制权。**只有两种终态。**\n"
            "预算不够了不用调它——那是暂停，用户可以加预算接着跑。",
            FinishParams,
            _finish,
        ),
    ]


__all__ = ["GoalContext", "goal_tools"]
