"""把一条路径重放成 director 的产品历史（S60/S61）。

轮内执行链会持久化到可恢复草稿，以支持打断；一旦 turn 完成，跨轮只保留并重放
`用户输入 → 用户插话 → 正文`。thinking、工具调用与工具结果都是 turn 内协议流量，
不会进入节点，也不会在后续 turn 里换一种形态继续存活。

## 压缩的触发方式：越界一次，全部替换

压缩**不是段落一收就做**，而是等整份对话越过阈值再做，**并且一次把所有
能被摘要顶替的段落全部替换掉**。

理由是缓存：持续对话的价值在于前缀稳定。**每收一段就替换一段，等于每隔几轮
就动一次前缀，缓存反复失效**——那样还不如不持续。攒到越界再一次性替换，
则失效只发生一次，两次压缩之间的所有轮次都命中同一个前缀。

因此这里有两条线，**不要混**：

- **分段**：`Node.closes_segment`，生成时当场判（S58）。它只是打标记。
- **压缩**：本模块，按体量触发。它决定标记什么时候真正兑现成摘要。
"""

from __future__ import annotations

from ..config import LIMITS
from ..llm import Message, Role
from ..models import InputSource, Node
from ..prompting import rendering, special, special_messages

CONTEXT_BUDGET_CHARS = LIMITS.context.budget_chars
"""越过这个体量才压缩。

取值上的取舍：**调小则压得勤、缓存失效频繁；调大则单次上下文更贵**。
它不是模型的硬上限，是"多久动一次前缀"的旋钮——所以宁可偏大。
"""


def append_message(said: str) -> Message:
    """把用户追加的那一句**说明之后再追加**进对话。

    引导和打断插进来的那条消息，以前是裸文本前面顶一句写死在 Python 里的
    `（用户在这里插了一句）`。裸着塞进去，模型读到的是一条突然冒出来的用户
    发言，看不出它和这一轮已经进行到一半的推演是什么关系。措辞在
    `director.yaml` 的 `specials.append` 里——它是提示词，跟别的提示词放在
    一起（R31：**一份文件 = 一个 agent 收到的全部文字**）。
    """
    messages = special_messages("director", "append", values={"said": said})
    if len(messages) != 1:
        raise ValueError(f"director.yaml 的 specials.append 只能有一条，现在有 {len(messages)} 条")
    return messages[0]


def interjection_mark() -> str:
    """追加输入那一条的**结构标记**。

    扁平化时要靠它把用户的插话和编排层的催促（"请直接输出本轮细纲"）
    分开：前者是不可再生的用户输入，跨轮必须留；后者是脚手架，跨轮带着只会
    像一条仍然生效的指令。

    **装配器不再自动包标签**（R31），所以记号由用户自己写在 `body` 里，而
    `specials.append.mark` 声明它是哪一串。**两处必须对上**——对不上的后果是
    插话在重放时被当成催促悄悄丢掉（同 P29），所以这里当场校验。
    """
    spec = special("director", "append")
    mark = str(spec.get("mark") or "")
    if not mark:
        raise ValueError("director.yaml 的 specials.append 必须有 `mark`——重放时靠它认出用户的插话")
    bodies = "".join(str(s.get("body") or "") for s in spec.get("sections") or [])
    if mark not in bodies:
        raise ValueError(
            f"director.yaml 的 specials.append 正文里没有 `{mark}`——重放时会认不出插话"
        )
    return mark


def interjections_of(node: Node) -> list[str]:
    """这一轮用户插了哪几句（S50）。

    节点只保存跨轮产品记录；工具对话与 reasoning 的生命周期止于本 turn。
    """
    return list(node.interjections)


def interjection_texts(messages: list[Message]) -> list[str]:
    """从本轮临时对话中提取已渲染的插话，供节点产品记录落盘。"""
    mark = interjection_mark()
    return [
        _interjection_body(m.content, mark)
        for m in messages
        if m.role is Role.USER and m.content and mark in m.content
    ]


