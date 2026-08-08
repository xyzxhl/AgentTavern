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
