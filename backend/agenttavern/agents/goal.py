"""goal agent（D28 / D141）。

**它是第三个 agent，不是 director 顺手做的一步。** 上下文只有四样：目标、
阶段计划、剧情梗概（各轮细纲的摘要）、事件密度。**它不读正文**（同 D54 的
理由），也不读世界设定和状态——它管的是"推到哪儿了"，不是"这一轮怎么写"。

## 为什么值得独立出来

这是 D17 判别式（上下文差 × 验证器）**两个条件罕见地都满足**的场合：

- **有验证器**：用户设定的目标就是明确给定的真值。
- **有上下文差**：判定与规划只需要 [目标 + 已产出内容摘要]，完全不需要创作过程。

所以它值得用**独立的、干净上下文**的调用，而不是让生成模型顺手判断——生成模型
知道自己想怎么写，会往"已经达标"的方向读。这不复活 critic：频率极低（一轮一次）、
上下文极短、输出是几行文本。

## 它做三件事

1. **拆阶段**（`plan_subgoals`）——把目标拆成若干阶段性 subgoal。
2. **推一步**（`take_step`）——核对哪些阶段已达成，并**替用户写出下一条输入**，
   把剧情往当前那个未达成的阶段推。这两件事共用同一份上下文，所以合成一次调用。
   **不要求每轮达成一个阶段**：一个阶段可以推好几轮，一轮也可能跨过两个。
3. **判去向**（`final_verdict`）——全部阶段走完之后，目标是完成、完全偏离，
   还是该重新规划。

## 三种去向为什么不能并成两种

「完成」和「完全偏离」都交回控制权，看起来可以合并。不行：**用户需要知道是哪一种**。
一个是达成了，一个是这个目标已经不可能达成了，后续动作完全不同。

「重新规划」这条路本身没有出口——每次重规划都会给出新的阶段，模型永远可以再拆
一层。所以 `Goal.max_revisions` 是止损，和 `max_runs` / `max_total_tokens` 一个性质。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..llm import LLMGateway, Usage
from ..models import (
    Actor,
    ExpansionDensity,
    Goal,
    GoalOutcome,
    GoalPlan,
    Node,
    SubGoal,
    TraceEventType,
)
from ..prompting import assemble
from ..tracing import Tracer

_OUTCOMES = {
    "完成": GoalOutcome.DONE,
    "完全偏离": GoalOutcome.DERAILED,
    "继续": GoalOutcome.REPLAN,
}
"""结论那一行的字样。**它和 `goal.yaml` 里那三个词是一对**，改一处必须改另一处——
这是提示词和解析之间少数几处真的耦合。
"""


@dataclass
class GoalStep:
    """推一步的结果：更新过的计划 + 替用户写的那条输入。"""

    plan: GoalPlan
    user_text: str
    usage: Usage = field(default_factory=Usage)


def digest_of(path: list[Node]) -> str:
    """剧情梗概：各轮细纲的摘要，按顺序。**不含正文**（D54）。"""
    return "\n".join(f"{i + 1}. {n.summary_or_head(200)}" for i, n in enumerate(path))


def plan_along(path: list[Node]) -> GoalPlan | None:
    """当前计划：沿路径往回找，**最后一个带计划的节点**上那份（D48）。

    和 `state_delta` 一样是增量思路——换一条分支自然拿到那条分支自己的进度。
    """
    return next((n.goal_plan for n in reversed(path) if n.goal_plan is not None), None)


async def _ask(
    gateway: LLMGateway,
    task: str,
    *,
    goal: Goal,
    plan: GoalPlan | None,
    digest: str,
    density: ExpansionDensity,
    model: str | None,
    user_id: str | None,
    enabled: bool,
) -> tuple[str, Usage]:
    """三件事共用的一次调用。**任务由 `task` 那个选项组切**，上下文完全一样。"""
    prompt = assemble(
        "goal",
        contexts={
            "goal": goal.description,
            "plan": plan.render() if plan else "",
            "digest": digest,
        },
        options={"task": task if enabled else "off", "pace": density.value},
    )
    response = await gateway.complete(prompt.messages, model=model, user_id=user_id)
    return response.content.strip(), response.usage


async def plan_subgoals(
    gateway: LLMGateway,
    tracer: Tracer,
    goal: Goal,
    path: list[Node],
    *,
    density: ExpansionDensity = ExpansionDensity.NORMAL,
    previous: GoalPlan | None = None,
    model: str | None = None,
    user_id: str | None = None,
    enabled: bool = True,
) -> GoalPlan:
    """把目标拆成阶段。``previous`` 非空即为**重新规划**——版本号接着往上加。"""
    content, usage = await _ask(
        gateway,
        "plan",
        goal=goal,
        # 重新规划时不给旧计划：那一版已经判定为走不通了，给了只会被照抄。
        # 该依据的是剧情梗概——**从当前局面往下拆**，这是提示词里明写的。
        plan=None,
        digest=digest_of(path),
        density=density,
        model=model,
        user_id=user_id,
        enabled=enabled,
    )

    subgoals = [
        SubGoal(description=body)
        for line in content.splitlines()
        if (body := line.strip().lstrip("-*0123456789.、 ").strip())
    ]
    plan = GoalPlan(subgoals=subgoals, revision=(previous.revision + 1) if previous else 1)

    tracer.emit(
        TraceEventType.GOAL_EVALUATED,
        Actor.GOAL_AGENT,
        f"拆出 {len(subgoals)} 个阶段（第 {plan.revision} 版）",
        {"goal": goal.description, "subgoals": [s.description for s in subgoals]},
        usage=usage,
    )
    return plan


async def take_step(
    gateway: LLMGateway,
    tracer: Tracer,
    goal: Goal,
    path: list[Node],
    plan: GoalPlan,
    *,
    density: ExpansionDensity = ExpansionDensity.NORMAL,
    model: str | None = None,
    user_id: str | None = None,
    enabled: bool = True,
) -> GoalStep:
    """核对进度，并替用户写出下一条输入。

    **两件事一次调用**：它们要看的东西完全一样（目标、计划、梗概），拆成两次
    只是把同一份上下文付两遍钱。
    """
    content, usage = await _ask(
        gateway,
        "step",
        goal=goal,
        plan=plan,
        digest=digest_of(path),
        density=density,
        model=model,
        user_id=user_id,
        enabled=enabled,
    )

    reached, user_text = _parse_step(content)
    updated = plan.model_copy(deep=True)
    for index in reached:
        if 1 <= index <= len(updated.subgoals):
            updated.subgoals[index - 1].done = True

    current = updated.current
    tracer.emit(
        TraceEventType.GOAL_EVALUATED,
        Actor.GOAL_AGENT,
        f"本轮达成 {len(reached)} 个阶段" if reached else "本轮没有新达成的阶段",
        {
            "reached": sorted(reached),
            "current": current.description if current else "（全部达成）",
            "user_text": user_text,
        },
        usage=usage,
    )
    return GoalStep(plan=updated, user_text=user_text, usage=usage)


def _parse_step(content: str) -> tuple[set[int], str]:
    """解析两行输出。**认不出来就当没达成、没输入**，不猜（同 grader 的做法）。"""
    reached: set[int] = set()
    user_text = ""
    for line in content.splitlines():
        line = line.strip().lstrip("-* ").strip()
        body = line.split("：", 1)[-1].split(":", 1)[-1].strip()
        if line.startswith("已达成"):
            reached = {int(part) for part in body.replace("，", ",").split(",") if part.isdigit()}
        elif line.startswith("输入"):
            user_text = body
    return reached, user_text


async def final_verdict(
    gateway: LLMGateway,
    tracer: Tracer,
    goal: Goal,
    path: list[Node],
    plan: GoalPlan,
    *,
    density: ExpansionDensity = ExpansionDensity.NORMAL,
    model: str | None = None,
    user_id: str | None = None,
    enabled: bool = True,
) -> tuple[GoalOutcome, str]:
    """全部阶段走完之后，目标的三种去向之一。**认不出来就当「继续」**。"""
    content, usage = await _ask(
        gateway,
        "verdict",
        goal=goal,
        plan=plan,
        digest=digest_of(path),
        density=density,
        model=model,
        user_id=user_id,
        enabled=enabled,
    )

    last = content.splitlines()[-1].strip().lstrip("-*` ").strip() if content else ""
    outcome = next(
        (value for word, value in _OUTCOMES.items() if last.startswith(word)),
        GoalOutcome.REPLAN,
    )

    tracer.emit(
        TraceEventType.GOAL_EVALUATED,
        Actor.GOAL_AGENT,
        {
            GoalOutcome.DONE: "目标已完成",
            GoalOutcome.DERAILED: "剧情已完全偏离目标",
            GoalOutcome.REPLAN: "目标尚未完成，重新规划阶段",
        }[outcome],
        {"goal": goal.description, "verdict": content},
        usage=usage,
    )
    return outcome, content


def budget_exhausted(goal: Goal, runs: int, total_tokens: int) -> str | None:
    """硬上限保底：模型可能永远判不达标，或目标本身不可达（D28）。"""
    if runs >= goal.max_runs:
        return f"已达 run 数上限（{goal.max_runs}）"
    if total_tokens >= goal.max_total_tokens:
        return f"已达 token 上限（{goal.max_total_tokens}）"
    return None
