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


# --------------------------------------------------------------------------
# Session 元指令
# --------------------------------------------------------------------------


class Goal(BaseModel):
    """长任务终止条件（D28）。

    机制：正常一轮生成结束后检查是否达标，未达标则开启下一 run。

    **goal 不是模式**——有无 goal 系统跑法一样，只是 run 末多一个判定。
    达标判定一律交给模型，不降级为代码：那些结构化字段的值本就是模型写的，
    代码读取比对不是独立验证（D30）。
    """

    description: str
    """用户设定的可判定标准。表述越具体，模型判定越准。"""

    max_runs: int = 50
    """硬上限保底——模型可能永远判不达标，或目标本身不可达。"""

    max_total_tokens: int = 1_000_000
    """硬上限保底（同上）。"""


class SessionMeta(BaseModel):
    """Session 元指令：这个 session 要什么体验。定义目标，不定义过程。

    文风与角色语言习惯归此处而非控制维度——它们不随 run 变化，
    是 writer 的常驻输入。
    """

    title: str = "未命名"
    content_scale: str = ""
    """剧情尺度：暴力、性、黑暗程度。"""

    genre_tone: str = ""
    """题材与基调。"""

    pov: str = ""
    """视角。"""

    prose_style: str = ""
    """文风指导。仅注入 writer（D33 的隔离基础）。"""

    character_voices: dict[str, str] = Field(default_factory=dict)
    """角色语言习惯：角色名 -> 说话方式。仅注入 writer。"""

    forbidden: list[str] = Field(default_factory=list)
    """否定项：不能提前揭示的、不允许发明的。

    **必须同时给 director 和 writer**（D35）——细纲说"会发生什么"，
    否定项说"不能写什么"，按定义不在细纲里。体量极小，不破坏隔离。
    """

    nsfw_guidance: str = ""
    """情色与身体描写的指导。**默认空，空则整节不注入。**

    与 `content_scale` 的分工：那个说**能到什么程度**（尺度），这个说**怎么写**
    （手法）。前者是判据，后者是文风的一部分。

    **两侧都要**，理由同 D35：director 决定这一轮发生什么（哪些事进不进这一轮），
    writer 决定怎么落笔。只给一侧，另一侧会按自己的默认来，两边对不上。
    """

    jailbreak: str = ""
    """破限文本。**默认空，空则整节不注入。**

    这是 SillyTavern 预设里的标准字段，位置约定是**整份提示词的最后一段**——
    越靠后权重越高。这里只提供槽位与位置，内容由用户自己填：
    它取决于用的是哪个模型、哪个端点，本项目不预设。

    **两侧都要**：director 和 writer 是两次独立的模型调用，
    只在一侧生效等于只解开了一半。
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
