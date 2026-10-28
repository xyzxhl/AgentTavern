"""节点：分支树的基本单元。

一个完整 run 原子落成一个 node。细纲只在 run 内作为 director → writer 的临时制品；
节点持久化最终正文与状态增量，不再把计划稿当成跨轮事实。

树结构与交互参考常见 AI chat（S44）：编辑/重生成产生兄弟节点，`< 2/3 >` 切换，
活跃路径是根到叶的一条链。关键差异是 **节点内容包括 state 增量**（S1）——
切换分支必须同时切换状态，否则会出现"文本对、状态悄悄错"这类最隐蔽的 bug。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from .state import StateDelta


def _now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class RunStatus(StrEnum):
    COMPLETE = "complete"

    # `PARTIAL`（writer 阶段被打断，保留正文片段）**已删除**（S49）。
    # 留半截正文看似省事，实则是把一个失败的阶段当成了产出：下一轮会接着那半句
    # 往下写，而用户按停止恰恰是因为不想要那半句。现在打断即该阶段失败，
    # 已完成的上游产物存进草稿等续跑，节点根本不落。

    FAILED = "failed"


class InputSource(StrEnum):
    """这条输入是谁给的（S102）。

    **只有 goal_manager 读它。** director 不知道有 goal 这回事，两种输入在它眼里
    完全一样——那是有意的：模拟输入必须和真实输入长得一模一样，否则 director
    会对它区别对待。
    """

    USER = "user"
    SIMULATED = "simulated"
    """goal_manager 替用户写的（S92）。"""


class UserInput(BaseModel):
    """一条用户输入。

    队列是 FIFO，**每条独立成一个 run，不得打包**（S107）——RP 中每个用户动作都
    需要独立的世界响应。把"我推开门"/"我环顾四周"/"我叫了一声"打包会跳过中间
    信息，而推开门看到什么本应影响要不要叫。
    """

    text: str
    source: InputSource = InputSource.USER
    """**给 goal_manager 分辨用的**（S102）：剧情偏了，是它自己推错了，还是用户
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
    """正常与 goal 驱动 turn 都会填写；可空是为旧数据与尚未装配完成的草稿兼容。"""

    # `control` **已删除**（R31/S80）：控制维度整体上调到提示词层之后，
    # 这个字段记的是一份全局配置的快照——每个节点存一遍同样的东西，而它既不
    # 随节点变化，也不再有"单轮覆盖"可记。要知道某一轮用的什么文风，看当时的
    # agent 配置，不看节点。

    prose_summary: str | None = None
    """这一段**正文**的摘要，存在段落的最后一个节点上（S57）。

    摘要与正文是 **n : 1**，不是一一对应：一段剧情可能横跨四轮，一次转场
    可能只占半轮。按轮切分不是语义单位，切出来的东西既不好读、条数也不收敛
    （50 轮就是 50 条索引，Q23 的规模问题原样还在）。

    ``None`` 表示这一节点还没有被归入任何已完成的段落，仍以正文原文进入 history。
    """

    interjections: list[str] = Field(default_factory=list)
    """本轮内用户按发生顺序插入的话（S50/S60）。

    跨轮只保留会进入后续剧情上下文的产品记录：顶层输入、插话与正文。director 的
    reasoning、tool call/result 只为本轮恢复而持久化在 ``RunDraft``，节点不保存。
    """

    closes_segment: bool = False
    """这一轮是不是一段剧情的结尾。**在生成之后当场判定**（S58）。

    判定与压缩仍是两个动作：**判定在生成时**（director 刚写完，知道这场戏收没收），
    **压缩在下一次重放时**兑现成摘要。

    合在一起做过一版，漏判很严重：待归段缓冲区里只有已滑出窗口的轮次，
    所以"第 9 轮是个分界"要等三轮之后才被问到——**而那时问的人已经看不到
    第 10 轮的转场了**。分界恰恰是靠后面那一轮才认得出来的。
    """

    segment_span: int = 1
    """``prose_summary`` 覆盖了几轮，包含本节点自己。

    **按"末节点 + 跨度"寻址，而不是记起止 id**，因为父指针唯一：从末节点
    上溯 ``span - 1`` 步得到的链是确定的，**任何经过末节点的路径都得到同一段**。
    分支因此天然安全（S42）。
    """

    prose: str = ""
    """writer 的最终产出，也是后续叙事上下文的唯一权威来源。"""

    deviation_report: str | None = None
    """writer 若为通顺而增删了实质内容则报告，通常为空（S8）。

    **细纲不回写**：writer 只报告偏离，编排层记录供审计；后续模型统一直接读取正文，
    不把偏离报告当作剧情 context。
    """

    state_delta: StateDelta = Field(default_factory=StateDelta)
    """由 state_manager 通过 ``state_write`` 提交（S7 起；此前是 director，S7）。

    **唯一写者仍然只有一个**，只是换了人：状态维护后置到 writer 之后，
    记的人因此看得见这一轮最终写出来的正文。
    """

    # `goal_plan` **已删除**（S92）。S92 把它挂在节点上是为了分支隔离；
    # goal 跑着的时候不允许切分支之后，那个理由不成立了。计划现在挂在
    # `Session.goal_run.plan` 上，跟着 goal 的生命周期走。
