"""director 的对话跨轮持续（D114）、越界时的批量压缩、以及跨轮扁平化（D121）。"""

from agenttavern.agents.replay import (
    CONTEXT_BUDGET_CHARS,
    INTERJECTION_MARK,
    _chars,
    flatten_turn,
    replay_messages,
    without_reasoning,
)
from agenttavern.llm import Message, Role, ToolCall
from agenttavern.models import Node, UserInput


def _run(
    outline: str,
    *,
    stale: int = 0,
    filler: int = 0,
    closes: bool = False,
    summary: str | None = None,
):
    """一轮的**原样记录**：轮次提示 → 带 tool_calls 的 assistant → tool 结果 → 提交细纲。

    两种"占地方"要分开，因为扁平化只吃掉其中一种：

    - ``stale`` 塞在轮次提示里，模拟那份 state 快照——**扁平化会丢掉它**。
    - ``filler`` 塞在细纲里，那是要留住的内容——用它来把体量顶过预算。
    """
    node = Node(
        outline=outline + "长" * filler,
        closes_segment=closes,
        outline_summary=summary,
        user_input=UserInput(text=f"用户：{outline}"),
    )
    node.director_messages = [
        Message(role=Role.USER, content="## 本轮状态\n" + "填" * stale + f"\n用户：{outline}"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[
                ToolCall(id=f"q{outline}", name="state_query", arguments_raw='{"keys":[]}')
            ],
        ),
        Message(role=Role.TOOL, content=f"工具结果：{outline}", tool_call_id=f"q{outline}"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id=f"s{outline}", name="submit_outline")],
        ),
        Message(role=Role.TOOL, content="细纲已提交。", tool_call_id=f"s{outline}"),
    ]
    return node


# -- 跨轮扁平化（D121） ---------------------------------------------------------


def test_a_past_turn_carries_no_tool_calls_at_all() -> None:
    """扁平化之后历史轮次里**一个 `tool_calls` 都没有**。

    这不只是省地方：DeepSeek 要求 `reasoning_content` 回传的前提就是那条
    assistant 消息带着 `tool_calls`。一个都没有，那条要求根本不适用——
    换到严格执行的模型也不会 400。
    """
    flat = flatten_turn(_run("第1轮"))

    assert all(not m.tool_calls for m in flat)
    assert all(m.role is not Role.TOOL for m in flat)
    assert all(m.reasoning_content is None for m in flat)


def test_flattening_keeps_the_facts_and_drops_the_stale_state() -> None:
    """留住三样不可再生的：用户输入、查到了什么、提交的细纲。

    轮次提示里那份 state 快照**不留**——它已经被这一轮的新快照完全取代，
    留着不只是浪费，还会以"写在前面"的权威姿态和新值打架。
    """
    text = "\n".join(m.content or "" for m in flatten_turn(_run("第1轮", stale=500)))

    assert "用户：第1轮" in text
    assert "state_query" in text
    assert "工具结果：第1轮" in text
    assert "第1轮" in text
    assert "填" not in text, "旧的 state 快照不该跨轮"
    assert "细纲已提交。" not in text, "工具的回执是脚手架，不是事实"


def test_an_interjection_survives_flattening_in_place() -> None:
    """用户插的话改变了这一轮的走向，位置也有意义——原位保留。

    编排层的催促则不留：跨轮带着它，会像一条仍然生效的指令。
    """
    node = _run("第1轮")
    node.director_messages.insert(
        3, Message(role=Role.USER, content=f"{INTERJECTION_MARK}\n别去码头")
    )
    node.director_messages.insert(4, Message(role=Role.USER, content="请通过工具完成本轮。"))

    flat = flatten_turn(node)
    text = "\n".join(m.content or "" for m in flat)

    assert "别去码头" in text
    assert "请通过工具完成本轮" not in text
    # 插在第一次工具调用之后，所以排在那条动作记录后面。
    positions = [i for i, m in enumerate(flat) if "别去码头" in (m.content or "")]
    assert positions and positions[0] > 0


def test_everything_is_kept_until_the_budget_is_crossed() -> None:
    """未越界时每一轮都在场——只是以扁平形态。"""
    path = [_run(f"第{i}轮") for i in range(5)]
    messages = replay_messages(path)
    text = "\n".join(m.content or "" for m in messages)

    assert all(f"工具结果：第{i}轮" in text for i in range(5))
    assert all(f"第{i}轮" in text for i in range(5))


