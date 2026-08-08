"""优化流程的采集与归因工具（docs/Optimization.md §4）。

    采集 → 筛 → 归因 → 统计 → 改 → 回归 → 验收

本包覆盖 1（`runner`）、3（`grader`）、4（`report`），并提供 6 的回归集（`scenarios`）。
2 是 `orchestration.checks.sampling_flags`，5 和 7 是人的事。

**它不属于产品运行时**，只在开发者手里跑。
"""

from .grader import Attribution, Judgement, Verdict, grade_turn
from .metrics import Exchange, Metrics, RecordingGateway, collect
from .runner import BASELINE, BenchResult, TurnRecord, Variant, load_result, run_scenario
from .scenarios import DATA, NOIR, SCENARIOS, Scenario

__all__ = [
    "BASELINE",
    "DATA",
    "NOIR",
    "SCENARIOS",
    "Attribution",
    "BenchResult",
    "Exchange",
    "Judgement",
    "Metrics",
    "RecordingGateway",
    "Scenario",
    "TurnRecord",
    "Variant",
    "Verdict",
    "collect",
    "grade_turn",
    "load_result",
    "run_scenario",
]
