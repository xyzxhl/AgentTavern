"""回归集：固定的场景 + 录制好的输入序列。

**回归集能保的只有流程，不是质量**（docs/Optimization.md §6）——创作输出不确定，
把某一次生成当成标准答案是错的。因此这里只固定**输入**，不固定期望输出。

两个场景是**刻意选成相反的**：

- ``noir``  文学场景。约束长效、定性、只增不减（谁知道什么、什么关系）。
- ``data``  数据化世界。约束短效、定量、有增有减（灵石、冷却、消耗品）。

只在一边成立的规则是那个场景的经验，不是规则。`remove` 语义的缺口（S32）和
"存续时长不是判据"（S34）都是被 ``data`` 揭穿的——两条都在 ``noir`` 上看着完全正常。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..models import (
    EntryKind,
    Goal,
    Session,
    SessionMeta,
    WorldbookEntry,
    WorldbookMode,
)
from ..prompting import PromptSetup


@dataclass(frozen=True)
class Scenario:
    name: str
    make_session: Callable[[], Session]
    worldbook: list[WorldbookEntry]
    """整份设定（S63/S64）：**只有条目，没有字段**。题材与基调也是一条
    `WORLD` 条目——它和「旧货店在哪」是同一种东西。设定不属于 session。"""

    world_name: str = "未命名"
    """这份设定叫什么。worldbook 上**唯一**的字段（S64）。"""

    inputs: list[str] = field(default_factory=list)
    """按顺序喂给同一个 session 的多轮输入。**每一项是独立的一轮**（S92）。"""

    note: str = ""

    prompts: PromptSetup = field(default_factory=PromptSetup)
    """这个场景要把哪几档提示词换掉（S80）。

    以前是 `session.control` / `session.capabilities`——那两个类整体上调到了
    提示词层，所以场景改的也是提示词。**改的是一份副本**（`prompts_as`），
    跑完不在仓库里留痕。
    """

    segments: list[int] = field(default_factory=list)
    """预期的段落切分：每一项是这一段占几轮，累加应等于 ``len(inputs)``。

    **只用于评测，不喂给模型。** 摘要的分界点由模型自己判断（S57），
    这里记的是"设计时认为的自然分界"，用来量它判得准不准。

    这是少数几个 program grader 能直接算的语义指标之一——分界点是位置，
    位置对不对是二值的，不需要品味。
    """

    @property
    def boundaries(self) -> set[int]:
        """预期分界点（累计轮次）。段长 [3,3,3] → {3, 6}，末尾不算分界。"""
        cuts: set[int] = set()
        total = 0
        for span in self.segments[:-1]:
            total += span
            cuts.add(total)
        return cuts


# 题材与基调是**一条 `WORLD` 条目**（S64）：它和「旧货店在老城区一条窄巷
# 尽头」是同一种东西，一段关于这个世界的字，凭什么要特殊处理。


def _tone(body: str) -> WorldbookEntry:
    """题材与基调那一条。**常驻**——它每轮都该在眼前，按需展开对它没有意义。"""
    return WorldbookEntry(name="题材与基调", kind=EntryKind.WORLD, content=body)


NOIR_TONE = _tone("现代，悬疑，克制。")
DATA_TONE = _tone("修真，带明确的数值系统。资源、冷却、伤势都是硬约束，不能含糊带过。")
PULP_TONE = _tone("都市异能，节奏快，爽感优先。冲突直给，不绕弯子。")
GUFENG_TONE = _tone("架空古代，宫廷。含蓄，话里有话，礼数是武器。")
XIHUAN_TONE = _tone("低魔西幻，边境小镇。冷硬，务实，魔法昂贵且有代价。")


# -- noir ------------------------------------------------------------------


def _noir_session() -> Session:
    return Session(
        meta=SessionMeta(title="回归·悬疑"),
    )


NOIR_WORLDBOOK = [
    NOIR_TONE,
    # 静态部分（性格、身份）留在 content；会变的部分（在哪、知道什么、关系）
    # 作为 seeds 迁移进 state——模型因此不必把设定抄过去（S68）。
    WorldbookEntry(
        name="林昭",
        kind=EntryKind.CHARACTER,
        # **用户扮演的角色是一般角色的子类**（S65）：它就是个角色，多一个标记。
        is_player=True,
        mode=WorldbookMode.ALWAYS,
        content="三十岁出头，本地报社记者。做事谨慎，习惯先观察再开口。",
    ),
    WorldbookEntry(
        name="林昭·初始状态",
        kind=EntryKind.STATE,
        values={
            "林昭": {
                "在场": True,
                "知情": ["三个月前收到匿名信，内含一张照片：陈屿与一个已死之人站在一起"],
            },
            "林昭与陈屿": {
                "关系": "认识多年，谈不上深交。林昭没提过照片的事。",
            },
        },
    ),
    WorldbookEntry(
        name="陈屿",
        kind=EntryKind.CHARACTER,
        mode=WorldbookMode.ALWAYS,
        content="四十岁，旧货店老板。说话习惯绕开正题，用反问代替回答。",
        # 语言习惯挂在角色身上，不再靠名字去 meta 那张字典里对（S65）。
        voice="说话绕，答非所问，很少给准话",
    ),
    WorldbookEntry(
        name="陈屿·初始状态",
        kind=EntryKind.STATE,
        values={
            "陈屿": {
                "在场": True,
                "知情": ["林昭手里有那张照片", "照片的来源"],
            },
        },
    ),
    WorldbookEntry(
        name="旧货店",
        mode=WorldbookMode.ON_DEMAND,
        content="老城区一条窄巷尽头。门口挂着褪色的蓝布帘，里面堆满旧钟表、搪瓷缸、"
        "成箱的旧杂志。柜台后有一扇通往里屋的门，常年锁着。店里常年有灰尘和机油味。"
        "陈屿在这里待了十二年，从没换过招牌。",
    ),
    WorldbookEntry(
        name="旧货店·初始状态",
        kind=EntryKind.STATE,
        values={"地点": "陈屿的旧货店", "时间": "下午，快收摊"},
    ),
]


NOIR = Scenario(
    name="noir",
    prompts=PromptSetup(choices={"writer": {"prose_style": "jishi", "pov": "third"}}),
    make_session=_noir_session,
    worldbook=NOIR_WORLDBOOK,
    world_name="回归·悬疑",
    inputs=[
        "我推开门走进钟表铺，柜台后面没有人。",
        "林昭没有立刻回答。她把那张照片从包里拿出来，正面朝下扣在柜台上。",
        "她把照片翻了过来。",
        "「这个人是谁。」",
        # 元指令：括号内容应隐式生效，不出现在正文里，也不该被写成林昭说出口的话。
        "（跳到当天夜里，林昭回到报社）",
    ],
    note="约束长效、定性、只增不减。用于查转述、抢话、知情累积。",
)


# -- noir_goal：goal 驱动的探针 ------------------------------------------------
#
# 除了种子那一条，**每一轮的输入都由 goal_manager 写**（S92）。要看的是四件事：
#
# 1. 中间状态拆得像不像**状态**——「陈屿说出照片上那个人是谁」是状态，
#    「让林昭追问」是步骤。拆成步骤是最容易犯的错。
# 2. 会不会**重给一版**。剧情一偏就该重给，抱着旧的一版硬推是 S92 那版的病。
# 3. 会不会**过早 `finish(done)`**。S94 把那条硬前提删了，现在全靠提示词
#    （`goal_judge` 那一节）担保——所以这一次实跑就是在验它。
# 4. 引导的**力度**：`steering_input` 要求「只施加最小必要干预」，
#    直接替角色把结果说出来就是过界。
#
# 目标写成**可判定的剧情状态**，不是抽象的进度描述——它是模型判定的唯一真值，
# 含糊的目标测不出任何东西。


def _noir_goal_session() -> Session:
    session = _noir_session()
    session.meta.title = "回归·目标驱动"
    session.meta.goal = Goal(
        description=(
            "林昭得知照片上那名男子的姓名与死因，并且陈屿明确承认自己认识他。"
            "三样缺一不可：姓名、死因、陈屿的承认。"
        ),
        # 八轮到顶就暂停。**够长到能看出重给一版，又不至于烧太多**——
        # 判不出来时它会一直推下去，那正是 `max_runs` 要兜的。
        max_runs=8,
    )
    return session


NOIR_GOAL = Scenario(
    name="noir_goal",
    prompts=PromptSetup(choices={"writer": {"prose_style": "jishi", "pov": "third"}}),
    make_session=_noir_goal_session,
    worldbook=NOIR_WORLDBOOK,
    world_name="回归·悬疑",
    inputs=["我推开门走进钟表铺，柜台后面没有人。"],
    note="goal 驱动：只给种子输入，其余每轮由 goal_manager 写。查拆状态、重给、过早收尾。",
)


# -- noir_impossible：**真正够不着的目标** --------------------------------------
#
# 先有过一版 `data_dead_end`，目标是「在只卖丹符的铺子里买到法宝，而身上只有三块
# 灵石」。**那不算不可达**——铺子收进一件当品是这个世界里再正常不过的事，
# 模型两轮就办成了，而它没有作弊。**「资源不够」和「不可能」是两回事**：
# 前者只是难，剧情总能补上；后者是**与已确立的事实逻辑矛盾**，补不上。
#
# 这一版按后者造：许平三年前已死、尸体确认过，而目标要他**当面**开口。
# 死人不能当面说话——这是事实的直接蕴含，不是难度。模型可以绕（录音、遗书、
# 托梦、认错人），但那些没有一个满足「当面告诉」。
#
# **事实故意放进种子输入里**，不只放在世界设定里。因为 goal_manager 读不到
# worldinfo（S92），事实只留在设定里的话它根本无从知道，那测的就成了
# 「它有没有拿到事实」而不是「它认不认得出矛盾」。这两件事要分开测——
# 另一半（事实只在设定里、不进剧情）值得单独跑一次。
#
# 三种结局各说明一件事：
#
# - `failed`——**它认了**。这是想要的。
# - `paused`（预算跑光）——一直在推，判不出来。`goal_judge` 那节不够硬。
# - `done`——它把「当面告诉」读松了，或者让死人开了口。**后者比判不出来严重**：
#   判不出来只是浪费，读松判据是把不可能说成可能。


PING = WorldbookEntry(
    name="许平",
    mode=WorldbookMode.ALWAYS,
    content="照片上那个男人。**三年前冬天死于城郊一场火灾**，尸体经过确认，葬在西山。"
    "生前在老城区一带做些小买卖，和陈屿有过往来。那张照片是他生前留下的最后一张。",
)


def _noir_impossible_session() -> Session:
    session = _noir_session()
    session.meta.title = "回归·不可能的目标"
    session.meta.goal = Goal(
        description="许平**当面**告诉林昭，那张照片是谁拍的。",
        max_runs=6,
    )
    return session


NOIR_IMPOSSIBLE = Scenario(
    name="noir_impossible",
    prompts=PromptSetup(choices={"writer": {"prose_style": "jishi", "pov": "third"}}),
    make_session=_noir_impossible_session,
    worldbook=[*NOIR_WORLDBOOK, PING],
    world_name="回归·悬疑",
    inputs=[
        "我把那张照片放在柜台上。“许平三年前就烧死了，这我知道。我想知道的是——这张照片，是谁拍的。”"
    ],
    note="goal 驱动、且目标与既有事实矛盾（死人当面开口）。查它认不认，还是把判据读松。",
)


# -- data ------------------------------------------------------------------


def _data_session() -> Session:
    return Session(
        meta=SessionMeta(title="回归·数据化"),
    )


DATA_WORLDBOOK = [
    DATA_TONE,
    WorldbookEntry(
        name="沈砚",
        kind=EntryKind.CHARACTER,
        is_player=True,
        mode=WorldbookMode.ALWAYS,
        content="炼气八层散修，二十四岁。用剑，也会几个符法。行事谨慎，宁可绕路不肯赊账。",
    ),
    WorldbookEntry(
        name="沈砚·初始状态",
        kind=EntryKind.STATE,
        values={
            "沈砚": {
                "在场": True,
                "境界": "炼气八层",
                "灵力": "六成",
                "伤势": "左肩旧伤未愈，发力时会牵扯",
                "灵石": 3,
                "随身": ["青锋剑", "止血符 ×2", "定身符 ×1", "半瓶回气丹（4 颗）"],
                "冷却": {},
            },
        },
    ),
    WorldbookEntry(
        name="柜台执事",
        kind=EntryKind.CHARACTER,
        mode=WorldbookMode.ALWAYS,
        content="青石镇坊市柜台的执事。只认规矩和价钱。",
        voice="公事公办，只认规矩和价钱",
    ),
    WorldbookEntry(
        name="法术",
        mode=WorldbookMode.ALWAYS,
        content="沈砚会的几手：\n"
        "- 御剑斩：耗灵力两成，用后冷却三回合。\n"
        "- 定身符：一次性消耗，对炼气九层以下有效，持续两回合。\n"
        "- 止血符：一次性消耗，止血但不接骨。\n"
        "- 回气丹：一颗回灵力三成，一个时辰内最多服两颗，超过会灼伤经脉。",
    ),
    WorldbookEntry(
        name="青石镇坊市",
        mode=WorldbookMode.ON_DEMAND,
        content="镇东头的小坊市，只有十几个摊位和一间挂着「聚宝」木牌的铺子。"
        "铺子收灵材、卖丹符，柜台执事姓孙。镇上唯一的客栈住一夜要两块灵石，"
        "坊市戌时关门。镇外二十里是黑风岭，近来常有妖兽下山。",
    ),
    WorldbookEntry(
        name="青石镇坊市·初始状态",
        kind=EntryKind.STATE,
        values={
            # 申时是 15–17 点，戌时是 19–21 点——中间隔着两个时辰，不是一个。
            # 首批归因 3/3 命中的正是这条：**回归集自己的设定前后矛盾**，
            # 模型忠实用了设定里的「戌时」，反倒被判成检索不足。
            # 记在这里是因为它说明 grader 抓得住真矛盾，包括素材里的。
            "地点": "青石镇坊市",
            "时间": "申时，离坊市关门约两个时辰",
            "坊市": {
                "行情": "止血符 1 石一张，回气丹 2 石一颗，客栈一夜 2 石",
            },
        },
    ),
]


DATA = Scenario(
    name="data",
    prompts=PromptSetup(choices={"writer": {"prose_style": "jishi", "pov": "third"}}),
    make_session=_data_session,
    worldbook=DATA_WORLDBOOK,
    world_name="回归·数据化",
    inputs=[
        "我走到聚宝铺子的柜台前，把袋子里的三块灵石倒在台面上，问执事：「止血符还有么，我要两张。」",
        # 逼它面对余额：买完只剩 1 石，而客栈一夜要 2 石。
        "我问他：「今晚镇上还有便宜些的住处么。」",
        # 逼它面对冷却与消耗：御剑斩用后三回合冷却，定身符只有一张。
        "天黑前我出镇往黑风岭走。走出十里，路边草丛里窜出一头狼形妖兽，直扑过来。我拔剑就是一记御剑斩。",
        "它躲开了，从侧面再扑上来。我再来一记御剑斩。",
        "我把定身符甩出去。",
    ],
    note="约束短效、定量、有增有减。用于查资源结算、冷却、消耗品增删。",
)


# -- noir_long：渐进式披露的探针 ------------------------------------------------


# 前 3 轮的细纲全文注入，更早的只以摘要在场（``RECENT_OUTLINES``）。因此**只有
# 长会话才走得到那条路径**——5 轮的场景里 `history_search` 与 `summarize` 一次
# 都不会被调用，等于这两个组件从未被测过。
#
# 探针改了三版，每一版的失效方式都值得记下来——**造一个能真正压到检索层的探针，
# 比修检索层本身难。**
#
# 一版：追问时**把要找的事实又说了一遍**（"三年前七月那天，你在哪"）。
#      答案就在眼前，当然不必检索。→ **问句里不能含有答案。**
# 二版：埋点落在第 2 轮细纲的**开头**，而索引回退是"前 120 字"，原样泄漏。
#      → **埋点要落在细纲中后段。**（这条同时暴露了 `summarize` 从未启用，
#      索引一直是头部截断——已改由编排层自动补摘要。）
# 三版：埋的是那一轮**中心问题的直接答案**（"你那天在哪" → "城东旧货市场"）。
#      摘要把它完整保住了，模型直接用上，**检索正确地闲置**。
#      → 这是渐进式披露**通过**，但探针什么也没测到。
#
# 由此得到探针的真正条件，它就是 S56 判据本身：**埋的东西当时必须不要紧。**
# 摘要保留"人物、地点、关键事件与因果"，所以埋点必须落在这四样之外——
# 一个背景细节、一行无关的字、一件没人在意的物件；等到很多轮之后再让它成为关键。
# 只有这种东西才会既进不了 state（不约束后续）、也留不住在摘要里（不是当时的重点）。
def _long_session() -> Session:
    """**必须显式打开 `history_search`。**

    它默认关闭（P14：能力开关默认关），关闭时 `history_search` / `history_read`
    根本不注册。第一次跑这个场景时忘了开——于是"12 轮里检索工具一次都没被调用"
    这个观察什么也说明不了：**工具当时不存在**。

    这是一类很值钱的错误：**测量对象没有被启用，而结果看起来像是一个发现。**
    """
    session = _noir_session()
    session.meta.title = "回归·长会话"
    return session


LONG = Scenario(
    name="noir_long",
    prompts=PromptSetup(
        choices={"writer": {"prose_style": "jishi", "pov": "third"}},
        # **必须显式打开检索。** 它默认关着，关着时 `history_search` /
        # `history_read` 根本不注册。第一次跑这个场景时忘了开——于是
        # 「12 轮里检索工具一次都没被调用」这个观察什么也说明不了：
        # **工具当时不存在**。这是一类很值钱的错误：测量对象没有被启用，
        # 而结果看起来像是一个发现。
        tools={"director": {"history_search": True}},
    ),
    make_session=_long_session,
    worldbook=NOIR_WORLDBOOK,
    world_name="回归·悬疑",
    inputs=[
        "我推开门走进钟表铺，柜台后面没有人。",
        # 埋点：一行台账。它**当时完全不要紧**——不是人物、不是地点、不是关键事件，
        # 也不构成因果，摘要没有理由留住它；它也约束不了后续，进不了 state。
        # 这正是 S56 说的那个交集盲区。
        "「三年前七月，许平出事那天，你在哪。」林昭把话问出口。"
        "问的时候她瞥见柜台角上摊着一本台账，最上面一行写着「7月12日 收 座钟一台 王」。",
        "我不说话，等他回答。",
        "「我不是来找你麻烦的。」",
        "我把照片从包里拿出来，正面朝下扣在柜台上。",
        "她把照片翻了过来。",
        "「这个人是谁。」",
        "我看着他的手。",
        "「那扇门后面是什么。」",
        "我往里屋走了两步。",
        # 探针：只给指代，不复述内容。要接住它，必须知道台账上那一行写了什么——
        # 而那一行既不在 state 里，也多半不在摘要里。
        "「我进门时看见你台账最上面那一行，」林昭说，「日期和名字，我都记下来了。」",
        "我把手机放在柜台上，屏幕朝上，录音正在走。",
    ],
    note="12 轮。查渐进式披露、summarize 的触发、以及早期细节会不会被编造替代（Q23）。"
    "探针：第 2 轮埋地名，第 11 轮只用指代去取。",
)


# -- chapters：多段剧情，用来量模型找不找得准语义分界 -------------------------------

# 摘要与细纲是 **n : 1**，而 n 由模型决定（S57）。**它能不能找到语义分界点，
# 是这套压缩能不能用的前提**：切错了，一段话被拦腰截断，两边都读不通；
# 切不出来，就退化回按轮切，条数不收敛。
#
# 这个场景刻意由**四段界限分明的剧情**组成，每段之间是**转场**——换地方、隔时间、
# 一件事有了结果。转场是最没有争议的一类分界；模型连这个都找不到，
# 更细的分界就不用谈了。
#
# `segments` 记的是设计时的预期切分，**不喂给模型**，只用来算它判得准不准。
# 这是少数几个 program grader 能直接算的语义指标——**分界点是位置，
# 位置对不对是二值的，不需要品味**（§3 的准入标准正好卡在这里）。


def _chapters_session() -> Session:
    session = _noir_session()
    session.meta.title = "回归·多段"
    return session


CHAPTERS = Scenario(
    name="chapters",
    prompts=PromptSetup(
        choices={"writer": {"prose_style": "jishi", "pov": "third"}},
        tools={"director": {"history_search": True}},
    ),
    make_session=_chapters_session,
    worldbook=NOIR_WORLDBOOK,
    world_name="回归·悬疑",
    inputs=[
        # 第一段：店里的对峙（3 轮）。以林昭拿出照片、被赶出来收束。
        "我推开门走进钟表铺，柜台后面没有人。",
        "我把照片从包里拿出来，正面朝上放在柜台上。「三年前七月，你在哪。」",
        "他没有回答。我把照片收回包里，转身走出店门。",
        # ── 转场：换地方、隔时间 ──
        # 第二段：报社查资料（3 轮）。以查到一个名字收束。
        "（当天夜里，报社资料室）我把三年前七月的旧报纸调了出来。",
        "我一页一页翻过去，找那几天的社会版。",
        "我在一则简讯里看到了许平的名字，旁边还有一个名字：王建军。",
        # ── 转场：换地方、隔时间 ──
        # 第三段：城东旧市场找人（3 轮）。以扑空收束。
        "（第二天上午，城东旧货市场）我按报纸上的地址找了过去。",
        "我向摊主打听王建军。",
        "摊主说王建军两年前就搬走了，没人知道去哪。",
        # ── 转场：回到起点 ──
        # 第四段：回店里摊牌（3 轮）。
        "（当天下午，回到钟表铺）我又一次推开那扇门。",
        "「王建军。」我把这个名字放在柜台上。",
        "他抬起头看着我。",
    ],
    segments=[3, 3, 3, 3],
    note="四段剧情，段间是转场。用来量模型找语义分界的准确率（S57）。",
)


# -- pulp：文风的另一极 ---------------------------------------------------------

# 前两个场景的文风都是克制文学向（现在同为 `jishi` 档），而它们产出的正文
# **节奏几乎一样**。这就有两种可能：writer 确实在跟着文风走、只是两种文风本来就近；
# 或者 **writer 根本没在跟文风走**，一直在用同一副默认腔调。
# 两个场景分不开这两种解释。
#
# 因此需要一个**风格反差最大**的场景：节奏快、情绪外放、心理直给、爽感优先。
# 它同时是目标"表达自然、达到主流网文水平"的量尺——拿克制文学向的样本
# 去量网文水平是量错了尺子。


def _pulp_session() -> Session:
    return Session(
        meta=SessionMeta(title="回归·网文"),
    )


PULP_WORLDBOOK = [
    PULP_TONE,
    WorldbookEntry(
        name="周野",
        kind=EntryKind.CHARACTER,
        is_player=True,
        mode=WorldbookMode.ALWAYS,
        content="二十三岁，城中村送外卖。三个月前一场车祸后能"
        "「听见」三秒后的声音——够他躲开一拳，不够他躲开一颗子弹。没告诉过任何人。",
    ),
    WorldbookEntry(
        name="周野·初始状态",
        kind=EntryKind.STATE,
        values={
            "周野": {
                "在场": True,
                "能力": "预听三秒",
                "秘密": ["能力的事没有第二个人知道"],
                "欠款": 8000,
            },
        },
    ),
    WorldbookEntry(
        name="赵铖",
        kind=EntryKind.CHARACTER,
        voice="居高临下，说话带刺，习惯用反问压人",
        mode=WorldbookMode.ALWAYS,
        content="裕丰快递站站长。三十五岁，手里攥着站里所有人的排班和罚款。"
        "看不起送外卖的，尤其看不起欠钱的。",
    ),
    WorldbookEntry(
        name="赵铖·初始状态",
        kind=EntryKind.STATE,
        values={"赵铖": {"在场": True}},
    ),
    WorldbookEntry(
        name="小满",
        kind=EntryKind.CHARACTER,
        voice="语速快，爱插话，情绪都在脸上",
        mode=WorldbookMode.ALWAYS,
        content="十九岁，同站点的骑手。周野的邻居，从小一起长大。藏不住话。",
    ),
    WorldbookEntry(
        name="小满·初始状态",
        kind=EntryKind.STATE,
        values={"小满": {"在场": True, "知情": ["周野欠了八千"]}},
    ),
    WorldbookEntry(
        name="裕丰快递站",
        mode=WorldbookMode.ON_DEMAND,
        content="城中村口的临街铺面，卷帘门一半是坏的。里面堆着待分拣的包裹，"
        "一张掉漆的办公桌，桌上压着一块写满名字和数字的白板——那是罚款榜。"
        "站里二十来个骑手，大半住在后面那栋握手楼里。",
    ),
    WorldbookEntry(
        name="裕丰快递站·初始状态",
        kind=EntryKind.STATE,
        values={"地点": "裕丰快递站", "时间": "早上七点，早高峰前"},
    ),
]


PULP = Scenario(
    name="pulp",
    prompts=PromptSetup(choices={"writer": {"prose_style": "wangwen", "pov": "third"}}),
    make_session=_pulp_session,
    worldbook=PULP_WORLDBOOK,
    world_name="回归·网文",
    inputs=[
        "我推开卷帘门走进站里，赵铖正站在白板前面，手里拿着笔。",
        "「我这个月的罚款怎么算到一千二了。」",
        "他伸手来推我肩膀。我提前半步侧开了。",
        "小满在旁边看见了，张嘴就要说话。",
        "我抢在她前面开口：「行，一千二我认。」",
    ],
    note="文风的另一极：网文腔、节奏快、心理直给。用于查 writer 是否真的跟着 prose_style 走。",
)


# -- 古风：称呼、自称与不能出现的现代词 -------------------------------------------

# 挑这个题材是因为它压的是**别的场景压不到的东西**：
# 一是**称呼与自称**（奴婢/妾身/本宫/你我），用户给的对白设计里专门点了这条——
#   称呼的变化比一句解释更能说明关系；
# 二是**时代词汇的边界**，现代词一冒出来就出戏，而这是 program grader 查不了、
#   统计指标也看不见的东西，只能人读。


def _gufeng_session() -> Session:
    return Session(
        meta=SessionMeta(title="回归·古风"),
    )


GUFENG_WORLDBOOK = [
    GUFENG_TONE,
    WorldbookEntry(
        name="沈玉茹",
        kind=EntryKind.CHARACTER,
        is_player=True,
        voice="话不说满，越紧张越客气",
        mode=WorldbookMode.ALWAYS,
        content="十七岁，新入宫的选侍，父亲是外放的六品小官。识字，会画，不谙宫里的规矩。",
    ),
    WorldbookEntry(
        name="沈玉茹·初始状态",
        kind=EntryKind.STATE,
        values={
            "沈玉茹": {
                "在场": True,
                "位分": "选侍",
                "知情": ["昨夜有人往她窗下放了一支断簪"],
            },
        },
    ),
    WorldbookEntry(
        name="崔嬷嬷",
        kind=EntryKind.CHARACTER,
        voice="句句合规矩，规矩就是她的刀",
        mode=WorldbookMode.ALWAYS,
        content="掌事嬷嬷，在宫里三十二年。管教新人，也替上头递话。从不说重话，也从不放过错处。",
    ),
    WorldbookEntry(
        name="崔嬷嬷·初始状态",
        kind=EntryKind.STATE,
        values={"崔嬷嬷": {"在场": True, "知情": ["断簪是谁放的"]}},
    ),
    WorldbookEntry(
        name="拾翠殿",
        mode=WorldbookMode.ON_DEMAND,
        content="偏殿，住着四位新入宫的选侍。院里一口井，一棵老石榴。"
        "晨昏各一次点卯，迟到要记。宫规：选侍不得私出宫门，不得与外臣通信。",
    ),
    WorldbookEntry(
        name="拾翠殿·初始状态",
        kind=EntryKind.STATE,
        values={"地点": "拾翠殿", "时间": "清晨，点卯前"},
    ),
]


GUFENG = Scenario(
    name="gufeng",
    prompts=PromptSetup(choices={"writer": {"prose_style": "gufeng", "pov": "third"}}),
    make_session=_gufeng_session,
    worldbook=GUFENG_WORLDBOOK,
    world_name="回归·古风",
    inputs=[
        "我把那支断簪收进袖子，推门出去。",
        "崔嬷嬷已经站在院里了。我上前行礼。",
        "「嬷嬷，昨夜我窗下落了样东西。」我把断簪双手递过去。",
        "她没有接。",
        "我把手收回来，簪子还在掌心里。",
    ],
    note="压称呼、自称、时代词汇边界。这三样统计看不见，只能读。",
)


# -- 西幻：专名密度与「翻译腔」 ----------------------------------------------------

# 西幻是**翻译腔最容易冒出来的地方**：人名地名是音译，句式容易跟着英文走
# （「他有着一双……」「这是一个……的男人」），而这正是用户点的
# 「地道的中文本土化表达」要对治的东西。
# 同时它压**专名一致性**：一轮里蹦出五个生造名词，读者记不住，state 也会炸。


def _xihuan_session() -> Session:
    return Session(
        meta=SessionMeta(title="回归·西幻"),
    )


XIHUAN_WORLDBOOK = [
    XIHUAN_TONE,
    WorldbookEntry(
        name="卡伦",
        kind=EntryKind.CHARACTER,
        is_player=True,
        mode=WorldbookMode.ALWAYS,
        content="三十岁，退役边境兵，如今替人跑腿收账。左手缺两指。不信神，但会照规矩画十字。",
    ),
    WorldbookEntry(
        name="卡伦·初始状态",
        kind=EntryKind.STATE,
        values={
            "卡伦": {
                "在场": True,
                "身上钱": "十一枚银币",
                "知情": ["雇主要他在日落前拿回一只封蜡的木匣"],
            },
        },
    ),
    WorldbookEntry(
        name="教士艾德蒙",
        kind=EntryKind.CHARACTER,
        voice="引经据典，但话里全是价钱",
        mode=WorldbookMode.ALWAYS,
        content="镇上唯一的教士，也是唯一能验魔物的人。收费从不明说，只说「随心奉献」。",
    ),
    WorldbookEntry(
        name="教士艾德蒙·初始状态",
        kind=EntryKind.STATE,
        values={"艾德蒙": {"在场": True, "知情": ["木匣里是什么"]}},
    ),
    WorldbookEntry(
        name="灰岩镇",
        mode=WorldbookMode.ON_DEMAND,
        content="边境上的矿镇，三百来口人。一条主街，一座石头教堂，一家挂着铁锚招牌的酒馆。"
        "镇上规矩：日落后关门，谁也不给开。验一次魔物的行价是三枚银币。",
    ),
    WorldbookEntry(
        name="灰岩镇·初始状态",
        kind=EntryKind.STATE,
        values={"地点": "灰岩镇教堂", "时间": "午后，离日落还有三个时辰"},
    ),
]


XIHUAN = Scenario(
    name="xihuan",
    prompts=PromptSetup(choices={"writer": {"prose_style": "jishi", "pov": "third"}}),
    make_session=_xihuan_session,
    worldbook=XIHUAN_WORLDBOOK,
    world_name="回归·西幻",
    inputs=[
        "我推开教堂的门，把木匣放在长凳上。",
        "「验一下。」",
        "艾德蒙没有动手。",
        "我把三枚银币摞在木匣旁边。",
        "他终于伸手，把木匣转了个方向。",
    ],
    note="压翻译腔与专名密度。低魔设定下钱是硬约束，同时可查资源结算。",
)


# -- forks：让 ask_question 一定会触发 -------------------------------------------
#
# `ask_question` 在别的场景里**几乎不出场**：`model_authority` 是 `medium`，那一档
# 的措辞是「本轮若存在真正的分岔，可以调用一次」，实测十几个 session 一共只问过
# 三次。三个样本量不出任何东西，所以这个场景把两件事同时拧到位：
#
# - **控制权调到 `low`**——「遇到任何值得由用户决定的分岔都调用它征询」。
# - **输入刻意留白或与设定冲突**，让分岔真的存在，而不是靠模型硬找。
#
# 要看的是三件事（S23）：
#
# 1. 选项是不是**恰好三个**。参数上写死了，这里量的是模型给不给得出来——
#    给不出就是一次工具错误，会进 `tool_errors`。
# 2. 三个选项是不是**三个不同的方向**，而不是同一条路的三种措辞。这条只能人看。
# 3. **自由作答**接不接得住。采集侧 `auto_answer` 奇数次自由作答、偶数次挑选项，
#    所以一次跑里两条路都会出现。


def _forks_session() -> Session:
    session = _noir_session()
    session.meta.title = "回归·岔路"
    return session


FORKS = Scenario(
    name="forks",
    prompts=PromptSetup(
        choices={
            "writer": {"prose_style": "jishi", "pov": "third"},
        },
        # **控制权在用户这边**：`ask_question` 的描述分三档，`low` 那档
        # 才会让它遇岔就问（S10②）。
        tools={},
    ),
    make_session=_forks_session,
    worldbook=NOIR_WORLDBOOK,
    world_name="回归·悬疑",
    inputs=[
        # 1. 留白：没说怎么开口，也没说来意。方向不明。
        "我推开门走进旧货店。",
        # 2. 留白：「问下去」有好几种问法，而且分量差得很远。
        "我决定不再绕弯子，直接问下去。",
        # 3. **与设定冲突**：worldbook 写的是「里屋的门常年锁着」，
        #    而这条输入要求它开着。冲突时该问，不该悄悄改设定。
        "我发现柜台后那扇门开着，就走了进去。",
        # 4. 留白：里面有什么完全没定，正是该由用户定的那种。
        "我在里屋看到了一样东西。",
        # 5. 留白 + 分量重：摊牌的方式决定后面整条线怎么走。
        "我拿着它走回柜台前。",
    ],
    note="ask_question 的专用探针：控制权 low + 输入留白/与设定冲突（S23）。",
)


SCENARIOS = {
    s.name: s
    for s in (
        NOIR,
        NOIR_GOAL,
        NOIR_IMPOSSIBLE,
        DATA,
        LONG,
        CHAPTERS,
        PULP,
        GUFENG,
        XIHUAN,
        FORKS,
    )
}