def test_crossing_the_budget_replaces_every_summarised_segment_at_once() -> None:
    """**越界一次，全部替换**——不是每收一段就换一段。

    持续对话的价值在于前缀稳定。每收一段就替换一段，等于每隔几轮就动一次前缀，
    **缓存反复失效**，那还不如不持续。攒到越界再一次性换掉，失效只发生一次，
    两次压缩之间的所有轮次命中同一个前缀。
    """
    filler = CONTEXT_BUDGET_CHARS // 4
    path = [
        _run("第1轮", filler=filler, closes=True, summary="第一段的摘要"),
        _run("第2轮", filler=filler),
        _run("第3轮", filler=filler, closes=True, summary="第二段的摘要"),
        _run("第4轮", filler=filler),
        _run("第5轮", filler=filler),
    ]
    path[2].segment_span = 2

    messages = replay_messages(path)
    text = "\n".join(m.content or "" for m in messages)

    # 两段都被换掉了，而不是只换最老的那一段
    assert "第一段的摘要" in text
    assert "第二段的摘要" in text
    # 被覆盖的轮次不再单独出现
    assert "工具结果：第1轮" not in text
    assert "工具结果：第2轮" not in text
    assert "工具结果：第3轮" not in text
    # 最后一段之后的轮次原样保留
    assert "工具结果：第4轮" in text
    assert "工具结果：第5轮" in text


def test_a_summary_says_how_many_runs_it_replaced_and_how_to_get_them_back() -> None:
    """替身必须写清它顶替了几轮、怎么取回原文。

    否则模型会把这一段读成"那几轮只发生了这么点事"——**比不压缩更糟**，
    因为它会以为自己已经看全了。
    """
    filler = CONTEXT_BUDGET_CHARS // 2
    path = [
        _run("第1轮", filler=filler, closes=True, summary="她进店，他锁了门。"),
        _run("第2轮", filler=filler),
    ]
    path[0].segment_span = 1

    text = "\n".join(m.content or "" for m in replay_messages(path))

    assert "她进店，他锁了门。" in text
    assert "1 轮" in text
    assert "history_read" in text
    assert path[0].id in text, "要给出 ref，否则没法展开"


def test_nothing_is_dropped_when_no_segment_has_been_summarised_yet() -> None:
    """越界了但还没有任何段落归好——只能先原样带着。

    **绝不能因为超预算就随意截断**：那会让上下文里出现无主的工具结果，
    也会让模型不知道自己丢了什么（D62）。
    """
    filler = CONTEXT_BUDGET_CHARS
    path = [_run("第1轮", filler=filler), _run("第2轮", filler=filler)]

    messages = replay_messages(path)
    text = "\n".join(m.content or "" for m in messages)
    assert "工具结果：第1轮" in text
    assert "工具结果：第2轮" in text


def test_empty_path_replays_to_nothing() -> None:
    assert replay_messages([]) == []


# -- 推理段的生命周期（D120） ---------------------------------------------------


def test_reasoning_is_stripped_before_a_node_is_stored() -> None:
    """推理只活到产出它的那个 run 结束。

    实测一轮 director 的 `reasoning_content` 有一万五千字，而它产出的细纲只有
    两百字。原样存进 `director_messages`，持续对话跑两轮就撞预算——**压缩变成
    每轮都发生，前缀反复失效，D114 的全部意义就没了**。
    """
    kept = Message(role=Role.TOOL, content="结果", tool_call_id="c1")
    messages = [
        Message(role=Role.USER, content="本轮输入"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="c1", name="state_query")],
            reasoning_content="想了很久" * 500,
        ),
        kept,
    ]

    stripped = without_reasoning(messages)
    assert all(m.reasoning_content is None for m in stripped)
    # 其余部分一字不动——抹掉的只有推理段。
    assert [m.content for m in stripped] == [m.content for m in messages]
    assert stripped[1].tool_calls == messages[1].tool_calls
    assert messages[1].reasoning_content is not None, "不能就地改掉调用方的消息"


def test_reasoning_counts_toward_the_context_budget() -> None:
    """推理段是随消息一起发出去的，算预算时**必须算上**。

    不算的话预算会系统性低估，而低估的幅度随对话变长越来越大。
    """
    plain = [Message(role=Role.ASSISTANT, content="x" * 100)]
    with_reasoning = [Message(role=Role.ASSISTANT, content="x" * 100, reasoning_content="y" * 900)]
    assert _chars(plain) == 100
    assert _chars(with_reasoning) == 1000
