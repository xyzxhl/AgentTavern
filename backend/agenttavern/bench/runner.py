"""批量采集：把一个场景的整条输入序列跑完，把每一轮都记下来。

对应流程第 1 步「采集」（docs/Optimization.md §4）。**它不判断质量**——
判断分别是 `sampling_flags`（筛）、`grader`（归因）和人（验收）的事。

与 `smoke` 的区别：smoke 是单轮探针，用来看"模型到底收到了什么"；
这里是多轮批量，用来产出**可统计的样本**。阶段 0/1 一直缺的就是这个——
n=1 的肉眼观察撑不起流程第 4 步的"看哪一类占多数"。
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Any

from ..agents import advance_goal, sync_conversation
from ..config import Settings
from ..llm import LLMGateway, Usage
from ..models import (
    GoalRun,
    InputSource,
    Node,
    TraceEvent,
    UserInput,
)
from ..orchestration import execute_run
from ..prompting import PromptSetup, player_character, prompts_as
from ..storage import SessionRepository, Store
from ..tracing import Tracer
from .metrics import Metrics, collect
from .scenarios import Scenario


@dataclass(frozen=True)
class Variant:
    """一个消融/对照配置。**它改的是提示词，不是 session**（D174）。

    以前它往 session 上写 `capabilities` / `control`。那两个类整体上调到了
    提示词层之后，这条路失去了全部着力点——而 P19 要求同一批素材跑多档，
    回归集不能就这么散掉。

    **补法是换一份 YAML，不是开一个 `overrides` 后门**：一个"只有程序能用"
    的第二取值入口迟早会有第二个调用者，而那正是 D173 要消掉的东西。
    `prompts_as` 把 `prompting/` 复制一份、在副本上改那几档，跑完就删。

    变体只改配置，不改题材与输入——否则比的就不是同一件事了（P19）。
    """

    name: str
    prompts: PromptSetup = field(default_factory=PromptSetup)
    note: str = ""

    def over(self, base: PromptSetup) -> PromptSetup:
        """叠在场景自带那份之上。**场景先定，变体再改**——变体提到的那几档
        盖掉场景的，没提到的沿用（比如换 `history_search` 不该顺手把文风也换了）。
        """
        return PromptSetup(
            choices=_layer(base.choices, self.prompts.choices),
            tools=_layer(base.tools, self.prompts.tools),
            tool_choices=_layer(base.tool_choices, self.prompts.tool_choices),
            agents={**base.agents, **self.prompts.agents},
        )


def _layer(base: Any, top: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {k: dict(v) for k, v in base.items()}
    for agent, items in top.items():
        out.setdefault(agent, {}).update(items)
    return out


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

    goal_outcome: str = ""
    goal_reason: str = ""
    """goal 怎么收的：`done` / `failed` / `paused:<理由>` / 空（没跑到头）。"""

    goal_plan: list[str] = field(default_factory=list)
    """收尾时那一版中间状态（带成立与否）。看它重给过几版要翻 trace。"""

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
        segments = [s for t in done for s in t.metrics.segments]
        questions = [q for t in done for q in t.metrics.questions]
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
            # ask_question（D155）。**`questions_not_three` 是契约检查**：
            # 参数上写死了恰好三个，这里量的是模型实际给了几个。
            "questions": len(questions),
            "questions_not_three": sum(1 for n, _ in questions if n != 3),
            "questions_free_form": sum(1 for _, free in questions if free),
            # 摘要**没有上限了，只统计**（D154）。看的是压缩比而不是绝对字数：
            # 249 字是 10× 还是 1.6×，取决于它从多少压下来。
            "segments": len(segments),
            "summary_chars_avg": _mean([out for _, _, out in segments]),
            "compression_avg": _mean([src / out for _, src, out in segments if out]),
            "compression_min": (
                round(min(src / out for _, src, out in segments if out), 2) if segments else None
            ),
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

    **整条序列跑在一份改过的提示词副本上**（D174）：场景自带的那几档（文风、
    要不要检索）加上变体要消融的那几处，一起写进副本，跑完删掉——仓库里那份
    不会被动过。
    """
    with prompts_as(variant.over(scenario.prompts)):
        return await _run_scenario(
            scenario, gateway, settings, variant=variant, limit=limit, on_turn=on_turn
        )


