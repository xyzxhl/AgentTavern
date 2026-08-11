"""节点：分支树的基本单元。

**run = node = 一份细纲 = 一份正文**，四者严格一一对应（D51）。

树结构与交互参考常见 AI chat（D46）：编辑/重生成产生兄弟节点，`< 2/3 >` 切换，
活跃路径是根到叶的一条链。关键差异是 **节点内容包括 state 增量**（D47）——
切换分支必须同时切换状态，否则会出现"文本对、状态悄悄错"这类最隐蔽的 bug。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from ..llm.types import Message
from .control import ControlDimensions
from .state import StateDelta


def _now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class RunStatus(StrEnum):
    COMPLETE = "complete"

    # `PARTIAL`（writer 阶段被打断，保留正文片段）**已删除**（D142）。
    # 留半截正文看似省事，实则是把一个失败的阶段当成了产出：下一轮会接着那半句
    # 往下写，而用户按停止恰恰是因为不想要那半句。现在打断即该阶段失败，
    # 已完成的上游产物存进草稿等续跑，节点根本不落。

    FAILED = "failed"


class InputSource(StrEnum):
    """这条输入是谁给的（D148）。

    **只有 goal_manager 读它。** director 不知道有 goal 这回事，两种输入在它眼里
    完全一样——那是有意的：模拟输入必须和真实输入长得一模一样，否则 director
    会对它区别对待。
    """

    USER = "user"
    SIMULATED = "simulated"
    """goal_manager 替用户写的（D141）。"""


class UserInput(BaseModel):
    """一条用户输入。

    队列是 FIFO，**每条独立成一个 run，不得打包**（D26）——RP 中每个用户动作都
    需要独立的世界响应。把"我推开门"/"我环顾四周"/"我叫了一声"打包会跳过中间
    信息，而推开门看到什么本应影响要不要叫。
    """

    text: str
    source: InputSource = InputSource.USER
    """**给 goal_manager 分辨用的**（D148）：剧情偏了，是它自己推错了，还是用户
    主动把剧情带走了？后者强得多——用户的意图优先于目标，那往往意味着计划该重拆，
    甚至该判 failed。不标注的话两种偏离在梗概里长得一模一样，而应对完全不同。
    """

    submitted_at: datetime = Field(default_factory=_now)


class Node(BaseModel):
    """一个 run 的全部产物。落盘为 ``nodes/<id>.json``。

    只存**权威数据**：当前状态是沿路径重放 ``state_delta`` 的结果，不落盘快照。
    """

    id: str = Field(default_factory=lambda: new_id("run"))
    parent_id: str | None = None
    created_at: datetime = Field(default_factory=_now)
    status: RunStatus = RunStatus.COMPLETE

    user_input: UserInput | None = None
    """时间驱动模式或 goal 驱动的后续 run 可能没有用户输入。"""

    control: ControlDimensions = Field(default_factory=ControlDimensions)
    """本 run 实际生效的取值（常驻设置 + 单轮覆盖合并后的结果）。"""

    outline: str = ""
    """director 的产出：无文风约束的文章（D34）。

    是散文而非结构化对象——不需要预设字段格式（不依赖未经证实的写作理论），
    模型也不必做"把想写的翻译成 schema"这个元层任务。
    """

    outline_summary: str | None = None
    """这一段剧情的摘要，**存在段落的最后一个节点上**（D110）。

    摘要与细纲是 **n : 1**，不是一一对应：一段剧情可能横跨四轮，一次转场
    可能只占半轮。按轮切分不是语义单位，切出来的东西既不好读、条数也不收敛
    （50 轮就是 50 条索引，Q23 的规模问题原样还在）。

    ``None`` 表示这一节点还没有被归入任何已完成的段落——它要么还在窗口内，
    要么在等这一段结束。索引对它回退到"前 N 字 + 字数"，仍满足 D62。
    """

    director_messages: list[Message] = Field(default_factory=list)
    """这一轮给 director 的对话增量：用户消息、模型推理、工具调用与工具结果（D114）。

    **只存增量，不存整份对话**——一条消息只属于一个节点，重放时沿路径拼接即可，
    分支天然安全（D48）。

    存它是为了让 director 的上下文**跨轮持续**：它看得见自己上一轮查过什么、
    为什么那么写，比把历史重新渲染成一段文字信息量大得多。正文仍然从不进来（D54）。
    """

    closes_segment: bool = False
    """这一轮是不是一段剧情的结尾。**在生成之后当场判定**（D113）。

    判定与压缩必须分开：**判定在生成时**（director 刚写完，知道这场戏收没收），
    **压缩在需要时**（那几轮滑出全文窗口之后）。

    合在一起做过一版，漏判很严重：待归段缓冲区里只有已滑出窗口的轮次，
    所以"第 9 轮是个分界"要等三轮之后才被问到——**而那时问的人已经看不到
    第 10 轮的转场了**。分界恰恰是靠后面那一轮才认得出来的。
    """

    segment_span: int = 1
    """``outline_summary`` 覆盖了几轮，包含本节点自己。

    **按"末节点 + 跨度"寻址，而不是记起止 id**，因为父指针唯一：从末节点
    上溯 ``span - 1`` 步得到的链是确定的，**任何经过末节点的路径都得到同一段**。
    分支因此天然安全（D48）。
    """

    prose: str = ""
    """writer 的产出：成品正文。**director 从不读它**（D54）。"""

    deviation_report: str | None = None
    """writer 若为通顺而增删了实质内容则报告，通常为空（D57）。

    D36「细纲更新为实际发生的」由此落地：writer 不改写细纲，只报告偏离，
    编排层记录；下一 run 的 director 读 history 时可见。
    """

    state_delta: StateDelta = Field(default_factory=StateDelta)
    """由 state_manager 通过 ``state_write`` 提交（D143 起；此前是 director，D55）。

    **唯一写者仍然只有一个**，只是换了人：状态维护后置到 writer 之后，
    记的人因此看得见这一轮最终写出来的正文。
    """

    # `goal_plan` **已删除**（D148）。D141 把它挂在节点上是为了分支隔离；
    # goal 跑着的时候不允许切分支之后，那个理由不成立了。计划现在挂在
    # `Session.goal_run.plan` 上，跟着 goal 的生命周期走。

    def summary_or_head(self, head_chars: int = 120) -> str:
        """history 索引用的摘要。无缓存时回退到头部截断。

        **截断要出声**：一个省略号说不清丢了多少，而模型据此判断"这一轮我看全了
        没有"。所以直接把还剩多少字写出来——那是它决定要不要 `history_read`
        的唯一依据。隐藏（"用工具展开"）不算截断，那条路是无损的；这里是真的
        砍掉了尾巴，砍掉了就得说。
        """
        if self.outline_summary:
            return self.outline_summary
        if len(self.outline) <= head_chars:
            return self.outline
        return f"{self.outline[:head_chars]}……（截断，全文共 {len(self.outline)} 字）"
