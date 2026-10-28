"""关闭按需展开时的三种行为（S55）。

三条各自关，但**共用一条原则**：截断 + 按需展开是无损降维，
一旦没了展开的路，截断就变成有损筛选——模型看得见名字却永远拿不到内容，
而且不知道自己丢了什么。那正是 P13 判 lorebook 关键词触发不合格的理由。
"""

from conftest import using

from agenttavern.agents.replay import replay_messages
from agenttavern.models import (
    Node,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
)
from agenttavern.prompting import render_state, render_worldinfo
from agenttavern.tools import ToolRegistry, build_state_tools

LONG = "很长的一段关系描述。" * 40


def test_state_is_injected_in_full_when_query_is_off() -> None:
    """关闭 `state_query` → **每轮全量注入**，叶端对象不再折叠。"""
    state = WorldState(values={"案件": {"证物": {"铜盒": {"夹层": {"微刻": LONG}}}}})

    on = render_state(state, agent="director", expandable=True)
    assert LONG not in on and "state_query 展开" in on

    off = render_state(state, agent="director", expandable=False)
    assert LONG in off
    assert "state_query" not in off, "不能广告一个没注册的工具"


def test_state_query_disappears_from_both_sides_but_write_stays() -> None:
    """关的是**按需展开**，不是 state 本身——写入照常。

    `state_query` 两侧都有（director 读状态推演，state_manager 写之前先看），
    所以这个开关一关就两边一起消失（S7）。
    """
    # **两侧各有各的开关**（R31）：同一个工具在两个 agent 手上是两条描述、
    # 两个开关，所以「按需展开」这件事要在两边各关一次。
    off = {
        "director": {"state_query": False},
        "state_manager": {"state_query": False},
    }
    with using(tools=off):
        assert "state_query" not in ToolRegistry.for_session().names
        assert {t.name for t in build_state_tools()} == {"state_write"}


def test_on_demand_worldinfo_is_inlined_when_read_is_off() -> None:
    """关闭 `worldinfo_read` → 按需条目也**全文进静态前缀**。

    设定跨轮不变，所以它天然属于缓存前缀，全量注入的代价只付一次（S60）。
    """
    entries = [
        WorldbookEntry(name="旧货店", mode=WorldbookMode.ON_DEMAND, content=LONG),
    ]

    on = render_worldinfo(entries, expandable=True)
    assert LONG not in on and "worldinfo_read" in on

    off = render_worldinfo(entries, expandable=False)
    assert LONG in off
    assert "worldinfo_read" not in off


def test_worldinfo_tools_disappear_when_read_is_off() -> None:
    entries = [WorldbookEntry(name="旧货店", mode=WorldbookMode.ON_DEMAND, content="老城区。")]

    assert "worldinfo_read" in ToolRegistry.for_session(entries).names
    with using(tools={"director": {"worldinfo_read": False}}):
        assert "worldinfo_read" not in ToolRegistry.for_session(entries).names


def _compacted(readable: bool) -> str:
    node = Node(prose="第一轮", prose_summary="她进店，他锁了门。")
    tail = Node(parent_id=node.id, prose="第二轮")

    return "\n".join(m.content or "" for m in replay_messages([node, tail], readable=readable))


def test_compacted_history_is_gone_for_good_when_read_is_off() -> None:
    """关闭 `history_read` → 被压缩的原文**永久隐去**。

    但**摘要仍然留着**：模型必须知道那几轮存在过，只是精度低了（P13）。
    整段抹掉才是不合格的——那会让它不知道自己丢了什么。
    """
    off = _compacted(readable=False)

    assert "她进店，他锁了门。" in off, "摘要必须留着"
    assert "第一轮" not in off, "原文必须真的隐去"
    assert "history_read" not in off, "不能广告一个没注册的工具"

    on = _compacted(readable=True)
    assert "history_read" in on
