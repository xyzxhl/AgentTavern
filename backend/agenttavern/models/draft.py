"""被打断的一次推演。**打断是挂起，不是作废**（D118）。

早先 director 阶段被打断就什么都不留（D60 原文）。那是错的：**打断和重生成不是
同一件事**。

| | 用户想要 | 已完成的部分 |
|---|---|---|
| **重生成** | 换一个结果 | 不要了 |
| **引导** | 在这个结果之后加一句 | 等它写完，再作为新的一轮 |
| **打断** | 现在就插一句 | **要**——它只是被截在了半路 |

打断唯一独有的东西是**时机**：它不等当前动作跑完，直接掐掉在飞的那次 API 调用
或工具调用。除了时机，它和引导要的是同一个东西——**在目前已完成的内容上继续**。
既然如此，已经查过的资料、已经暂存的增量、已经想到一半的推演就都该留着。

草稿因此存的是**已经完成的动作**：查过的资料、想到一半的推演，重来一遍只是白花
一次调用。它原先还存 `staged_delta` 和 `state_write_count`——那时 director 边推演
边写状态。**D143 之后 director 不碰状态**，这一阶段暂存区恒为空，两个字段一并删掉。

草稿按 `parent_id` 认领。**父节点不同就丢弃**——那说明用户已经切到别的分支上去了，
这份推演接不上（D48）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ..llm.types import Message, Role


class DirectorDraft(BaseModel):
    """一次未完成的 director 推演，等着下一条输入把它接下去。"""

    parent_id: str | None = None
    """这份推演挂在哪个节点下。**续跑时必须一致**，否则丢弃。

    ``None`` 本身是合法的父节点（根），所以不能用它表示"没有草稿"——
    没有草稿是**没有这份文件**（同 D103 那个坑）。
    """

    messages: list[Message] = Field(default_factory=list)
    """被截断处之前的全部对话：用户消息、推理、工具调用与结果。

    存的是**本轮新增的那几条**，不含重放出来的历史——历史每次都从路径重建，
    存进来只会在续跑时重复一遍。
    """

    rounds: int = 0
    """已经用掉的 tool loop 轮数。续跑时接着数，硬上限才仍然是硬上限——
    否则反复打断可以无限续，上下文没有边界。
    """

    interrupted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_empty(self) -> bool:
        return not self.messages


def trim_dangling_tool_call(messages: list[Message]) -> list[Message]:
    """丢掉尾部那个没有收到结果的工具调用，连同它后面的一切。

    **打断多半正好落在这里**——调用发出去了，结果还没回来。留着它，续跑时
    OpenAI 兼容端点会因为 `tool_calls` 没有对应的 tool 消息而**拒绝整份请求**，
    于是打断过一次的会话再也跑不起来了。

    只需要检查最后一次带工具调用的 assistant 消息：在它之前的都已经配平过。
    同一条消息里发起多个调用而只回了一部分时，整条一起丢——**半份工具结果
    比没有更糟**，它对应不上任何调用。
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role is not Role.ASSISTANT or not message.tool_calls:
            continue
        answered = {m.tool_call_id for m in messages[index + 1 :] if m.role is Role.TOOL}
        if any(call.id not in answered for call in message.tool_calls):
            return messages[:index]
        return messages
    return messages
