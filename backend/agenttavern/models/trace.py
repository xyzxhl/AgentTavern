"""trace：完整可回放的执行记录。

两条约束（S112 / S111 / S47）：

1. **trace 与 UI 呈现分两层。** trace 完整、机器可读、不筛选；UI 是它的**投影**
   （选事件 + 渲染成人话）。筛选规则与渲染模板属 UI 层，**不得污染 trace schema**，
   否则 trace 会被 UI 需求牵着走、eval 时反而缺字段。

   但 trace 必须携带**足够的语义信息使投影可行**——若只记
   ``state_query(dimension=..., subject=...)``，事后无论如何也渲染不出
   "确认了她现在在哪"。这部分事后补代价很高，必须在设计时纳入。

2. **trace 是全局 append-only，不随分支切换**（与 state 相反）。eval 需要看到
   全部被放弃的尝试——用户重 roll 五次就有五条，而重 roll 率本身是"模型是否在写
   用户想要的东西"最直接的行为信号。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TraceEventType(StrEnum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"
    RUN_INTERRUPTED = "run_interrupted"

    CONTEXT_ASSEMBLED = "context_assembled"

    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"

    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    OUTLINE_SUBMITTED = "outline_submitted"
    PROSE_PRODUCED = "prose_produced"

    # `CONTRACT_CHECK` **已删除**：它记的是 E0（提交细纲之前必须写过一次
    # `state_write`），而 director 卸下 `state_write` 之后那条契约自然作废（S7）。
    # 留一个谁都发不出来的事件类型，等于让 schema 承诺一件代码不做的事。
    CROSS_CHECK = "cross_check"

    STATE_COMMITTED = "state_committed"

    GOAL_EVALUATED = "goal_evaluated"

    USER_EDIT = "user_edit"
    """用户直接编辑 state / 细纲 / 正文（R22）。来源是用户，仍须入 trace。"""

    USER_INTERJECTED = "user_interjected"
    """用户在 run 跑到一半时插了一句（F3b / S50）。

    必须单独一类：它改变了这一轮的走向，而且发生在**某两次操作之间**——
    事后看细纲完全看不出来这一轮被拨过一次，只能靠 trace 定位。
    """


class Actor(StrEnum):
    ORCHESTRATOR = "orchestrator"
    DIRECTOR = "director"
    WRITER = "writer"
    GOAL_MANAGER = "goal_manager"
    """目标管理者（S95）。它拆阶段、核对进度、替用户写下一条输入、判目标去向。
    以前叫 `goal_judge`，那时它只做最后一件事——**判定只是它四件事里的一件**，
    名字得跟上。
    """
    STATE_MANAGER = "state_manager"
    """状态维护者（S7）。**跑在 writer 之后**，读这一轮的细纲和成文更新 state。
    以前这活在 director 手上（trace 里记的是 `DIRECTOR`），边推演边记。
    """
    USER = "user"


class TraceEvent(BaseModel):
    """一条 trace 记录。落盘为 ``trace/<run_id>.jsonl`` 的一行。"""

    run_id: str
    seq: int
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: TraceEventType
    actor: Actor

    label: str
    """**人类可读的语义标签**，供 UI 投影渲染（S111）。

    例："确认了她现在在哪"，而不是 ``state_query(location)``。
    这是"呈现 agent 工作过程"这一体验的架构侧前提——把延迟从"卡顿"变成"沉浸"。
    """

    payload: dict[str, Any] = Field(default_factory=dict)
    """机器可读的完整细节，不做筛选。"""

    tokens_in: int | None = None
    tokens_out: int | None = None
    """成本可见性：实时显示消耗，由用户手动打断，不做自动预算拦截（S43 决议）。"""

    cache_hit: int | None = None
    """输入里命中前缀缓存的那部分（DeepSeek 的 `prompt_cache_hit_tokens`）。

    **S60 的全部理由就是缓存命中，而它此前从没被记下来过。** 命中条件很硬：
    从第 0 个 token 起完全相同的前缀才算，中间对上不算，最小粒度 64 token。
    所以任何动到 system prompt 或早期消息的改动都会让整条前缀作废——
    "每收一段就换一段等于每隔几轮动一次前缀"现在可以被量出来，不必再靠推理。
    """


class AttributionClass(StrEnum):
    """归因六类（S12）。归因的可行性是 director/writer 拆分的副产品——
    中间量物化了才分得清"规划错了"和"执行错了"。

    其中五类有可供 AI 对照的参照物；writer 文风失败仍需人工判断。
    """

    DIRECTOR_RETRIEVAL = "director_retrieval"
    """细纲与 state 矛盾 → 改默认包覆盖 / schema。"""

    DIRECTOR_REASONING = "director_reasoning"
    """细纲与用户指令不符（漏转述、覆盖不全）→ 改 director 契约。"""

    WRITER_DEVIATION = "writer_deviation"
    """正文与细纲不符 → 改 writer 契约 / 细纲粒度（Q21）。"""

    WRITER_STYLE = "writer_style"
    """符合细纲但读起来不对 → 改文风约束（Q20）。"""

    WRITE_OMISSION = "write_omission"
    """增量与正文不符 → 改写入契约。"""

    WRITE_REDUNDANCY = "write_redundancy"
    """增量写入了不该进入 state 的内容 → 改记录门槛与既有 state 注入。"""