def _interjection_body(content: str, mark: str) -> str:
    """默认标记是成对标签；自定义成别的形状时至少不丢整条消息。"""
    after = content.split(mark, 1)[1]
    closing = mark.replace("<", "</", 1)
    if closing in after:
        return after.split(closing, 1)[0].strip()
    return content.strip()


def _chars(messages: list[Message]) -> int:
    """**推理段要算进去**（S62）。它是随消息一起发出去的，长度常常和正文相当——
    不算的话预算会系统性低估，压缩迟迟不触发，而低估的幅度随对话变长而变大。
    """
    return sum(len(m.content or "") + len(m.reasoning_content or "") for m in messages)


def _summary_message(node: Node, *, readable: bool, agent: str = "director") -> Message:
    """一段被压缩后留下的替身：**折起来的那一段**（S157）。

    写清它**顶替了几轮**；展开能力开启时再给出怎么取回原文——否则模型会把
    这段读成"那几轮只发生了这么点事"，比不压缩更糟。

    **地址是段落 id，提要是那段摘要。** 段落是三个域里唯一没有名字的东西，
    所以它是唯一用 id 当 key 的——而 id 旁边永远跟着摘要。

    **两句话都在 `director.yaml` 的 `rendering` 里**（R31）：它们是模型读到的字，
    而且其中一句还是明确的操作指令（「用 history_read 展开 `xxx`」）——
    改了工具名却忘了改它，模型会去调一个不存在的工具。

    展开那条路关掉时换另一句：**不要广告一个不存在的工具，也不展示降级说明**。
    原文到此永久隐去，但摘要仍然留着——模型必须知道那几轮存在过，只是精度低了（P13）。
    """
    return Message(
        role=Role.USER,
        content=rendering(
            agent,
            "history_folded" if readable else "history_folded_gone",
            span=max(1, node.segment_span),
            summary=node.prose_summary or "",
            key=node.id,
        ),
    )


def flatten_turn(node: Node) -> list[Message]:
    """把一轮重放成「顶层输入 → 插话 → 正文」（S60/S61）。

    三样东西必须留住，它们各自都是不可再生的；工具过程不跨轮：

    - **用户输入**：换成 `node.user_input` 的原文，而不是当时那条完整的轮次提示——
      提示里裹着**那一轮的 state 快照**，几百字，而且它已经被这一轮的新快照
      完全取代。旧快照留着不只是浪费，还会以"写在前面"的权威姿态和新值打架。
    - **插话**：用户在操作之间插的话改变了这一轮的走向，位置也有意义，所以原位保留。
    - **正文**：直接取 `node.prose`。它是这一轮最终落成的叙事事实。
    """
    out: list[Message] = []
    text = node.user_input.text if node.user_input else ""
    if text:
        out.append(
            Message(
                role=Role.USER,
                content=rendering("director", "flat_user_input", text=text),
            )
        )
    out.extend(Message(role=Role.USER, content=text) for text in node.interjections if text)
    if node.prose:
        out.append(Message(role=Role.ASSISTANT, content=node.prose))
    return out


def goal_turn_record(node: Node, index: int) -> Message:
    """goal_manager 的逐轮产品记录：正文，加上用户真正介入的方向。

    模拟输入是 goal_manager 自己刚写的，不必再原样回灌；普通剧情事实只认正文。
    用户手写输入与 turn 内插话则代表用户接管方向，尤其在 goal 暂停期间不能只靠
    正文猜回去，所以与正文一起保留（R30/S102）。
    """
    parts: list[str] = []
    if node.user_input and node.user_input.source is InputSource.USER and node.user_input.text:
        parts.append(rendering("goal_manager", "turn_user", text=node.user_input.text))
    for word in node.interjections:
        if word:
            parts.append(rendering("goal_manager", "turn_interjection", text=word))
    parts.append(node.prose or "（这一轮没有写出正文。）")
    body = rendering("goal_manager", "turn", index=index, body="\n".join(parts))
    return Message(role=Role.USER, content=body)


