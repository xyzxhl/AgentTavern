"""报告与统计。

**第 4 步「看哪一类占多数」是整个流程的核心**（docs/Optimization.md §4）——
五类的修法完全不同，而最贵的那一类最容易被误判成下游的问题：
director 检索不足导致的细纲错误，看起来像 writer 写砸了。

因此归因分布是这份报告的主体，正文样本只是佐证。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime

from .grader import Attribution, Judgement, Verdict
from .runner import BenchResult

FIX_HINTS = {
    Attribution.RETRIEVAL: "默认包范围、键名一致性、`history_search` 是否该开",
    Attribution.REASONING: "director 提示词、控制维度的措辞映射",
    Attribution.DEVIATION: "writer 提示词、**细纲粒度**（Q21）",
    Attribution.WRITE_MISS: "`state_write` 的工具描述、流程步骤的措辞",
    Attribution.WRITE_NOISE: "`state_write` 里判据的表述、注入格式（state 越长越容易看漏）",
}


def _fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def attribution_table(judgements: list[list[Judgement]]) -> str:
    """归因分布。分母是**判过的轮数**，unknown 单列——它不该被算进任何一边。"""
    counts: dict[Attribution, Counter[str]] = {a: Counter() for a in Attribution}
    for turn in judgements:
        for j in turn:
            counts[j.attribution][j.verdict.value] += 1

    lines = ["| 归因 | 有问题 | 没问题 | 判不出 | 该改什么 |", "|---|---|---|---|---|"]
    for attribution, counter in counts.items():
        issue = counter[Verdict.ISSUE.value]
        ok = counter[Verdict.OK.value]
        unknown = counter[Verdict.UNKNOWN.value]
        total = issue + ok
        rate = f"**{issue}**（{issue / total:.0%}）" if total else str(issue)
        hint = FIX_HINTS[attribution]
        lines.append(f"| {attribution.value} | {rate} | {ok} | {unknown} | {hint} |")

    lines.append("")
    lines.append(
        "**「writer 文风失败」不在表内**——它没有参照物，AI 判不了（D83）。"
        "那一类只能人看，样本见下。"
    )
    return "\n".join(lines)


def render(
    result: BenchResult,
    judgements: list[list[Judgement]],
    *,
    model: str,
    grader_model: str,
) -> str:
    out: list[str] = [
        f"# 批量采集 · {result.scenario} / {result.variant}",
        "",
        f"- 时间：{datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- 生成模型：`{model}`　grader：`{grader_model}`",
        f"- session：`{result.session_id}`",
        f"- 汇总：{json.dumps(result.totals(), ensure_ascii=False)}",
        "",
    ]

    if result.goal_outcome:
        # goal 驱动的场景：**要看的不是产出，是它怎么收的**。三样都得在——
        # 收成什么、为什么、以及收尾时那一版中间状态长什么样。
        out += [
            "## goal",
            "",
            f"- 结局：`{result.goal_outcome}`",
            f"- 理由：{result.goal_reason}",
            "",
            "收尾时的中间状态：",
            "",
            *(f"- {line}" for line in result.goal_plan),
            "",
        ]

    if (score := result.boundary_score()) is not None:
        out += [
            "## 语义分界",
            "",
            "摘要与细纲是 n : 1，n 由模型判断（D110）。下面是它切出来的位置"
            "与设计时预期的对比——**预期分界不喂给模型**。",
            "",
            _fence(json.dumps(score, ensure_ascii=False, indent=2), "json"),
            "",
        ]

    out += [
        "## 归因分布",
        "",
        attribution_table(judgements),
        "",
        "## 逐轮",
        "",
    ]

    for i, turn in enumerate(result.turns):
        out.append(f"### 第 {turn.index + 1} 轮")
        out.append("")
        out.append(f"**输入**：{turn.user_text}")
        out.append("")

        if turn.failure:
            out += [f"**失败**：{turn.failure}", ""]
            continue

        ratio = f"{turn.ratio:.1f}" if turn.ratio else "—"
        out.append(f"细纲 {len(turn.outline)} 字 / 正文 {len(turn.prose)} 字（{ratio}×）")
        out.append("")
        out += ["**细纲**", "", _fence(turn.outline), ""]
        out += ["**正文**", "", _fence(turn.prose), ""]

        if turn.node and turn.node.deviation_report:
            out += [f"**偏离报告**：{turn.node.deviation_report}", ""]

        delta = (
            turn.node.state_delta.model_dump(exclude_defaults=True, mode="json")
            if turn.node
            else {}
        )
        out += [
            "**状态增量**",
            "",
            _fence(json.dumps(delta, ensure_ascii=False, indent=2) if delta else "（空）", "json"),
            "",
        ]

        if turn.notes:
            out += ["**采样标记**", ""] + [f"- {n}" for n in turn.notes] + [""]

        if i < len(judgements):
            issues = [j for j in judgements[i] if j.verdict is not Verdict.OK]
            if issues:
                out += ["**归因**", ""]
                for j in issues:
                    rate = f"{j.hits}/{j.runs}" if j.runs > 1 else j.verdict.value
                    out.append(f"- {j.attribution.value}｜{rate}｜{j.evidence}")
                out.append("")
            else:
                out += ["**归因**：各类均无问题", ""]

    out += [
        "## 待人工判断",
        "",
        "以上正文的**文风**没有被任何自动手段评价过——它没有参照物。",
        "请只看正文，回答：读起来是不是要的东西。",
        "",
    ]
    return "\n".join(out)
