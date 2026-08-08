"""goal 判定（D28）。

机制：正常一轮生成结束后检查是否达标，未达标则开启下一 run。**goal 不是模式**——
有无 goal 系统跑法一样，只是 run 末多一个判定。

**达标判定一律交给模型，不降级为代码。** 曾设想把目标绑到结构化维度上做代码判定
（关系=决裂、伏笔=已兑现），那是错的：这些字段的值本就是模型写进去的，代码读取
比对不是独立验证，只是一次查表（D30）。

但这里是 D17 判别式（上下文差 × 验证器）**两个条件罕见地都满足**的场合：
  - 有验证器：用户设定的目标就是明确给定的真值。
  - 有上下文差：判定只需要 [目标 + 已产出内容摘要]，完全不需要创作过程。

因此值得用一次**独立的、干净上下文**的调用，而不是让生成模型顺手判断——生成模型
知道自己想怎么写，会往「已经达标」的方向读。这不复活 critic：频率极低、上下文极短、
输出是 yes/no，成本可忽略。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..llm import LLMGateway, Message, Role, Usage
from ..models import Actor, Goal, Node, TraceEventType
from ..tracing import Tracer

_SYSTEM = (
    "你在判断一个创作目标是否已经达成。只看给出的目标与剧情梗概，"
    "不要考虑写得好不好、也不要揣测后续打算。\n\n"
    "先逐条核对目标里的每一项要求是否都已出现在剧情中，**必须全部核对完**，"
    "不要看到部分符合就下结论。\n\n"
    "最后单独一行输出结论：`达成` 或 `未达成`。"
)


@dataclass
class GoalVerdict:
    reached: bool
    reason: str
    usage: Usage


async def judge_goal(
    gateway: LLMGateway,
    tracer: Tracer,
    goal: Goal,
    path: list[Node],
    *,
    model: str | None = None,
    user_id: str | None = None,
) -> GoalVerdict:
    digest = "\n".join(f"{i + 1}. {n.summary_or_head(200)}" for i, n in enumerate(path))

    response = await gateway.complete(
        [
            Message(role=Role.SYSTEM, content=_SYSTEM),
            Message(
                role=Role.USER, content=f"## 目标\n{goal.description}\n\n## 剧情梗概\n{digest}"
            ),
        ],
        model=model,
        user_id=user_id,
    )

    text = response.content.strip()
    reached = text.splitlines()[-1].strip().startswith("达成") if text else False

    tracer.emit(
        TraceEventType.GOAL_EVALUATED,
        Actor.GOAL_JUDGE,
        "目标已达成" if reached else "目标尚未达成",
        {"goal": goal.description, "verdict": text},
        usage=response.usage,
    )
    return GoalVerdict(reached=reached, reason=text, usage=response.usage)


def budget_exhausted(goal: Goal, runs: int, total_tokens: int) -> str | None:
    """硬上限保底：模型可能永远判不达标，或目标本身不可达（D28）。"""
    if runs >= goal.max_runs:
        return f"已达 run 数上限（{goal.max_runs}）"
    if total_tokens >= goal.max_total_tokens:
        return f"已达 token 上限（{goal.max_total_tokens}）"
    return None
