"""WebSocket 消息协议。

用 Pydantic 定义、导出 JSON Schema 生成 TS 类型，避免前后端手写重复定义漂移。

**为什么用 WebSocket 而不是 SSE**——即使已经放弃逐 token 流式（P21）：
``ask_question`` 的挂起可能很久且无超时（S24）、长循环下每个 run 完成要推送但没有
用户请求可搭载、trace 投影要实时、后续还有时间驱动模式。SSE 三条都不满足。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import Node, Session, StateDelta, SubGoal, TraceEvent, Tree

# --------------------------------------------------------------------------
# 客户端 → 服务端
# --------------------------------------------------------------------------


class Submit(BaseModel):
    """提交一条用户输入，**开新的一轮**（F3a「推进」）。

    **进队列，每条独立成一个 run，不打包**（S107）——RP 中每个用户动作都需要独立的
    世界响应。要插进正在跑的那一轮，用 `Interject`（F3b）。
    """

    type: Literal["submit"] = "submit"
    text: str

    # **没有单轮覆盖字段**（S80）：控制维度的取值住在提示词层，「覆盖」
    # 没有被覆盖的对象了。要改一档就去改那份 agent YAML。


class Interject(BaseModel):
    """把一句话插进**正在跑的**那一轮（F3b / S50）。

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
    """回答 director 通过 ``ask_question`` 提出的问题。

    ``selected`` **不校验是否在选项里**（S23）：用户可以三选一，也可以都不选、
    自己写一条。校验等于把「模型没想到的第四条路」判为非法，而那恰恰是最该
    传下去的一种回答。工具那侧按"在不在选项里"分开写回执。
    """

    type: Literal["answer"] = "answer"
    question_id: str
    selected: str


class Stop(BaseModel):
    """打断当前 run，不带话。元操作，进控制面。

    **不是"作废"**（S48）：掐掉在飞的那次操作后，这一轮挂起成草稿，下一条输入
    接着它跑。按下停止只说明"别再往下跑了"，没说"刚才那些都不算"。

    writer / state 阶段没有操作边界（各是一次不可分的生成），那里靠取消整个 task。
    **掐到哪一段，哪一段就判为失败、产出作废**，已完成的上游存进草稿等续跑
    （S49）。**半截正文不留**——留着，下一轮会接着那半句往下写，而用户
    按停止恰恰是因为不想要它。
    """

    type: Literal["stop"] = "stop"


class SwitchBranch(BaseModel):
    """切换活跃路径。**状态随之重放**（S42）——不这样做就会"文本对、状态悄悄错"。"""

    type: Literal["switch_branch"] = "switch_branch"
    node_id: str


class EditNode(BaseModel):
    """用户直接编辑节点数据（R22）。

    **直接改写，不做依赖检查**——乐观假设用户只改最新输出，此时本无下游。
    """

    type: Literal["edit_node"] = "edit_node"
    node_id: str
    user_input: str | None = Field(default=None, title="EditedUserInput")
    """替换这一 turn 的顶层输入文字；来源与提交时间保持不变（S164）。"""
    prose: str | None = None

    state_delta: StateDelta | None = None
    """**改指令**：整份替换该节点的增量。

    以前这里只有 `state_set`，只能覆盖 `set` 那一半——于是 S32 补的 `remove`
    （以及 `append`）从界面根本够不着，"清单要能减"这件事只有模型做得到。
    """

    state_result: Tree | None = None
    """**改结果**：这一轮跑完之后**你想要的状态树**，服务端和上一轮求差写回。

    和 `state_delta` 是同一件事的两个视图（R22），只能给一个。收结果而不是让
    前端自己算差，是为了不破坏「权威是增量」（S42）：换算发生在进门那一刻，
    落盘的仍然只有增量。
    """


class Regenerate(BaseModel):
    """重生成：在**当前节点**的父节点下新建兄弟（S44）。原分支保留。

    ``node_id`` 必须是当前活跃叶——**分支只能在当前节点上开**（S44）。想在更早
    的地方分叉，先 `SwitchBranch` 切过去，再重生成或推进；**切换不受限制，
    受限的只是创建**。带着 id 而不是省掉它，是为了让"你以为的当前节点已经不是
    当前节点了"这件事当场报错，而不是悄悄重 roll 掉另一轮。
    """

    type: Literal["regenerate"] = "regenerate"
    node_id: str