def _goal_direction_record(node: Node, index: int) -> Message | None:
    """正文折成摘要后仍保留用户亲自给出的输入与插话。"""
    parts: list[str] = []
    if node.user_input and node.user_input.source is InputSource.USER and node.user_input.text:
        parts.append(rendering("goal_manager", "turn_user", text=node.user_input.text))
    parts.extend(
        rendering("goal_manager", "turn_interjection", text=word)
        for word in node.interjections
        if word
    )
    if not parts:
        return None
    body = rendering("goal_manager", "turn", index=index, body="\n".join(parts))
    return Message(role=Role.USER, content=body)


def _compact_goal_history(
    path: list[Node],
    full: list[Message],
    *,
    readable: bool,
    start_index: int,
    keep_directions: bool,
) -> list[Message]:
    """按 director 的阈值和段边界折叠 goal_manager 的正文历史。"""
    if not path:
        return []
    if _chars(full) <= CONTEXT_BUDGET_CHARS:
        return full

    last_summarised = max(
        (index for index, node in enumerate(path) if node.prose_summary), default=-1
    )
    if last_summarised < 0:
        return full

    compacted: list[Message] = []
    cursor = 0
    for end_index, end in enumerate(path[: last_summarised + 1]):
        if not end.prose_summary:
            continue
        segment_start = max(cursor, end_index - max(1, end.segment_span) + 1)
        # 数据若有不连续的旧分段标记，宁可保留中间正文，也不静默丢掉。
        compacted.extend(full[cursor:segment_start])
        if keep_directions:
            for index in range(segment_start, end_index + 1):
                direction = _goal_direction_record(path[index], start_index + index)
                if direction is not None:
                    compacted.append(direction)
        compacted.append(_summary_message(end, readable=readable, agent="goal_manager"))
        cursor = end_index + 1

    compacted.extend(full[cursor:])
    return compacted


def goal_background_messages(path: list[Node], *, readable: bool = True) -> list[Message]:
    """goal 建立前的背景只含正文，不把旧用户指令重新变成当前方向。"""
    full = [
        Message(role=Role.USER, content=node.prose or "（这一轮没有写出正文。）") for node in path
    ]
    return _compact_goal_history(
        path,
        full,
        readable=readable,
        start_index=1,
        keep_directions=False,
    )


def goal_replay_messages(
    path: list[Node], *, readable: bool = True, start_index: int = 1
) -> list[Message]:
    """给 goal_manager 重放目标期间历史，使用 director 的同一压缩时机与段地址。

    越界前逐轮给正文；越界后把已经归段的正文换成同一份段落摘要，并留下
    ``history_read`` 可展开的 key。用户亲自给出的输入/插话不随正文摘要消失。
    """
    full = [goal_turn_record(node, start_index + i) for i, node in enumerate(path)]
    return _compact_goal_history(
        path,
        full,
        readable=readable,
        start_index=start_index,
        keep_directions=True,
    )


def replay_messages(path: list[Node], *, readable: bool = True) -> list[Message]:
    """沿路径重建 director 的对话。

    历史轮次一律**扁平化**（S61）；未越界时全带，越界后**所有已归段的段落
    一次性换成摘要**，最后一段之后的轮次仍以扁平形态在场。
    """
    if not path:
        return []

    full: list[Message] = []
    for node in path:
        full.extend(flatten_turn(node))

    if _chars(full) <= CONTEXT_BUDGET_CHARS:
        return full

    # 越界：从后往前找最后一个已归段的位置，它之前（含它）的全部替换。
    last_summarised = -1
    for index, node in enumerate(path):
        if node.prose_summary:
            last_summarised = index

    if last_summarised < 0:
        return full  # 还没有任何段落归好，只能先这么带着

    compacted: list[Message] = []
    for index, node in enumerate(path[: last_summarised + 1]):
        if node.prose_summary:
            compacted.append(_summary_message(node, readable=readable))
        # 被某一段覆盖的中间轮次不单独出现——它们已经在那条摘要里了。
        del index

    for node in path[last_summarised + 1 :]:
        compacted.extend(flatten_turn(node))
    return compacted
