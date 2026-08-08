"""把一条路径重放成 director 的对话（D114）。

## 轮内保结构，跨轮扁平化（D121）

同一条信息在两种场合要的形态不同：
- **本轮内**：原样保留 `tool_calls`、`role=tool` 结果、`reasoning_content`。
  模型正在这条链上往下想；这也是 DeepSeek 的契约要求。
- **跨轮重放**：压成 `assistant` 文本——调了什么、返回了什么、细纲是什么。
  上一轮的往返对这一轮只是**事实来源**，不再是一条待续的链。

扁平化顺带把 D120 里那条已知风险**消掉了**，不只是压低：DeepSeek 要求
`reasoning_content` 回传的前提是**那条 assistant 消息带着 `tool_calls`**，
文档同时写明"模型未执行工具调用时，中间 assistant 的 `reasoning_content`
不需要参与上下文拼接"。扁平化之后历史轮次里**一个 `tool_calls` 都没有**，
这条要求根本不适用——换到严格执行的模型也不会 400。

**扁平化发生在重放时，不在落盘时。** 节点里存的仍是结构化的原样记录：
存储要忠实，呈现是可以改的决定（同 D48 存增量而不存快照的理由）。

## 压缩的触发方式：越界一次，全部替换

压缩**不是段落一收就做**，而是等整份对话越过阈值再做，**并且一次把所有
能被摘要顶替的段落全部替换掉**。

理由是缓存：持续对话的价值在于前缀稳定。**每收一段就替换一段，等于每隔几轮
就动一次前缀，缓存反复失效**——那样还不如不持续。攒到越界再一次性替换，
则失效只发生一次，两次压缩之间的所有轮次都命中同一个前缀。

因此这里有两条线，**不要混**：

- **分段**：`Node.closes_segment`，生成时当场判（D113）。它只是打标记。
- **压缩**：本模块，按体量触发。它决定标记什么时候真正兑现成摘要。
"""

from __future__ import annotations

from ..llm import Message, Role
from ..models import Node

CONTEXT_BUDGET_CHARS = 24_000
"""越过这个体量才压缩。

取值上的取舍：**调小则压得勤、缓存失效频繁；调大则单次上下文更贵**。
它不是模型的硬上限，是"多久动一次前缀"的旋钮——所以宁可偏大。
"""


INTERJECTION_MARK = "（用户在这里插了一句）"
"""引导 / 打断插进来的那条消息的抬头（D119）。

**它是结构标记，不是措辞。** 扁平化时要靠它把用户的插话和编排层的催促
（"请通过工具完成本轮……"）分开：前者是不可再生的用户输入，必须留；
后者是脚手架，跨轮带着只会像一条仍然生效的指令。
"""

SUBMIT_TOOL = "submit_outline"
"""扁平化时跳过它——它的产出就是 `node.outline`，直接取节点上的更准。"""


def _chars(messages: list[Message]) -> int:
    """**推理段要算进去**（D120）。它是随消息一起发出去的，长度常常和正文相当——
    不算的话预算会系统性低估，压缩迟迟不触发，而低估的幅度随对话变长而变大。
    """
    return sum(len(m.content or "") + len(m.reasoning_content or "") for m in messages)


def _summary_message(node: Node, *, readable: bool) -> Message:
    """一段被压缩后留下的替身。

    写清它**顶替了几轮**、以及怎么取回原文——否则模型会把这段读成
    "那几轮只发生了这么点事"，比不压缩更糟。
    """
    span = max(1, node.segment_span)
    if readable:
        tail = (
            f"\n需要那几轮的原文、原话或具体的名字日期，用 `history_read` 按 ref `{node.id}` 展开。"
        )
    else:
        # 检索关掉时**不要广告一个不存在的工具**。原文到此永久隐去——
        # 但摘要仍然留着：模型必须知道那几轮存在过，只是精度低了（D62）。
        tail = "\n那几轮的原文已经取不回来了，以这段摘要为准。"
    return Message(
        role=Role.USER,
        content=f"（前面 {span} 轮已压缩为摘要：{node.outline_summary}{tail}）",
    )


