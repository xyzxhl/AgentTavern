"""Session 元指令、控制维度、模式。

三类东西性质不同，不可混为一表（D38）：
  - 元指令   改变**目标**——这个 session 要什么体验。创建时设定，很少改。
  - 控制维度 改变**处理方式**——每 run 怎么处理。会话内可调。
  - 模式     改变**触发源**——谁触发生成。

详见 docs/ControlDimensions.md。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from ..config import LIMITS
from ..llm.types import Message

# --------------------------------------------------------------------------
# 控制维度（D42，四项）
# --------------------------------------------------------------------------


class InputHandling(StrEnum):
    """输入处理三档（D41）。取代了原先"转述二值 × 故事跨度"的设计。

    故事跨度不再是独立维度：想快进就写在指令里（那本就是指令内容），
    想零推进就是 NARRATE_ONLY。
    """

    NARRATE_AND_ADVANCE = "narrate_and_advance"
    """转述用户行为，然后继续演。典型输入："我推开门"。"""

    NARRATE_ONLY = "narrate_only"
    """只写用户这一步，停下等下一步。用户完全掌控节奏。"""

    ADVANCE_ONLY = "advance_only"
    """用户输入作为前提，直接写后续。典型输入："往悬疑走"。"""


class ModelAuthority(StrEnum):
    """模型剧情控制权。

    直接反映为 ``ask_question`` 工具的调用积极性——**控制权越高，调用积极性越低**（D39）。
    三档均保持 run ↔ 细纲一一对应：交互式创作是在一个 run 内多次提问，
    而非拆成多个 run（D58）。
    """

    LOW = "low"
    """一个 run 内多次提问，边推演边问（交互式创作）。"""

    MEDIUM = "medium"
    """约 1 次，细纲成型后给候选。"""

    HIGH = "high"
    """0 次，由 goal 驱动连续多个 run（长循环自主创作）。"""


class ExpansionDensity(StrEnum):
    """展开密度：每单位故事时间写多少字。

    与"故事跨度"正交——同一场戏可以写 200 字也可以写 2000 字。
    同时影响两侧：细纲的事件密度（director）与正文的铺陈程度（writer）。
    """

    SPARSE = "sparse"
    NORMAL = "normal"
    RICH = "rich"


class ControlDimensions(BaseModel):
    """每 run 生效的控制维度取值。

    由用户显式选择而非模型推断；与输入内容冲突时**显式优先**（D15）。
    """

    input_handling: InputHandling = InputHandling.NARRATE_AND_ADVANCE
    model_authority: ModelAuthority = ModelAuthority.MEDIUM
    expansion_density: ExpansionDensity = ExpansionDensity.NORMAL

    allow_speaking_for_user: bool = False
    """是否允许抢话——模型能否替用户的角色说话、行动、描写心理。

    两种偏好都很常见，没有默认答案，推断必错一半，因此必须显式。
    """

    prose_style: str = "wangwen"
    """文风档位名。仅注入 writer（D33 的隔离基础）。

    **存的是 `writer.yaml` 里 `prose_style` 那个选项组的二级标题名**（`wangwen`、
    `gufeng`、`jishi`……），不是一段自由文本：文风是最该反复调的东西，写成自由
    文本时它散在每个 session 里，改一次只影响一个故事，而且从提示词那侧完全看
    不见；做成选项之后，一档就是一段可编辑的提示词，加一档只是加一个二级标题。

    **它在这里而不在 `SessionMeta`**，理由和 `expansion_density` 一样：文风是
    读者这一侧的偏好，不是这个世界的属性。同一本世界设定换个文风照样成立，
    换个人来读就该能换。题材与基调不同——那是世界自己的事，留在 `SessionMeta`。

    这里**不做成枚举**：加一档不该动 Python。名字对不上时 `choice()` 会报错，
    错误信息里带着文件名和键名。
    """

    pov: str = "third"
    """视角。同上——读者偏好，不是世界属性。

    **和文风一样是选项名，不是自由文本**：三档全文写在 `writer.yaml` 的 `pov`
    选项组里，这里只说选哪一档。改成选项组是因为「第一人称／第二人称／第三人称」
    本来就是三个互斥的档，写成自由文本时每个 session 各写各的措辞，
    writer 那侧无从对齐。
    """

    def merged_with(self, override: ControlOverride | None) -> ControlDimensions:
        """套用单轮覆盖。未指定的项沿用常驻设置（D45）。"""
        if override is None:
            return self
        patch = override.model_dump(exclude_none=True)
        return self.model_copy(update=patch) if patch else self


class ControlOverride(BaseModel):
    """单轮覆盖。全部字段可选；未指定即沿用常驻设置。

    UI 上应做成**快捷方式**（``/只转述``）而非选择题——显式化的代价边界
    在交互形态上，不在维度数量上（D45）。
    """

    input_handling: InputHandling | None = None
    model_authority: ModelAuthority | None = None
    expansion_density: ExpansionDensity | None = None
    allow_speaking_for_user: bool | None = None
    prose_style: str | None = None
    pov: str | None = None


# --------------------------------------------------------------------------
# Session 元指令
# --------------------------------------------------------------------------


class Goal(BaseModel):
    """长任务终止条件（D28）。

    机制：正常一轮生成结束后检查是否达标，未达标则开启下一 run。

    **goal 不是模式**——有无 goal 系统跑法一样，只是 run 末多一次 goal_manager 的
    调用。达标判定一律交给模型，不降级为代码：那些结构化字段的值本就是模型写的，
    代码读取比对不是独立验证（D30）。
    """

    description: str
    """用户设定的可判定标准。表述越具体，模型判定越准。"""

    max_runs: int = LIMITS.goal_manager.max_runs
    """硬上限保底——模型可能永远判不达标，或目标本身不可达。"""

    max_total_tokens: int = LIMITS.goal_manager.max_total_tokens
    """硬上限保底（同上）。

    **两条预算都可以在 goal 跑着的时候调大**——那就是"加预算继续"（D148）。
    `description` 不行：对话是围着它建起来的，改了等于换了个目标。
    """

    # `max_revisions` **已删除**（D148）。它当初的理由是「重新规划这条路没有出口，
    # 模型永远可以再拆一层」。但重拆现在不是「又失败了一次」，是**脚手架跟着剧情
    # 走**——subgoal 只是让模型先想清楚路径，不是要逐项打勾的执行契约。
    # 留着它反而会在剧情正常波动几次之后误判 failed。止损交给上面两条预算：
    # 重拆本身不花钱，跑 run 才花钱。


class SubGoal(BaseModel):
    """一个**中间状态**。goal_manager 给出来的，不是用户写的。

    **它描述状态，不是步骤**，而且彼此之间**没有先后依赖**（D150）：可以被跳过、
    以不同顺序实现、同时实现，也可能因剧情变化而不再必要。所以这个列表是一组，
    不是一条链——序号只用来指代，不表示顺序。
    """

    description: str
    done: bool = False


class GoalOutcome(StrEnum):
    """goal 的**终态，只有两种**（D148）。

    `REPLAN` 不再是一种去向——**它是一个动作，不是结局**：任何一轮发现剩下的
    阶段失效了，就地重拆即可。`DERAILED` 并进了 `FAILED`。

    预算耗尽**不是终态**，是暂停（`GoalRun.paused`）：用户可以加预算接着跑。
    """

    DONE = "done"
    """完成。交回控制权。"""

    FAILED = "failed"
    """失败：**当前局面下拆不出任何能到达目标的路径**。

    判据要比「这一版计划没走通」重得多——那种情况该重拆。只有重拆也拆不出路时
    才是 failed。同样交回控制权，但用户需要知道是哪一种：一个是达成了，
    一个是这个目标已经不可能了，后续动作完全不同。
    """


class GoalPlan(BaseModel):
    """把 goal 拆成的一串阶段（D141/D148）。

    **它们是脚手架，不是执行契约。** 存在的目的是逼模型在动手之前先想清楚差距
    在哪；某一条最终有没有原样实现并不要紧——剧情变了就重给一版。因此没有
    「第几版」这种计数，也没有次数上限。

    但 `finish(done)` **要求全部标为成立**。这两条不矛盾，合起来才是要点：
    **重给随时可以，而宣布完成之前必须用「当前的」那一版重核一遍**，不是照着
    三轮前那份打勾。

    **它挂在 session 上**（`GoalRun.plan`），不再挂节点。D141 让它跟着节点走是
    为了分支隔离；goal 跑着的时候不允许切分支（D148），那个理由不成立了。
    """

    subgoals: list[SubGoal] = Field(default_factory=list)

    # `current`（第一个未达成的那个）**已删除**（D150）：中间状态之间没有先后
    # 依赖，挑出一个说"当前这个"等于凭空造了个顺序。要看还剩什么就看全部未成立的。

    @property
    def all_done(self) -> bool:
        return bool(self.subgoals) and all(s.done for s in self.subgoals)

    def render(self) -> str:
        lines = [
            f"{i + 1}. [{'已达成' if s.done else '未达成'}] {s.description}"
            for i, s in enumerate(self.subgoals)
        ]
        return "\n".join(lines)


class GoalRun(BaseModel):
    """一次 goal 从创建到结束期间的**运行时状态**（D148）。

    和 `SessionMeta.goal` 分开：那个是**用户配置**（目标是什么、给多少预算），
    这个是**跑起来之后的东西**（对话、进度、花销）。混在一起的话，"用户改了什么"
    和"系统跑出了什么"就分不开，而前者要禁改、后者每轮都在变。

    **它挂在 session 上，不挂节点。** goal 跑着的时候不允许切分支，所以 D141 让
    `GoalPlan` 跟着节点走的那个理由（分支隔离）不再成立。
    """

    conversation: list[Message] = Field(default_factory=list)
    """goal_manager 的对话，**跨 run 持续到这个 goal 结束**。

    静态前缀（任务说明 + 目标 + 节奏）在整个 goal 生命周期里不变，之后每轮只
    追加一条「第 N 轮发生了什么」。**这才是维持对话真正买到的东西**——以前每次
    都重发全量剧情梗概，梗概越来越长、前缀每轮失效。
    """

    plan: GoalPlan | None = None
    """当前计划与进度。**权威在这里，不在对话里。**

    对话是过程，这份是状态（同 `staged_delta` 的性质）。预算判断、UI 展示、
    恢复时的重建都读它——从对话里反解析既慢又脆。
    """

    spent_runs: int = 0
    spent_tokens: int = 0
    """已经花掉的。**落盘**——它们以前是 `SessionRunner` 的实例字段，
    进程重启就归零，于是 D28 那个用来兜住自主运行成本的上限实际上不生效。
    """

    paused: bool = False
    paused_reason: str = ""
    """停下了，但 goal 还在。两个来源：用户主动暂停，或预算耗尽。

    **预算耗尽不是失败**（D148）：用户可以把上限调大接着跑，所以它是暂停而不是
    终态。合成同一个状态是因为对用户而言它们是同一件事——停下了，要不要继续。
    """

    started_at_leaf_id: str | None = None
    """goal 是在哪个节点之后设的。**它把路径切成两段**（D150）：

    - **它之前**——goal 还不存在时写出来的剧情，进静态前缀，给**细纲**
      （摘要策略和 director 一样：归过段就用摘要，否则头部截断）。
    - **它之后**——goal 运行期间的每一轮，逐条追加，给**正文**。

    分开是因为两段要回答的问题不同：前面那段是"故事到这儿了"，细纲够用而且便宜；
    goal 期间要判的是"这一步到底有没有发生"，而**细纲只说定了什么，正文才是
    最终写出来的东西**——留白处落成了什么、话说到什么份上，只有正文看得见。

    ``None`` 表示这个 goal 从会话一开始就在，整条路径都算 goal 期间。
    """

    seen_leaf_id: str | None = None
    """对话已经追加到哪个节点为止。运行期间靠它做增量追加。

    恢复时**不靠它续，而是整份重建**：暂停期间用户可能自己跑了几轮、还可能切了
    分支，而「以最终选择的分支为准」——增量追加撤不回来，重建才做得到。
    """

    def exhausted(self, goal: Goal) -> str | None:
        """预算到顶了没有。到了返回一句人能读的理由，没到返回 ``None``。"""
        if self.spent_runs >= goal.max_runs:
            return f"已跑满 {goal.max_runs} 轮"
        if self.spent_tokens >= goal.max_total_tokens:
            return f"已用掉 {self.spent_tokens} token（上限 {goal.max_total_tokens}）"
        return None


class SessionMeta(BaseModel):
    """Session 元指令：**这个世界是什么**。定义目标，不定义过程。

    分界线是「换个人来读还成不成立」：题材与基调、尺度、角色语言习惯都是世界
    自己的属性，跟着世界设定走，换谁来读都一样；文风和视角不是——它们是读者
    这一侧的偏好，所以和铺陈程度一起归 `ControlDimensions`。

    **没有「否定项」字段**（D139）。不能提前揭示的东西属于世界设定，写进
    worldinfo 或 state；「不许写什么」这类写作纪律属于提示词，写进 YAML。
    单立一个字段等于给同一件事开第三个入口，而它两侧都没人注入。
    """

    title: str = "未命名"
    content_scale: str = ""
    """剧情尺度：暴力、性、黑暗程度。"""

    genre_tone: str = ""
    """题材与基调。**这是世界自己的事**，所以留在这里而不是控制维度。"""

    character_voices: dict[str, str] = Field(default_factory=dict)
    """角色语言习惯：角色名 -> 说话方式。仅注入 writer。"""

    user_character: str = ""
    """用户扮演的是谁。**仅注入 writer**——它决定人称怎么落。

    归这里而不是控制维度：换个人来读，"用户扮演林昭"这件事仍然成立，
    它是这个故事的设定，不是读者偏好（同 D138 那条判据）。视角（第几人称）
    才是偏好，那个在 `ControlDimensions.pov`。
    """

    goal: Goal | None = None


class SummarizeMode(StrEnum):
    MANUAL = "manual"
    """归段由**用户**决定：不做生成后追问，只在用户主动要求时归段。

    仍然保留"到 ``MAX_SEGMENT_RUNS`` 强制断开"那条止损支路——否则关掉自动之后
    索引会一直退化在头部截断上，而那个回退是**位置偏置**的，不是中性的。
    """

    AUTO = "auto"
    """每轮生成后追问 director"这一段收了没有"，收了就归段（D112）。

    追问放在**生成之后**而不是 tool loop 里，因为它对本轮产出毫无帮助，
    却要占往返、分散注意力、还和 `submit_outline` 的终止语义打架。

    但它**必须问 director 本人**：判据是"这场戏完了没有"，需要 state、前情和
    故事走向——另起一个只看得见几段细纲的冷调用，判据全靠猜。追问接在它自己的
    对话后面，因此几乎不花额外的输入 token。
    """


class Capabilities(BaseModel):
    """能力开关。**关闭时对应工具不注册**——模型根本看不到它。

    这是第四类配置，与元指令（目标）/ 控制维度（处理方式）/ 模式（触发源）都不同：
    它决定**系统能做什么**，而不同故事需要的能力差别很大。这与 D74 是同一个判断——
    不预设所有故事都一样。

    「不注册」比「注册了但拒绝调用」强：后者仍然占 prompt 体量、仍会被尝试、
    失败还要消耗一轮往返。工具面越小，模型的决策空间越小（分配原则 P9）。
    """

    nudge: bool = True
    """模型只回文本时推它一句（D140）。

    关闭时那句话根本不发——模型空手回来这一轮就算完。措辞在 `director.yaml`
    的 `nudge` 选项组里，这个开关只决定用不用 `off` 那一档。
    """

    state: bool = True
    """结构化状态。关闭时 ``state_query`` / ``state_write`` 都不注册，
    且 ``submit_outline`` 不再要求先提交增量（E0 契约随之失效）。
    """

    state_query: bool = True
    """state 的**按需展开**。关闭时 ``state_query`` 不注册，而 state **每轮全量注入**。

    两件事必须绑在一起：注入时截断长值、再让模型用工具展开，是"无损降维"；
    **一旦没了展开的路，截断就成了有损筛选**——模型看得见键名却永远拿不到内容，
    而它不知道自己丢了什么。那正是 D62 判为不合格的形态（lorebook 关键词触发同理）。
    """

    worldinfo_read: bool = True
    """设定的**按需展开**。关闭时 ``worldinfo_list`` / ``worldinfo_read`` 不注册，
    所有条目（含标记为按需的）一律**全文进静态前缀**。

    设定是跨轮不变的，所以它天然属于缓存前缀（D114）——全量注入的代价只付一次。
    """

    history_search: bool = False
    """细纲历史的检索。关闭时**禁止检索**——只有近期全文 + 更早的长期总结，
    ``history_search`` / ``history_read`` 都不注册。
    """

    upstream_runs: int = LIMITS.windows.upstream_runs
    """给 writer 几轮**正文**上文。它只用来接住语气和现场，不是长程记忆——
    长程记忆是 director 的事，它读细纲历史（D52/D54）。
    """

    state_recent_runs: int = LIMITS.windows.state_recent_runs
    """给 state_manager 几轮「细纲 → 成文 → 当时记下的」示范（D143）。

    **这是 state_manager 最大的一笔开销**，也是整个系统里最贵的一段上下文：
    它是滑动窗口，每轮全变，一点缓存前缀都吃不到，而前几轮的正文是其中的大头。
    实测 `data` 场景取 3 时，state_manager 占掉了全场 42% 的 input token。
    往下调省钱，代价是键名和值的形状更容易漂移。

    **和 `upstream_runs` 分开**：两者要的东西不一样。writer 要的是语气的连续，
    少一轮只是接得生硬些；state_manager 要的是记法的示范，少一轮直接影响
    键名一致性。合成一个参数会让其中一侧被另一侧的理由绑架。
    """

    summarize: SummarizeMode = SummarizeMode.AUTO
    """**默认开**，这与其他能力开关的"默认关"相反（D78），因为它的回退项不是中性的。

    没有摘要时索引回退到"细纲前 120 字"——那不是"精度低一点"，而是**位置偏置**：
    开头的东西原样保留、结尾的东西完全消失，留下什么取决于它碰巧在哪个位置。
    而它在上下文里还顶着「总结」的名字。**一个会误导模型的回退，不能当作安全的默认值。**
    """

    ask_question: bool = True
    """停下来问用户。关闭时模型只能自己拿主意——注意这与控制权等级不同：
    等级调节**积极性**，这里是**有没有这个动作**。
    """

    trace: bool = False
    """**开发者模式**。trace 服务于 eval，不是产品功能，默认关闭。

    关闭时事件仍会推给 UI（"呈现 agent 工作过程"依赖它），但**不落盘**——
    这正是 D25 要求 trace 与 UI 呈现分两层的好处：两者可以独立开关。
    """


class SessionModes(BaseModel):
    """模式：改变触发源的东西。目前只有一项。"""

    time_driven: bool = False
    """时间驱动：没有用户输入时也触发生成。

    与控制权等级不同——控制权管"跑多久停下来问一次"，
    时间驱动管"没有用户输入时会不会跑"。两者可叠加。
    """