class SetGoal(BaseModel):
    """设一个新目标（S104）。**已有 goal 时报错**——同一时间只有一个。

    `description` 一旦定下就不能改：goal_manager 的对话是围着它建起来的，
    改了等于换了个目标，而对话还是旧的。要换就先 `GoalControl(abandon)`。
    """

    type: Literal["set_goal"] = "set_goal"
    description: str
    max_runs: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    """**留空即不设上限**（S97）——原样透传，服务端不替用户填默认值。

    下界是 1 而不是 0：`0` 表达不了任何意图（一设上就是"已经跑满"，goal 当场
    暂停），而它最可能的来源是前端把空字符串转成了数字。宁可当场报错。
    """


class SetGoalBudget(BaseModel):
    """改预算。**这是"加预算接着跑"的入口**（S96）。

    和 `SetGoal` 分开，因为它俩的可改时机相反：目标定了就不能动，
    预算恰恰要能在跑到一半时调大——预算耗尽是暂停不是失败。
    """

    type: Literal["set_goal_budget"] = "set_goal_budget"
    max_runs: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    """**这里的 `None` 是"这条不动"，不是"改成无上限"**——和 `SetGoal` 不同。

    两条消息的语义差别在于有没有"原来的值"：设立时留空是在表态，改预算时留空
    只是没提到它。要改成无上限走 `unlimited`。
    """

    unlimited: bool = False
    """把两条上限一起去掉。**从有上限改成没上限需要一个明确的动作**——
    否则它和"这条不动"在协议里长得一模一样（都是缺省）。"""


class GoalControl(BaseModel):
    """暂停 / 恢复 / 放弃当前 goal（S104）。

    **恢复不是"从头再来"**：对话原样留着，只是按当前路径补齐暂停期间发生的轮次。
    暂停期间用户可以自己跑几轮、也可以切分支——恢复时**以最终选择的分支为准**。
    """

    type: Literal["goal_control"] = "goal_control"
    action: Literal["pause", "resume", "abandon"]


ClientMessage = (
    Submit
    | Interject
    | Answer
    | Stop
    | SwitchBranch
    | EditNode
    | Regenerate
    | SetGoal
    | SetGoalBudget
    | GoalControl
)


class ClientEnvelope(BaseModel):
    message: ClientMessage = Field(discriminator="type")


# --------------------------------------------------------------------------
# 服务端 → 客户端
# --------------------------------------------------------------------------


class RunStarted(BaseModel):
    type: Literal["run_started"] = "run_started"
    run_id: str
    input: str


class Trace(BaseModel):
    """一条 trace 事件的投影。

    这里递出完整事件，**由客户端决定显示什么**——筛选规则与渲染模板属 UI 层，
    不得污染 trace schema（S111）。事件自带人类可读的 ``label``，
    "呈现 agent 工作过程"这一体验就落在它上面。
    """

    type: Literal["trace"] = "trace"
    event: TraceEvent


class Question(BaseModel):
    """director 停下来问用户。**不设超时**（S24）——等待期间逻辑上什么都没发生。"""

    type: Literal["question"] = "question"
    question_id: str
    question: str
    options: list[str]


class Delta(BaseModel):
    """流式分片（S118）：**正文或思考的一小段**，边生成边推。

    **它是旁路，不是权威**——权威仍然是 `RunFinished` 带回来的那个节点。分片
    可能因为断线而丢，前端据此显示的东西必须能被最终结果整个替换掉，不能拿
    分片拼出来的字符串当真值存下来。

    **工具调用不走这里**：它们的参数是逐字拼出来的 JSON，半截 JSON 对界面毫无
    用处，只会逼前端去猜；等完整了随 trace 一次性给。
    """

    type: Literal["delta"] = "delta"
    run_id: str
    actor: str
    """哪个 agent 在写。前端据此决定这段字往哪儿放——正文区还是过程区。"""

    kind: Literal["content", "reasoning"]
    """`content` 是要写出来的东西，`reasoning` 是它的思考段。"""

    text: str


class RunFinished(BaseModel):
    type: Literal["run_finished"] = "run_finished"
    node: Node
    notes: list[str] = Field(default_factory=list)
    """eval 采样标记。**从不影响运行时行为**（P12/P12），仅供提示。"""