def flatten_turn(node: Node) -> list[Message]:
    """把一轮压成「用户输入 → 一条 assistant 记录」（D121）。

    产出的 assistant 消息里没有 `tool_calls`，因此 DeepSeek 那条
    `reasoning_content` 必须回传的要求不适用（见模块开头）。

    三样东西必须留住，它们各自都是不可再生的：

    - **用户输入**：换成 `node.user_input` 的原文，而不是当时那条完整的轮次提示——
      提示里裹着**那一轮的 state 快照**，几百字，而且它已经被这一轮的新快照
      完全取代。旧快照留着不只是浪费，还会以"写在前面"的权威姿态和新值打架。
    - **插话**：用户在操作之间插的话改变了这一轮的走向，位置也有意义，所以原位保留。
    - **细纲**：直接取 `node.outline`。它在原始消息里只是 `submit_outline` 的一个参数，
      藏在 JSON 里。
    """
    results = {
        m.tool_call_id: (m.content or "")
        for m in node.director_messages
        if m.role is Role.TOOL and m.tool_call_id
    }

    out: list[Message] = []
    actions: list[str] = []

    def flush() -> None:
        if actions:
            out.append(Message(role=Role.ASSISTANT, content="\n".join(actions)))
            actions.clear()

    seen_first_user = False
    for message in node.director_messages:
        if message.role is Role.USER:
            if not seen_first_user:
                seen_first_user = True
                text = node.user_input.text if node.user_input else ""
                if text:
                    flush()
                    out.append(Message(role=Role.USER, content=f"## 用户输入\n{text}"))
            elif message.content and INTERJECTION_MARK in message.content:
                flush()
                out.append(message)
            # 其余的 USER 消息是编排层的催促，跨轮不带。
        elif message.role is Role.ASSISTANT:
            for call in message.tool_calls:
                if call.name == SUBMIT_TOOL:
                    continue
                result = results.get(call.id, "")
                actions.append(f"· {call.name}({call.arguments_raw})\n  → {result}")

    if node.outline:
        actions.append(f"\n## 本轮提交的细纲\n{node.outline}")
    flush()
    return out


def replay_messages(path: list[Node], *, readable: bool = True) -> list[Message]:
    """沿路径重建 director 的对话。

    历史轮次一律**扁平化**（D121）；未越界时全带，越界后**所有已归段的段落
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
        if node.outline_summary:
            last_summarised = index

    if last_summarised < 0:
        return full  # 还没有任何段落归好，只能先这么带着

    compacted: list[Message] = []
    for index, node in enumerate(path[: last_summarised + 1]):
        if node.outline_summary:
            compacted.append(_summary_message(node, readable=readable))
        # 被某一段覆盖的中间轮次不单独出现——它们已经在那条摘要里了。
        del index

    for node in path[last_summarised + 1 :]:
        compacted.extend(flatten_turn(node))
    return compacted


def turn_messages(messages: list[Message], start: int) -> list[Message]:
    """这一轮新增的消息。工具消息必须成组保留，否则重放时会出现孤儿 tool result。"""
    return [m for m in messages[start:] if m.role is not Role.SYSTEM]


def without_reasoning(messages: list[Message]) -> list[Message]:
    """落盘前抹掉推理段。**推理只活到产出它的那个 run 结束**（D120）。

    实测一轮 director（3 个来回）的 `reasoning_content` 有 **15 000 字**，
    而它产出的细纲只有 207 字——推理比产出长了七十倍。原样存进
    `director_messages`，持续对话（D114）跑两轮就撞上 `CONTEXT_BUDGET_CHARS`，
    压缩变成每轮都发生，前缀反复失效，**持续对话的全部意义就没了**。

    分界划在 run 边界上，因为两侧的用途不同：

    - **run 内**：推理是那条工具链的一部分，模型接着自己上一步往下想，且这是
      DeepSeek 的契约要求。草稿（D118）续跑的是同一个 run，所以草稿里**要留**。
    - **run 间**：下一轮值钱的是查到了什么、写下了什么细纲，不是三轮之前
      模型怎么绕到那个结论的。

    **已知风险**：文档说带工具调用的 assistant 消息不回传 `reasoning_content`
    会返回 400。实测 `deepseek-v4-flash` 不回传能过（这也正是改动前一直没炸的
    原因）。若换到严格执行的模型，重放历史轮次时会立刻 400——症状明确、
    出现在第二轮，不是那种要查很久的错。
    """
    return [
        m if m.reasoning_content is None else m.model_copy(update={"reasoning_content": None})
        for m in messages
    ]
