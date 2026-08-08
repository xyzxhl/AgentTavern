"""批量采集：把一个场景的整条输入序列跑完，把每一轮都记下来。

对应流程第 1 步「采集」（docs/Optimization.md §4）。**它不判断质量**——
判断分别是 `sampling_flags`（筛）、`grader`（归因）和人（验收）的事。

与 `smoke` 的区别：smoke 是单轮探针，用来看"模型到底收到了什么"；
这里是多轮批量，用来产出**可统计的样本**。阶段 0/1 一直缺的就是这个——
n=1 的肉眼观察撑不起流程第 4 步的"看哪一类占多数"。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..llm import LLMGateway
from ..models import Capabilities, ControlDimensions, Node, Session, TraceEvent, UserInput
from ..orchestration import execute_run
from ..storage import SessionRepository, Store
from .metrics import Metrics, collect
from .scenarios import Scenario


@dataclass(frozen=True)
class Variant:
    """一个消融/对照配置。``None`` 表示沿用场景自带的设置。

    变体只改 **session 级配置**，不改题材与输入——否则比的就不是同一件事了（P19）。
    """

    name: str
    capabilities: Capabilities | None = None
    control: ControlDimensions | None = None
    note: str = ""

    def apply(self, session: Session) -> Session:
        update: dict[str, Any] = {}
        if self.capabilities is not None:
            update["capabilities"] = self.capabilities
        if self.control is not None:
            update["control"] = self.control
        return session.model_copy(update=update) if update else session


BASELINE = Variant(name="baseline", note="场景自带配置")


@dataclass
class TurnRecord:
    index: int
    user_text: str
    state_before: dict[str, Any]
    state_after: dict[str, Any]
    node: Node | None = None
    notes: list[str] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    events: list[TraceEvent] = field(default_factory=list)
    failure: str | None = None

    @property
    def outline(self) -> str:
        return self.node.outline if self.node else ""

    @property
    def prose(self) -> str:
        return self.node.prose if self.node else ""

    @property
    def ratio(self) -> float | None:
        """正文 / 细纲。隔离是否生效最廉价的指标（D93）。"""
        if not self.outline or not self.prose:
            return None
        return len(self.prose) / len(self.outline)


@dataclass
class BenchResult:
    scenario: str
    variant: str
    session_id: str
    turns: list[TurnRecord] = field(default_factory=list)
    actual_boundaries: set[int] = field(default_factory=set)
    """模型实际切出来的分界点（累计轮次）。由段落的末节点位置反推。"""

    expected_boundaries: set[int] = field(default_factory=set)
    """场景设计时的预期分界。**不喂给模型**，只用来对分。"""

    def boundary_score(self) -> dict[str, Any] | None:
        """分界准确率。没有预期分界的场景返回 ``None``。

        **这是少数几个 program grader 能直接算的语义指标**：分界点是位置，
        位置对不对是二值的，不需要品味——正好卡在 §3 的准入标准上。
        """
        if not self.expected_boundaries:
            return None
        hit = self.actual_boundaries & self.expected_boundaries
        return {
            "预期": sorted(self.expected_boundaries),
            "实际": sorted(self.actual_boundaries),
            "命中": sorted(hit),
            "查全": round(len(hit) / len(self.expected_boundaries), 2),
            "查准": (
                round(len(hit) / len(self.actual_boundaries), 2) if self.actual_boundaries else None
            ),
        }

    @property
    def completed(self) -> list[TurnRecord]:
        return [t for t in self.turns if t.node is not None]

    def totals(self) -> dict[str, Any]:
        done = self.completed
        ratios = [t.ratio for t in done if t.ratio is not None]
        return {
            "turns": len(self.turns),
            "failed": sum(1 for t in self.turns if t.failure),
            "flagged": sum(1 for t in done if t.notes),
            "empty_deltas": sum(1 for t in done if t.node and t.node.state_delta.is_empty()),
            "deviations": sum(1 for t in done if t.node and t.node.deviation_report),
            "rounds_avg": _mean([t.metrics.rounds for t in done]),
            "ratio_avg": _mean(ratios),
            "ratio_min": min(ratios) if ratios else None,
            "outline_chars_avg": _mean([len(t.outline) for t in done]),
            "prose_chars_avg": _mean([len(t.prose) for t in done]),
            "tokens_in": sum(t.metrics.tokens_in for t in done),
            "tokens_out": sum(t.metrics.tokens_out for t in done),
            # 缓存命中率：D114 的**唯一**理由，此前从没被量过。
            "cache_rate": _rate(
                sum(t.metrics.cache_hit for t in done),
                sum(t.metrics.tokens_in for t in done),
            ),
            "nudges": sum(t.metrics.nudges for t in done),
            "e0_pushbacks": sum(t.metrics.e0_pushbacks for t in done),
            "parse_errors": sum(t.metrics.parse_errors for t in done),
            "tool_errors": sum(t.metrics.tool_errors for t in done),
        }


def _rate(hit: int, total: int) -> float | None:
    return round(hit / total, 3) if total else None


def _mean(values: list[float] | list[int]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


async def run_scenario(
    scenario: Scenario,
    gateway: LLMGateway,
    settings: Settings,
    *,
    variant: Variant = BASELINE,
    limit: int | None = None,
    on_turn: Any = None,
) -> BenchResult:
    """把整条输入序列跑完。

    **一轮失败不终止整条序列**——后续轮次仍会在最后一个成功节点上继续。
    否则第一轮的偶发错误会让整批样本作废，而我们要的正是失败样本。
    """
    store = Store(settings.data_dir)
    session = variant.apply(scenario.make_session())
    repo = store.create_session(session)
    for entry in scenario.worldbook:
        repo.save_worldbook_entry(entry)

    result = BenchResult(scenario=scenario.name, variant=variant.name, session_id=session.id)
    inputs = scenario.inputs[:limit] if limit else scenario.inputs
    parent_id: str | None = None

    for i, text in enumerate(inputs):
        record = TurnRecord(
            index=i,
            user_text=text,
            state_before=dict(repo.state_at(parent_id).values),
            state_after={},
        )
        events: list[TraceEvent] = []

        try:
            outcome = await execute_run(
                repo,
                session,
                gateway,
                user_input=UserInput(text=text),
                parent_id=parent_id,
                ask_user=auto_answer,
                on_trace=events.append,
            )
        except Exception as exc:
            record.failure = f"{type(exc).__name__}: {exc}"
        else:
            record.node = outcome.node
            record.notes = outcome.notes
            parent_id = outcome.node.id

        record.events = events
        record.metrics = collect(events)
        record.state_after = dict(repo.state_at(parent_id).values)
        result.turns.append(record)

        if on_turn:
            on_turn(record)

    result.expected_boundaries = scenario.boundaries
    result.actual_boundaries = _actual_boundaries(repo, parent_id)
    return result


def _actual_boundaries(repo: SessionRepository, leaf_id: str | None) -> set[int]:
    """从落盘的段落反推模型实际切在了哪里。

    段落按「末节点 + 跨度」寻址，所以末节点在路径中的位置（1 起）就是分界点。
    **最后一个分界不算**——它是序列结束，不是模型的判断。
    """
    if leaf_id is None:
        return set()
    path = repo.path_to(leaf_id)
    cuts = {i + 1 for i, node in enumerate(path) if node.outline_summary}
    return cuts - {len(path)}


def load_result(settings: Settings, session_id: str) -> BenchResult:
    """从落盘的 session 重建一份可重判的素材。

    **改了 grader 就要能重判同一批素材。** 否则每次校准都得重新生成——既贵，
    而且换了一批生成结果，grader 前后的差异和模型本身的随机性就混在一起了。
    这里不重建 trace 与指标（那些属于采集时的现场），只重建归因需要的四样：
    用户输入、细纲、正文、增量，以及每一轮**开始时**的状态。
    """
    repo = Store(settings.data_dir).session(session_id)
    session = repo.load_session()
    path = repo.path_to(session.active_leaf_id) if session.active_leaf_id else []

    result = BenchResult(scenario=session.meta.title, variant="regrade", session_id=session_id)
    for i, node in enumerate(path):
        result.turns.append(
            TurnRecord(
                index=i,
                user_text=node.user_input.text if node.user_input else "",
                state_before=dict(repo.state_at(node.parent_id).values),
                state_after=dict(repo.state_at(node.id).values),
                node=node,
            )
        )
    return result


async def auto_answer(question: str, options: list[str]) -> str:
    """非交互：取第一个选项。报告里会如实记下问了什么、选了什么。"""
    await asyncio.sleep(0)
    return options[0] if options else ""


def repo_of(settings: Settings, session_id: str) -> SessionRepository:
    return Store(settings.data_dir).session(session_id)
