"""关闭按需展开时的三种行为（D115）。

三条各自关，但**共用一条原则**：截断 + 按需展开是无损降维，
一旦没了展开的路，截断就变成有损筛选——模型看得见名字却永远拿不到内容，
而且不知道自己丢了什么。那正是 D62 判 lorebook 关键词触发不合格的理由。
"""

from agenttavern.agents.prompts import (
    STATE_INLINE_LIMIT,
    render_state,
    render_worldinfo,
)
from agenttavern.agents.replay import CONTEXT_BUDGET_CHARS, replay_messages
from agenttavern.llm import Message, Role
from agenttavern.models import (
    Capabilities,
    Node,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
)
from agenttavern.tools import ToolRegistry

LONG = "很长的一段关系描述。" * 40


def test_state_is_injected_in_full_when_query_is_off() -> None:
    """关闭 `state_query` → **每轮全量注入**，长值不再截断。"""
    state = WorldState(values={"林与陈.关系": LONG})
    assert len(LONG) > STATE_INLINE_LIMIT

    assert "state_query 展开" in render_state(state, expandable=True)

    off = render_state(state, expandable=False)
    assert LONG in off
    assert "state_query" not in off, "不能广告一个没注册的工具"


def test_state_query_tool_disappears_but_write_stays() -> None:
    """关的是**按需展开**，不是 state 本身——写入照常，E0 契约也照常。"""
    names = ToolRegistry.for_session(Capabilities(state=True, state_query=False)).names
    assert "state_write" in names
    assert "state_query" not in names


def test_on_demand_worldinfo_is_inlined_when_read_is_off() -> None:
    """关闭 `worldinfo_read` → 按需条目也**全文进静态前缀**。

    设定跨轮不变，所以它天然属于缓存前缀，全量注入的代价只付一次（D114）。
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

    assert "worldinfo_read" in ToolRegistry.for_session(Capabilities(), entries).names
    assert (
        "worldinfo_read"
        not in ToolRegistry.for_session(Capabilities(worldinfo_read=False), entries).names
    )


def _compacted(readable: bool) -> str:
    # 体量要顶在**扁平化之后仍然留下的东西**上（D121）：细纲留、轮次提示不留。
    # 塞在提示里会被扁平化吃掉，压根越不了界。
    filler = CONTEXT_BUDGET_CHARS
    node = Node(outline="第一轮" + "长" * filler, outline_summary="她进店，他锁了门。")
    node.director_messages = [Message(role=Role.USER, content="本轮提示")]
    tail = Node(parent_id=node.id, outline="第二轮" + "长" * filler)
    tail.director_messages = [Message(role=Role.USER, content="本轮提示")]

    return "\n".join(m.content or "" for m in replay_messages([node, tail], readable=readable))


def test_compacted_history_is_gone_for_good_when_search_is_off() -> None:
    """关闭 `history_search` → 被压缩的原文**永久隐去**。

    但**摘要仍然留着**：模型必须知道那几轮存在过，只是精度低了（D62）。
    整段抹掉才是不合格的——那会让它不知道自己丢了什么。
    """
    off = _compacted(readable=False)

    assert "她进店，他锁了门。" in off, "摘要必须留着"
    assert "取不回来" in off, "要说清原文没了，否则模型会以为还能查"
    assert "history_read" not in off, "不能广告一个没注册的工具"

    on = _compacted(readable=True)
    assert "history_read" in on