class NodeUpdated(BaseModel):
    """一个节点被用户改过了（R22）。**回传整个节点，不回传"改了什么"。**

    改「结果」那个视图下，落盘的增量是**服务端求差算出来的**——只把用户发来的
    那棵结果树塞回本地节点，本地的 `state_delta` 就成了旧的，而它才是权威数据
    （S42）。回传整份是唯一不需要客户端猜的做法。
    """

    type: Literal["node_updated"] = "node_updated"
    node: Node


class RunFailed(BaseModel):
    type: Literal["run_failed"] = "run_failed"
    reason: str


class RunInterrupted(BaseModel):
    """用户打断了这一轮。**打断是挂起，不是作废**（S48）。

    `resumable` 为真表示推演已存成草稿：下一条输入会**接在它后面**继续，
    而不是从头重来。UI 必须把这件事说清楚——用户以为按的是"取消"，
    结果下一句话被接进了同一段推演，那是最难查的一类困惑。
    """

    type: Literal["run_interrupted"] = "run_interrupted"
    resumable: bool = False


class StateSnapshot(BaseModel):
    """当前路径上的完整状态。**派生数据**——权威是各节点的增量（S42）。"""

    type: Literal["state"] = "state"
    values: dict[str, Any]
    leaf_id: str | None = None


class SessionUpdated(BaseModel):
    """**服务端在 run 之外改了 session，整份推回去。**

    没有它的时候，凡是不触发一轮生成的改动客户端一律不知道：按「暂停」目标真的
    暂停了，而按钮还写着「暂停」；按「放弃」目标真的没了，而面板照旧挂着。
    **服务端做了事，界面一动不动**——用户读到的是"这个按钮坏了"。

    它没有被发现是因为**设目标那一条顺带 `nudge()` 起了一轮**，正文开始动，
    看起来像是有反应；而 pause / abandon / 加预算三条不起轮，就全成了哑的。

    **推整份而不是推差异**：session 不大，而"哪几个字段变了"是个每加一个字段
    就要重想一次的问题——漏掉一个的症状又是同一种，界面显示旧的东西。

    客户端**不必也不该自己在发完之后去拉一次**：WebSocket 发出去和 HTTP GET
    之间没有先后保证，那一拉可能落在服务端写盘之前，拿回旧的那份。
    """

    type: Literal["session_updated"] = "session_updated"
    session: Session


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
    """goal 走完了，控制权交回用户（S96）。

    **`outcome` 必须传出去**：两种终态都停下来，但那是两件完全不同的事——
    一个是达成了，一个是这个目标已经不可能达成了，用户的下一步动作完全不同。
    """

    type: Literal["goal_finished"] = "goal_finished"
    outcome: str
    """`done` / `failed`（见 `GoalOutcome`）。"""

    reason: str = ""


class GoalProgress(BaseModel):
    """goal 又推了一轮之后的进度（S129）。

    **不推整个 `GoalRun`**：它带着 goal_manager 那份跨 run 的长对话，一轮就上
    十万字，而界面一个字都用不上。这里只给要显示的东西。

    **为什么需要这条消息**：进度只写在盘上的 `goal_run` 里，WS 那侧此前只有
    「结束」和「暂停」两条——也就是说 goal 跑着的那几轮，界面上什么都不会变，
    直到它突然结束。而中间状态一条条被划掉，恰恰是用户最想看的东西。
    """

    type: Literal["goal_progress"] = "goal_progress"
    subgoals: list[SubGoal] = Field(default_factory=list)
    spent_runs: int = 0
    spent_tokens: int = 0
    max_runs: int | None = None
    max_total_tokens: int | None = None
    """``None`` = 这条没设上限（S97）。**不是 0**——0 会被界面渲染成一条
    满格的进度条。"""


class GoalPaused(BaseModel):
    """goal 停下了，**但没有结束**（S92）。

    和 `GoalFinished` 分开是因为用户的下一步完全不同：结束了就是结束了，
    暂停则是"要不要接着跑"——最常见的来源是预算到顶，把上限调大就能继续，
    对话原样留着，不用重新判一遍。
    """

    type: Literal["goal_paused"] = "goal_paused"
    reason: str


class ServerError(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = (
    RunStarted
    | Delta
    | Trace
    | Question
    | RunFinished
    | NodeUpdated
    | RunFailed
    | RunInterrupted
    | StateSnapshot
    | QueueStatus
    | UsageUpdate
    | Branches
    | GoalFinished
    | GoalProgress
    | GoalPaused
    | SessionUpdated
    | ServerError
)