async def _run_scenario(
    scenario: Scenario,
    gateway: LLMGateway,
    settings: Settings,
    *,
    variant: Variant = BASELINE,
    limit: int | None = None,
    on_turn: Any = None,
) -> BenchResult:
    store = Store(settings.data_dir)
    session = scenario.make_session()
    repo = store.create_session(session)
    # 设定是**一级资产**（D170）：场景把整份都建起来。**它只有条目**（D175）——
    # 题材与基调也是其中一条，worldbook 上除了名字没有别的字段。
    book = repo.book()
    book.save(book.load().model_copy(update={"name": scenario.world_name}))
    for entry in scenario.worldbook:
        book.save_entry(entry)

    result = BenchResult(scenario=scenario.name, variant=variant.name, session_id=session.id)
    pending = list(scenario.inputs[:limit] if limit else scenario.inputs)
    parent_id: str | None = None
    goal_input: str | None = None
    index = 0

    # **带 goal 的场景由 goal_manager 接着驱动**：场景只给种子输入，之后每一轮的
    # 输入都是它写的。跑到 goal 收尾、预算耗尽或 `limit` 为止。
    while True:
        if pending:
            text, source = pending.pop(0), InputSource.USER
        elif goal_input:
            text, source, goal_input = goal_input, InputSource.SIMULATED, None
        else:
            break
        if limit is not None and index >= limit:
            break

        record = TurnRecord(
            index=index,
            user_text=text,
            state_before=dict(repo.state_at(parent_id).values),
            state_after={},
        )
        events: list[TraceEvent] = []

        try:
            outcome = await execute_run(
                repo,
                # **每轮重新载入**。`execute_run` 结尾会把这个对象存回去，
                # 而 goal_manager 在上一轮末尾往盘上写过 `goal_run`——
                # 跨轮持有同一个副本的话，那份进度每轮都被旧值覆盖掉。
                repo.load_session(),
                gateway,
                user_input=UserInput(text=text, source=source),
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
            goal_input = await _drive_goal(repo, gateway, parent_id, events, result, outcome.usage)

        record.events = events
        record.metrics = collect(events)
        record.state_after = dict(repo.state_at(parent_id).values)
        result.turns.append(record)
        index += 1

        if on_turn:
            on_turn(record)
        if result.goal_outcome:
            break

    result.expected_boundaries = scenario.boundaries
    result.actual_boundaries = _actual_boundaries(repo, parent_id)
    return result


async def _drive_goal(
    repo: SessionRepository,
    gateway: LLMGateway,
    leaf_id: str,
    events: list[TraceEvent],
    result: BenchResult,
    usage: Usage,
) -> str | None:
    """run 末跑一次 goal_manager，返回它写的下一条输入（D148）。

    这是 `SessionRunner._check_goal` 的 bench 版：没有 WebSocket、没有队列，
    但**判定与预算的语义必须一致**——否则量出来的不是产品跑起来的样子。
    ``usage`` 因此也一样：刚跑完那一轮的开销要算进 goal 的预算。
    """
    session = repo.load_session()
    goal = session.meta.goal
    if goal is None:
        return None

    run = session.goal_run or GoalRun(started_at_leaf_id=repo.load_node(leaf_id).parent_id)
    tracer = Tracer(repo, leaf_id, events.append, persist=True)

    sync_conversation(
        player_character(repo.worldbook()),
        goal,
        run,
        repo.path_to(leaf_id),
    )
    step = await advance_goal(gateway, tracer, goal, run, user_id=session.id)
    run.spent_runs += 1
    run.spent_tokens += (
        usage.tokens_in + usage.tokens_out + tracer.usage.tokens_in + tracer.usage.tokens_out
    )

    if run.plan is not None:
        result.goal_plan = run.plan.render().splitlines()

    if step.finished is not None:
        result.goal_outcome, result.goal_reason = step.finished
        session.meta.goal, session.goal_run = None, None
        repo.save_session(session)
        return None

    spent = run.exhausted(goal)
    if spent is not None:
        run.paused, run.paused_reason = True, spent
        result.goal_outcome, result.goal_reason = "paused", spent

    session.goal_run = run
    repo.save_session(session)
    return step.next_input


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


_answered = itertools.count()


async def auto_answer(question: str, options: list[str]) -> str:
    """非交互作答。**两条路轮流走**（D155）：奇数次自由作答，偶数次挑一个选项。

    以前一律取第一个选项，于是「用户自己写一条」那条路在采集里**一次都没跑过**——
    而它和三选一对后续推演的分量完全不同（模型没列过的第四条路）。轮流走保证
    一次采集里两条都出现，报告里如实记下问了什么、答了什么、是不是自由作答。
    """
    await asyncio.sleep(0)
    if not options:
        return ""
    if next(_answered) % 2 == 0:
        return "都不选。按你没想到的那条走：先别急着推进，把这一处的疑点摊开说清楚。"
    return options[1] if len(options) > 1 else options[0]


def repo_of(settings: Settings, session_id: str) -> SessionRepository:
    return Store(settings.data_dir).session(session_id)
