"""一次被打断的 run。**打断是挂起，不是作废**（D118）；**打断掉的是那一个阶段**（D142）。

早先 director 阶段被打断就什么都不留（D60 原文）。那是错的：**打断和重生成不是
同一件事**。

| | 用户想要 | 已完成的部分 |
|---|---|---|
| **重生成** | 换一个结果 | 不要了 |
| **引导** | 在这个结果之后加一句 | 等它写完，再作为新的一轮 |
| **打断** | 现在就插一句 | **要**——它只是被截在了半路 |

打断唯一独有的东西是**时机**：它不等当前动作跑完，直接掐掉在飞的那次 API 调用
或工具调用。除了时机，它和引导要的是同一个东西——**在目前已完成的内容上继续**。

## 三个阶段，一条依赖链（D142）

一个 run 是 `输入 → 细纲 → 正文 → state` 三段串行。**打断把当前阶段判为失败，
它的产出作废**；已完成的上游产物存进草稿，恢复点由**下一次输入是不是空的**决定：

| 打断在 | 空输入 | 非空输入 |
|---|---|---|
| director | 接着草稿续跑 | 追加进草稿续跑 |
| writer | 只重跑 writer | **回 director** |
| state_manager | 只重跑 state_manager | **回 director** |

判据是**生成内容的依赖关系**：state 依赖正文，正文依赖细纲，细纲依赖输入。
用户又说了话，细纲就可能要改，那下游全都不作数了；用户什么也没说，就是
「刚才那步没跑完，接着跑」。

**这推翻了 D60 的另一半**（「writer 阶段保留正文片段」）。留半截正文看似省事，
实则是把一个失败的阶段当成了产出：下一轮会接着那半句往下写，而用户按停止
恰恰是因为不想要那半句。

回 director 时**旧细纲作为一条 context 给它**——「你上一次写的是这个，用户又
说了话，判断要不要改」。这条路会丢掉那一轮查过的资料，但那笔钱只在 **director
阶段**被打断时才真值得省，而那条路径原样保留着。

草稿按 `parent_id` 认领。**父节点不同就丢弃**——那说明用户已经切到别的分支上去了，
这份推演接不上（D48）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ..llm.types import Message, Role
from .node import UserInput
from .state import StateDelta


class RunStage(StrEnum):
    """一个 run 的三段。**顺序就是依赖顺序**，上游变了下游必须重来。"""

    DIRECTOR = "director"
    WRITER = "writer"
    STATE = "state"


class RunDraft(BaseModel):
    """一次未完成的 run，等着下一条输入把它接下去。"""

    parent_id: str | None = None
    """这份推演挂在哪个节点下。**续跑时必须一致**，否则丢弃。

    ``None`` 本身是合法的父节点（根），所以不能用它表示"没有草稿"——
    没有草稿是**没有这份文件**（同 D103 那个坑）。
    """

    stage: RunStage = RunStage.DIRECTOR
    """打断落在哪一段。它决定空输入时从哪儿接着跑。"""

    user_input: UserInput | None = None
    """被打断那一轮的输入。

    **唯一不可再生的东西。** director 阶段它在 `messages` 里也有一份，但
    writer / state 阶段那些消息已经不留了（回 director 走的是重跑，不是续跑），
    所以必须单独存。
    """

    messages: list[Message] = Field(default_factory=list)
    """被截断处之前的全部对话：用户消息、推理、工具调用与结果。

    **只在 `stage is DIRECTOR` 时有意义。** 存的是本轮新增的那几条，不含重放出来
    的历史——历史每次都从路径重建，存进来只会在续跑时重复一遍。
    """

    rounds: int = 0
    """已经用掉的 tool loop 轮数。续跑时接着数，硬上限才仍然是硬上限——
    否则反复打断可以无限续，上下文没有边界。
    """

    outline: str = ""
    """已经提交的细纲。writer / state 阶段被打断时它已经成型了。"""

    prose: str = ""
    deviation: str | None = None
    """已经写完的正文与偏离报告。**只有 state 阶段被打断时才有**——writer 阶段
    被打断意味着正文没写完，那半截作废。
    """

    staged_delta: StateDelta = Field(default_factory=StateDelta)
    """state_manager 已经写下的增量。那几次 `state_write` 是**已经完成的动作**，
    和已经拿到的工具结果没有区别，重来一遍只是白花一次调用（同 D118）。
    """

    interrupted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def is_empty(self) -> bool:
        return not self.messages and not self.outline


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
