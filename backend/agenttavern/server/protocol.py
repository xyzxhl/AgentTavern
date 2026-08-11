"""WebSocket 消息协议。

用 Pydantic 定义、导出 JSON Schema 生成 TS 类型，避免前后端手写重复定义漂移。

**为什么用 WebSocket 而不是 SSE**——即使已经放弃逐 token 流式（D7）：
``ask_question`` 的挂起可能很久且无超时（D66）、长循环下每个 run 完成要推送但没有
用户请求可搭载、trace 投影要实时、后续还有时间驱动模式。SSE 三条都不满足。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import ControlOverride, Node, TraceEvent

# --------------------------------------------------------------------------
# 客户端 → 服务端
# --------------------------------------------------------------------------


class Submit(BaseModel):
    """提交一条用户输入，**开新的一轮**（F3a「推进」）。

    **进队列，每条独立成一个 run，不打包**（D26）——RP 中每个用户动作都需要独立的
    世界响应。要插进正在跑的那一轮，用 `Interject`（F3b）。
    """

    type: Literal["submit"] = "submit"
    text: str
    control_override: ControlOverride | None = None
    """单轮覆盖（D45）。UI 上应做成快捷方式而非选择题。"""


class Interject(BaseModel):
    """把一句话插进**正在跑的**那一轮（F3b / D119）。

    **插入点是 agent 的一次操作之间，不是一轮之后。** 一次模型调用、一次工具调用
    做完就是一个边界。等一轮写完再说话不叫引导，那是事后补救——用户看着 director
    往错方向查了六次资料，得能当场拨回来。

    `interrupt` 决定在飞的那次操作怎么办：

    - ``False``（引导）：让它做完，在下一个边界插入
    - ``True``（打断）：掐掉它，就在那里插入

    没有 run 在跑时服务端会回落到 `Submit` 的语义——那本来就是"新的一轮"。
    """

    type: Literal["interject"] = "interject"
    text: str
    interrupt: bool = False


class Answer(BaseModel):
    """回答 director 通过 ``ask_question`` 提出的问题。"""

    type: Literal["answer"] = "answer"
    question_id: str
    selected: str


class Stop(BaseModel):
    """打断当前 run，不带话。元操作，进控制面。

    **不是"作废"**（D118）：掐掉在飞的那次操作后，这一轮挂起成草稿，下一条输入
    接着它跑。按下停止只说明"别再往下跑了"，没说"刚才那些都不算"。

    writer 阶段没有操作边界（一次不可分的生成），那里仍是取消整个 task，
    语义按 D60：保留已产出的正文片段。
    """

    type: Literal["stop"] = "stop"


class SwitchBranch(BaseModel):
    """切换活跃路径。**状态随之重放**（D48）——不这样做就会"文本对、状态悄悄错"。"""

    type: Literal["switch_branch"] = "switch_branch"
    node_id: str


class EditNode(BaseModel):
    """用户直接编辑节点数据（D65）。

    **直接改写，不做依赖检查**——乐观假设用户只改最新输出，此时本无下游。
    """

    type: Literal["edit_node"] = "edit_node"
    node_id: str
    outline: str | None = None
    prose: str | None = None
    state_set: dict[str, Any] | None = None
    """覆盖该节点 ``state_delta`` 的 ``set`` 部分。"""


class Regenerate(BaseModel):
    """重生成：在**当前节点**的父节点下新建兄弟（D46/D147）。原分支保留。

    ``node_id`` 必须是当前活跃叶——**分支只能在当前节点上开**（D147）。想在更早
    的地方分叉，先 `SwitchBranch` 切过去，再重生成或推进；**切换不受限制，
    受限的只是创建**。带着 id 而不是省掉它，是为了让"你以为的当前节点已经不是
    当前节点了"这件事当场报错，而不是悄悄重 roll 掉另一轮。
    """

    type: Literal["regenerate"] = "regenerate"
    node_id: str


ClientMessage = Submit | Interject | Answer | Stop | SwitchBranch | EditNode | Regenerate


class ClientEnvelope(BaseModel):
    message: ClientMessage = Field(discriminator="type")


# --------------------------------------------------------------------------
# 服务端 → 客户端
# --------------------------------------------------------------------------


class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: str


class Trace(BaseModel):
    """一条 trace 事件的投影。

    这里递出完整事件，**由客户端决定显示什么**——筛选规则与渲染模板属 UI 层，
    不得污染 trace schema（D25）。事件自带人类可读的 ``label``，
    "呈现 agent 工作过程"这一体验就落在它上面。
    """

    type: Literal["trace"] = "trace"
    event: TraceEvent


class Question(BaseModel):
    """director 停下来问用户。**不设超时**（D66）——等待期间逻辑上什么都没发生。"""

    type: Literal["question"] = "question"
    question_id: str
    question: str
    options: list[str]


class RunFinished(BaseModel):
    type: Literal["run_finished"] = "run_finished"
    node: Node
    notes: list[str] = Field(default_factory=list)
    """eval 采样标记。**从不影响运行时行为**（D43/D44），仅供提示。"""


class RunFailed(BaseModel):
    type: Literal["run_failed"] = "run_failed"
    reason: str


class RunInterrupted(BaseModel):
    """用户打断了这一轮。**打断是挂起，不是作废**（D118）。

    `resumable` 为真表示推演已存成草稿：下一条输入会**接在它后面**继续，
    而不是从头重来。UI 必须把这件事说清楚——用户以为按的是"取消"，
    结果下一句话被接进了同一段推演，那是最难查的一类困惑。
    """

    type: Literal["run_interrupted"] = "run_interrupted"
    resumable: bool = False


class StateSnapshot(BaseModel):
    """当前路径上的完整状态。**派生数据**——权威是各节点的增量（D48）。"""

    type: Literal["state"] = "state"
    values: dict[str, Any]
    leaf_id: str | None = None


class QueueStatus(BaseModel):
    """队列可见性：用户应能看到待处理的输入并撤销尚未消费的部分。"""

    type: Literal["queue"] = "queue"
    pending: list[str]


class UsageUpdate(BaseModel):
    """成本可见性：**实时显示消耗，由用户手动打断**，不做自动预算拦截。"""

    type: Literal["usage"] = "usage"
    tokens_in: int
    tokens_out: int


class Branches(BaseModel):
    type: Literal["branches"] = "branches"
    node_id: str
    siblings: list[str]
    active_index: int


class GoalFinished(BaseModel):
    """goal 走完了，控制权交回用户（D141）。

    **`outcome` 必须传出去**：完成和完全偏离都停下来，但那是两件完全不同的事——
    一个是达成了，一个是这个目标已经不可能达成了，用户的下一步动作完全不同。
    """

    type: Literal["goal_finished"] = "goal_finished"
    outcome: str
    """`done` / `derailed`（见 `GoalOutcome`）。"""

    reason: str


class ServerError(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = (
    RunStarted
    | Trace
    | Question
    | RunFinished
    | RunFailed
    | RunInterrupted
    | StateSnapshot
    | QueueStatus
    | UsageUpdate
    | Branches
    | GoalFinished
    | ServerError
)
