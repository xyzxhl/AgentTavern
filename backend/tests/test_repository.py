"""仓储：节点树、分支下的状态重放、trace 追加。"""

from pathlib import Path

import pytest

from agenttavern.models import (
    Actor,
    Node,
    Session,
    StateDelta,
    TraceEvent,
    TraceEventType,
)
from agenttavern.storage import Store


def node(parent: str | None, location: str, **kw: object) -> Node:
    return Node(
        parent_id=parent,
        state_delta=StateDelta(set={"地点": location}),
        **kw,  # type: ignore[arg-type]
    )


def place_at(repo, leaf_id: str | None) -> str | None:
    return repo.state_at(leaf_id).values.get("地点")


@pytest.fixture
def repo(tmp_path: Path):
    store = Store(tmp_path)
    return store.create_session(Session())


def test_session_roundtrip(repo) -> None:
    session = repo.load_session()
    session.meta.title = "雨夜"
    repo.save_session(session)
    assert repo.load_session().meta.title == "雨夜"


def test_path_and_replay(repo) -> None:
    a = node(None, "门口")
    b = node(a.id, "客厅")
    c = node(b.id, "厨房")
    for n in (a, b, c):
        repo.save_node(n)

    assert [n.id for n in repo.path_to(c.id)] == [a.id, b.id, c.id]
    assert place_at(repo, c.id) == "厨房"
    assert place_at(repo, b.id) == "客厅"


def test_branches_have_independent_state(repo) -> None:
    """D48 的核心：切换分支必须同时切换状态。

    若状态是全局单例，切到分支 B 后仍会看到分支 A 的位置——文本对、状态悄悄错。
    """
    root = node(None, "门口")
    left = node(root.id, "客厅")
    right = node(root.id, "阳台")
    for n in (root, left, right):
        repo.save_node(n)

    assert place_at(repo, left.id) == "客厅"
    assert place_at(repo, right.id) == "阳台"
    assert place_at(repo, root.id) == "门口"


def test_siblings_are_ordered_deterministically(repo) -> None:
    """重生成产生兄弟节点，UI 按顺序做 < 2/3 > 切换（D46）。

    顺序必须**确定**——``all_nodes`` 来自 glob，文件系统顺序不能渗进来，
    否则切换器的顺序会在两次刷新之间跳动。这里三个节点在同一微秒内创建，
    正好压到时间戳并列的情况。
    """
    root = node(None, "门口")
    repo.save_node(root)
    kids = [node(root.id, f"位置{i}") for i in range(3)]
    for n in kids:
        repo.save_node(n)

    first = [n.id for n in repo.children_of(root.id)]
    assert sorted(first) == sorted(n.id for n in kids)
    assert first == [n.id for n in repo.children_of(root.id)]

    # 时间戳有差异时按时间排序。
    later = node(root.id, "最后")
    later.created_at = later.created_at.replace(year=later.created_at.year + 1)
    repo.save_node(later)
    assert repo.children_of(root.id)[-1].id == later.id


def test_empty_state_before_any_node(repo) -> None:
    assert place_at(repo, None) is None


def test_cycle_is_detected(repo) -> None:
    """节点文件可被用户直接编辑（D65），不能假设数据永远自洽。"""
    a = Node(id="run_a", parent_id="run_b")
    b = Node(id="run_b", parent_id="run_a")
    repo.save_node(a)
    repo.save_node(b)

    with pytest.raises(ValueError, match="环"):
        repo.path_to("run_a")


def test_trace_is_append_only(repo) -> None:
    """trace 全局 append-only，包含被放弃的尝试（D50）。"""
    for seq in range(3):
        repo.append_trace(
            TraceEvent(
                run_id="run_x",
                seq=seq,
                type=TraceEventType.TOOL_CALL,
                actor=Actor.DIRECTOR,
                label=f"查询了第 {seq} 项",
            )
        )

    events = repo.read_trace("run_x")
    assert [e.seq for e in events] == [0, 1, 2]
    assert events[1].label == "查询了第 1 项"


def test_files_are_human_readable(repo, tmp_path: Path) -> None:
    """存 JSON 的首要理由是用户可直接编辑——中文必须不被转义。"""
    session = repo.load_session()
    session.meta.title = "雨夜"
    repo.save_session(session)

    raw = repo.session_path.read_text(encoding="utf-8")
    assert "雨夜" in raw


# -- config.yaml：调参统一到一处 ----------------------------------------------


def test_config_yaml_covers_every_knob() -> None:
    """**代码里不再写这些字面量。**

    以前它们散在八个模块的模块级常量里：想知道"director 到底几轮上限"得先猜它在
    哪个文件；想调一个数得改代码。这条钉的是"每个旋钮都真的从 config.yaml 来"——
    漏掉一个的症状是改了配置没反应，而那是最难查的一类。
    """
    from agenttavern.agents import director, replay, state_manager, summarizer
    from agenttavern.config import LIMITS, load_limits
    from agenttavern.models import Capabilities, Goal
    from agenttavern.orchestration import checks
    from agenttavern.prompting import prompts
    from agenttavern.tools import builtin

    wired = {
        "director.max_rounds": (LIMITS.director.max_rounds, director.MAX_ROUNDS),
        "director.soft_rounds": (LIMITS.director.soft_rounds, director.SOFT_ROUNDS),
        "state_manager.max_rounds": (LIMITS.state_manager.max_rounds, state_manager.MAX_ROUNDS),
        "context.budget_chars": (LIMITS.context.budget_chars, replay.CONTEXT_BUDGET_CHARS),
        "context.state_inline_limit": (
            LIMITS.context.state_inline_limit,
            prompts.STATE_INLINE_LIMIT,
        ),
        "context.tool_read_chars": (LIMITS.context.tool_read_chars, builtin.MAX_READ_CHARS),
        "segment.summary_max_chars": (
            LIMITS.segment.summary_max_chars,
            summarizer.SUMMARY_MAX_CHARS,
        ),
        "segment.max_runs": (LIMITS.segment.max_runs, summarizer.MAX_SEGMENT_RUNS),
        "segment.min_runs": (LIMITS.segment.min_runs, summarizer.MIN_SEGMENT_RUNS),
        "sampling.expansion_min_ratio": (
            LIMITS.sampling.expansion_min_ratio,
            checks.EXPANSION_MIN_RATIO,
        ),
        "sampling.state_value_max_chars": (
            LIMITS.sampling.state_value_max_chars,
            checks.STATE_VALUE_MAX_CHARS,
        ),
        "windows.upstream_runs": (LIMITS.windows.upstream_runs, Capabilities().upstream_runs),
        "windows.state_recent_runs": (
            LIMITS.windows.state_recent_runs,
            Capabilities().state_recent_runs,
        ),
        "goal_manager.max_runs": (LIMITS.goal_manager.max_runs, Goal(description="x").max_runs),
    }
    assert {k for k, (a, b) in wired.items() if a != b} == set()

    # session 级那几处只是**取默认值**，仍然能单独覆盖。
    assert Capabilities(upstream_runs=9).upstream_runs == 9

    # **文件里的值就是真值**，不是被模型里那份兜底默认悄悄顶掉的。
    assert load_limits().model_dump() == LIMITS.model_dump()


def test_a_missing_config_file_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    """文件缺了要能起来。**在 import 阶段炸掉是最糟的失败方式**——
    什么都跑不了，而错误看起来跟配置无关。
    """
    from agenttavern.config import Limits, load_limits

    assert load_limits(tmp_path / "没有这个文件.yaml") == Limits()
