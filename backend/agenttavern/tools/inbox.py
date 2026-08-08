"""跑到一半插进来的用户输入（F3b）。

**单位是 agent 的一次操作，不是一轮。**

这一条纠正了此前把「引导」理解成"排队，等这一轮写完再作为新的一轮"。那样理解的话，
用户看着 director 查了六次资料往一个错方向走，也只能等它写完整段正文才说得上话——
**那不是引导，那是事后补救**。

因此两个动作的插入点都在**操作边界**上，差别只有一处：

| | 在飞的那次操作 | 插入时机 |
|---|---|---|
| **引导** | 让它做完 | 下一个边界 |
| **打断** | 掐掉 | 立刻，就在被掐掉的地方 |

「操作」指的是 director 的一次模型调用或一次工具调用。

## 为什么不能在任意时刻插

消息序有硬约束：**assistant 发出 `tool_calls` 之后，紧跟着必须是对应的 tool 结果**，
中间塞一条 user 消息，请求会被端点直接拒。所以边界只有三处：一轮开始前、
纯文本回复之后、**一次响应的全部工具结果都补齐之后**。

掐掉一次工具调用时也一样：不能就这么把调用悬在那里，要**补一条说明它被打断了的
工具结果**。这既让消息序合法，也如实告诉模型刚才发生了什么。
"""

from __future__ import annotations

import asyncio


class Inbox:
    """一个 run 的插话信箱。**每个 run 一个**，不跨 run 复用。"""

    def __init__(self) -> None:
        self._pending: list[str] = []
        self.interrupted = asyncio.Event()
        """有人要求掐掉在飞的那次操作。到边界后由 ``drain`` 清掉。"""

        self.accepting = False
        """现在有没有人在看这个信箱。**只在 director 的 tool loop 期间为真。**

        writer 是一次不可分的生成，没有操作边界可插——所以进了 writer 之后
        打断只能取消整个 task（D60 的语义：保留已产出的正文片段）。
        少了这个旗标，写正文时按停止就会石沉大海：信号进了没人读的信箱。
        """

    def push(self, text: str = "", *, interrupt: bool = False) -> None:
        """``interrupt=False`` 是引导，``True`` 是打断。

        **不带文本的打断是合法的**——那是"停下来别跑了"，此时 run 会挂起成草稿
        等下一条输入（D118）。
        """
        if text:
            self._pending.append(text)
        if interrupt:
            self.interrupted.set()

    def drain(self) -> list[str]:
        out = list(self._pending)
        self._pending.clear()
        self.interrupted.clear()
        return out

    @property
    def waiting(self) -> bool:
        return bool(self._pending)
