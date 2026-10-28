"""director 的对话跨轮持续、按段折叠，以及跨轮扁平化（S60/S61）。"""

from agenttavern.agents.replay import flatten_turn, replay_messages
from agenttavern.llm import Message, Role, ToolCall
from agenttavern.models import Node, RunDraft, UserInput


def _run(
    prose: str,
    *,
    stale: int = 0,
    filler: int = 0,
    closes: bool = False,
    summary: str | None = None,
):
    """一轮的产品记录；``stale`` 只保留为旧测试调用参数，节点不再存旧快照。"""
    del stale
    node = Node(
        prose=prose + "长" * filler,
        closes_segment=closes,
        prose_summary=summary,
        user_input=UserInput(text=f"用户：{prose}"),
    )
    return node


# -- 跨轮扁平化（S61） ---------------------------------------------------------


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


def test_flattening_keeps_only_the_input_and_prose() -> None:
    """跨轮只有产品记录；旧 state、thinking 与工具过程都不在节点里。"""
    text = "\n".join(m.content or "" for m in flatten_turn(_run("第1轮", stale=500)))

    assert "用户：第1轮" in text
    assert "第1轮" in text
    assert "state_query" not in text


def test_an_interjection_survives_flattening_in_place() -> None:
    """用户插的话改变了这一轮的走向，位置也有意义——原位保留。

    编排层的催促则不留：跨轮带着它，会像一条仍然生效的指令。
    """
    node = _run("第1轮")
    node.interjections = ["别去码头"]

    flat = flatten_turn(node)
    text = "\n".join(m.content or "" for m in flat)

    assert "别去码头" in text
    assert "请通过工具完成本轮" not in text
    positions = [i for i, m in enumerate(flat) if "别去码头" in (m.content or "")]
    assert positions and positions[0] > 0


def test_unclosed_runs_are_kept_in_full() -> None:
    """没有语义段落边界时，每一轮都以扁平原文在场。"""
    path = [_run(f"第{i}轮") for i in range(5)]
    messages = replay_messages(path)
    text = "\n".join(m.content or "" for m in messages)

    assert all(f"第{i}轮" in text for i in range(5))


def test_completed_segments_fold_without_a_character_threshold() -> None:
    """已完成并取得摘要的语义段立即折叠，不等待字符阈值。"""
    filler = 0
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

    # 两个已完成的语义段都被摘要替换。
    assert "第一段的摘要" in text
    assert "第二段的摘要" in text
    # 被覆盖的轮次不再单独出现
    assert "用户：第1轮" not in text
    assert "用户：第2轮" not in text
    assert "用户：第3轮" not in text
    # 最后一段之后的轮次原样保留
    assert "用户：第4轮" in text
    assert "用户：第5轮" in text


def test_a_summary_says_how_many_runs_it_replaced_and_how_to_get_them_back() -> None:
    """替身必须写清它顶替了几轮、怎么取回原文。

    否则模型会把这一段读成"那几轮只发生了这么点事"——**比不压缩更糟**，
    因为它会以为自己已经看全了。
    """
    filler = 0
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
    """没有可用摘要的未完成段保持原样，不按字符数丢弃或截断。"""
    filler = 20_000
    path = [_run("第1轮", filler=filler), _run("第2轮", filler=filler)]

    messages = replay_messages(path)
    text = "\n".join(m.content or "" for m in messages)
    assert "用户：第1轮" in text
    assert "用户：第2轮" in text


def test_empty_path_replays_to_nothing() -> None:
    assert replay_messages([]) == []


# -- 推理段的生命周期（S62） ---------------------------------------------------


def test_reasoning_and_tools_only_persist_in_a_draft() -> None:
    """执行流量为恢复同一 turn 可以落草稿，但不进入完成节点。"""
    messages = [
        Message(role=Role.USER, content="本轮输入"),
        Message(
            role=Role.ASSISTANT,
            tool_calls=[ToolCall(id="c1", name="state_query")],
            reasoning_content="想了很久" * 500,
        ),
        Message(role=Role.TOOL, content="结果", tool_call_id="c1"),
    ]
    draft = RunDraft(messages=messages)
    node = Node(prose="结果")

    assert draft.messages[1].reasoning_content is not None
    dumped = node.model_dump()
    assert "director_messages" not in dumped
    assert "messages" not in dumped
