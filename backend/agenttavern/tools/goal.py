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
        description="按先后顺序排列的阶段，每个一句话。\n"
        "**三条要求**：① 是剧情里能发生的事，不是抽象的进度描述"
        "（「他终于肯开口说那天的事」是阶段，「推进人物关系」不是）；"
        "② 达成与否能被看出来——读完那几轮剧情能明确回答它发生了没有；"
        "③ 前后有依赖，顺序不能随便换。\n"
        "数量按目标体量定，通常三到六个。不要为了凑数把一件事切成两半，"
        "也不要把整个目标压成一个阶段——那样等于没拆。",
    )


_PLAN_DESC = """给出（或**重新给出**）到达目标的阶段拆分。

**这些阶段是给你自己用的脚手架，不是必须逐项兑现的合同。** 它们存在的目的是逼你
在动手之前先想清楚路径。所以：

**剧情一旦偏离，就地重调这个工具重拆。** 不要抱着一份已经不成立的计划往下推——
后面那几个阶段建立在「前面按计划走」的假设上，假设垮了它们就是空的。
已经发生的事不要再列成阶段，**从当前局面往下拆**。

重拆不算失败，也不限次数。**但重拆会清空进度**：新计划里的阶段一律从未达成算起。"""


async def _plan(ctx: GoalContext, params: PlanParams) -> ToolResult:
    ctx.run.plan = GoalPlan(subgoals=[SubGoal(description=d) for d in params.subgoals])
    ctx.labels.append(f"拆出 {len(params.subgoals)} 个阶段")
    return ToolResult(
        content="阶段计划已更新（进度重置为全部未达成）。",
        label=f"拆出 {len(params.subgoals)} 个阶段",
        payload={"subgoals": params.subgoals},
    )


class AdvanceParams(BaseModel):
    reached: list[int] = Field(
        default_factory=list,
        description="本轮**新**达成的阶段序号（从 1 数）。没有就给空列表。\n"
        "判定从严：**剧情里真的发生了**才算，「正在朝那个方向走」不算。"
        "一轮可能跨过两个阶段，也可能一个阶段推好几轮都不算达成——"
        "不要为了显得有进展而放宽标准。",
    )
    next_input: str = Field(
        default="",
        description="替用户写的下一条输入，把剧情往**当前那个未达成的阶段**推。\n"
        "写法：是用户角色的动作或话，不是给系统的指令"
        "（写「我把那张照片推过去」，不写「让林昭拿出照片」）；"
        "**一次只做一件事**，不要把三步打包；不要替其他角色决定反应；"
        "不要提阶段、目标、进度这些词——用户不知道有这些东西；"
        "顺着已经写出来的剧情接。\n"
        "**全部阶段都已达成时留空**，然后调用 `finish`。",
    )


async def _advance(ctx: GoalContext, params: AdvanceParams) -> ToolResult:
    plan = ctx.run.plan
    if plan is None:
        return error("还没有阶段计划。先调用 `plan` 拆阶段。", label="没有计划可推进")

    hit = [i for i in params.reached if 1 <= i <= len(plan.subgoals)]
    for index in hit:
        plan.subgoals[index - 1].done = True

    ctx.next_input = params.next_input.strip() or None
    current = plan.current

    ctx.labels.append(f"达成 {len(hit)} 个阶段" if hit else "本轮无新达成")
    return ToolResult(
        content=(
            f"已记下。当前阶段：{current.description if current else '（全部达成）'}"
            + ("" if ctx.next_input else "\n（没有写下一条输入。）")
        ),
        label=(f"达成阶段 {'、'.join(map(str, hit))}" if hit else "核对进度：无新达成"),
        payload={"reached": hit, "next_input": ctx.next_input, "all_done": plan.all_done},
    )


class FinishParams(BaseModel):
    outcome: GoalOutcome = Field(
        description="`done`——目标的每一项要求都已在剧情里兑现。\n"
        "`failed`——**当前局面下拆不出任何能到达目标的路径**。"
        "这一条要慎用：只是这一版计划没走通的话，重调 `plan` 重拆即可。"
    )
    reason: str = Field(
        min_length=1,
        description="`done` 要**逐条**对着目标核对一遍写出来，不要只写一句"
        "「已完成」；`failed` 要说明为什么现在这个局面下拆不出路径。",
    )


async def _finish(ctx: GoalContext, params: FinishParams) -> ToolResult:
    plan = ctx.run.plan
    if params.outcome is GoalOutcome.DONE and not (plan and plan.all_done):
        # **硬前提**：宣布完成之前，必须用**当前的**计划重核一遍（D148）。
        # 它挡的不是"剧情没按计划走"（那种情况该重拆），而是模型的自我确认倾向——
        # 维持对话之后它读的是自己写的计划、自己判的进度，比干净上下文更容易
        # 往"已经达标"的方向读。
        left = [s.description for s in (plan.subgoals if plan else []) if not s.done]
        return error(
            "还有阶段没标为达成，不能判完成：" + ("、".join(left) or "（还没有计划）") + "\n"
            "确实已经不需要它们了，就先调用 `plan` 按当前局面重拆一版，再核对。",
            label="阶段未走完，退回",
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
            "核对阶段进度，并替用户写出下一条输入。**每一轮都要调用一次。**",
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
