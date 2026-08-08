"""director 的 tool loop 与上下文装配。"""

from pathlib import Path

import pytest
from conftest import ScriptedLLM, text, tool, tools

from agenttavern.agents import DirectorFailed, run_director
from agenttavern.agents.prompts import (
    director_static_prefix,
    director_turn_prompt,
    writer_system_prompt,
)
from agenttavern.models import (
    Capabilities,
    ControlDimensions,
    ExpansionDensity,
    InputHandling,
    ModelAuthority,
    Node,
    Session,
    SessionMeta,
    StateDelta,
    UserInput,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
    replay,
)
from agenttavern.storage import Store
from agenttavern.tools import RunContext, ToolRegistry
from agenttavern.tracing import Tracer

ALL_ON = Capabilities(state=True, history_search=True, ask_question=True, trace=True)


def make_ctx(tmp_path: Path, llm: ScriptedLLM, session: Session | None = None):
    repo = Store(tmp_path).create_session(session or Session(capabilities=ALL_ON))
    ctx = RunContext(
        run_id="run_1",
        repo=repo,
        session=repo.load_session(),
        gateway=llm.gateway(),
    )
    registry = ToolRegistry.for_session(ctx.capabilities, repo.worldbook())
    return ctx, Tracer(repo, "run_1", persist=ctx.capabilities.trace), registry


def prompt_for(
    meta: SessionMeta | None = None,
    control: ControlDimensions | None = None,
    state: WorldState | None = None,
    path: list[Node] | None = None,
    capabilities: Capabilities | None = None,
    worldinfo: list[WorldbookEntry] | None = None,
) -> str:
    """director 收到的全部文字：静态前缀 + 这一轮的用户消息。

    对话跨轮持续之后（D114），提示词拆成了两块——**跨轮不变的进 system**，
    **每轮都变的（state、控制维度、用户输入）进这一轮的消息**。
    这里拼起来断言，是因为分工规则该在哪一侧是内容问题，不是位置问题。
    """
    caps = capabilities or ALL_ON
    return (
        director_static_prefix(
            meta or SessionMeta(),
            control or ControlDimensions(),
            capabilities=caps,
            worldinfo=worldinfo,
        )
        + "\n\n"
        + director_turn_prompt(
            control or ControlDimensions(),
            state or WorldState(),
            None,
            capabilities=caps,
        )
    )


async def test_happy_path(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("state_write", set={"地点": "客厅"}, append={"林.知情": ["有人来了"]}),
        tool("submit_outline", outline="她推开门，屋里坐着陈。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))

    assert result.outline == "她推开门，屋里坐着陈。"
    assert result.rounds == 2
    assert ctx.staged_delta.set["地点"] == "客厅"
    assert result.usage.tokens_in == 20


async def test_contract_pushback_is_recoverable(tmp_path: Path) -> None:
    """先提交细纲会被 E0 退回，模型补上 state_write 后应能继续（而非整轮失败）。"""
    llm = ScriptedLLM(
        tool("submit_outline", outline="太早了"),
        tool("state_write"),
        tool("submit_outline", outline="她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert result.outline == "她推开门。"
    assert result.rounds == 3


async def test_text_only_reply_is_pushed_back(tmp_path: Path) -> None:
    """模型只回文本时不能当成产出——细纲必须经 submit_outline，否则 E0 就绕过去了。"""
    llm = ScriptedLLM(
        text("我觉得应该这样写……"),
        tool("state_write"),
        tool("submit_outline", outline="她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert result.outline == "她推开门。"

    nudge = llm.messages_of(1)[-1]
    assert nudge["role"] == "user"
    assert "submit_outline" in nudge["content"]


async def test_calls_after_terminate_are_ignored(tmp_path: Path) -> None:
    """submit_outline 是终止点，同一次响应里它之后的调用不该再执行。"""
    llm = ScriptedLLM(
        tool("state_write"),
        tools(
            ("submit_outline", {"outline": "她推开门。"}),
            ("state_write", {"set": {"地点": "不该生效"}}),
        ),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert "地点" not in ctx.staged_delta.set


async def test_hard_limit_forces_close(tmp_path: Path) -> None:
    llm = ScriptedLLM(tool("state_query", key="location"))
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    with pytest.raises(DirectorFailed):
        await run_director(
            ctx, reg, tracer, UserInput(text="我推开门"), max_rounds=3, soft_rounds=1
        )

    assert "最后一轮" in llm.messages_of(2)[-1]["content"]


async def test_recoverable_tool_error_does_not_kill_run(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("state_query", key="不存在的键"),
        tool("state_write"),
        tool("submit_outline", outline="她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert result.outline == "她推开门。"


async def test_trace_carries_readable_labels(tmp_path: Path) -> None:
    """D25：trace 必须带人类可读语义标签，UI 才渲染得出"确认了她现在在哪"。"""
    llm = ScriptedLLM(
        tool("state_write", set={"地点": "客厅"}),
        tool("state_query", keys=["地点"]),
        tool("submit_outline", outline="她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)
    await run_director(ctx, reg, tracer, UserInput(text="我推开门"))

    labels = [e.label for e in ctx.repo.read_trace("run_1")]
    assert "查了 地点" in labels
    assert "更新了 地点" in labels
    assert "细纲完成" in labels
    assert all(e.label for e in ctx.repo.read_trace("run_1"))


# -- 上下文装配：拓扑的地基 --------------------------------------------------


def make_meta() -> SessionMeta:
    return SessionMeta(
        genre_tone="悬疑",
        prose_style="短句，冷调，不用比喻",
        character_voices={"陈": "说话总是绕圈子"},
        forbidden=["不要提前揭示照片的来源"],
    )


def test_director_never_sees_style_or_prose(tmp_path: Path) -> None:
    """D33 的隔离基础：文风与角色语言习惯只进 writer，正文只进 writer。"""
    prompt = prompt_for(meta=make_meta())

    assert "短句，冷调" not in prompt
    assert "说话总是绕圈子" not in prompt
    assert "不要提前揭示照片的来源" in prompt  # 否定项两边都要（D35）
    # 「正文不进 director」现在由 test_run.py 守着——对话跨轮持续之后（D114），
    # 前情在**重放的消息**里而不在提示词里，那条断言必须看全部上下文才有意义。


def test_writer_never_sees_state_or_history(tmp_path: Path) -> None:
    """writer 只承担文风。它拿不到状态值、世界设定、用户原始指令——都已在细纲里。"""
    prompt = writer_system_prompt(make_meta(), ControlDimensions())

    assert "短句，冷调" in prompt
    assert "说话总是绕圈子" in prompt
    assert "不要提前揭示照片的来源" in prompt
    assert "悬疑" not in prompt
    assert "state" not in prompt.lower()

    # 落笔手法归 writer——它管怎么写；推进与转述归 director——它管写什么。
    #
    # 断言的是**这一段在不在**，不是它的措辞。原来这里钉的是「情感落在外在」，
    # 而那句话已经被删了：它把「不要用情绪词概括」和「一律外化成身体动作」
    # 混成了一条，后半句会压过 session 自己的 `prose_style`（`pulp` 明写
    # 「心理活动直接写出来」，产出里却一个念头都没有）。**钉措辞会让提示词改不动。**
    assert "## 落笔" in prompt
    assert "视角人物每一段都该在想事情" in prompt
    assert "剧情锚点" not in prompt
    assert "实质变化" not in prompt


def test_authority_maps_to_ask_question_eagerness() -> None:
    """D39：控制权直接反映为 ask_question 的调用积极性——越高越少问。"""
    high = prompt_for(control=ControlDimensions(model_authority=ModelAuthority.HIGH))
    low = prompt_for(control=ControlDimensions(model_authority=ModelAuthority.LOW))

    assert "不要调用 ask_question" in high
    assert "一轮内可以多次" in low


def test_input_handling_three_ways() -> None:
    only = prompt_for(control=ControlDimensions(input_handling=InputHandling.NARRATE_ONLY))
    advance = prompt_for(control=ControlDimensions(input_handling=InputHandling.ADVANCE_ONLY))

    assert "不要推进后续剧情" in only
    assert "不要转述" in advance

    # 不转述时不必讲怎么转述——省下的是每 run 都要付的 token。
    assert "剧情锚点" in only
    assert "剧情锚点" not in advance


def test_narration_and_speaking_are_contrasted() -> None:
    """转述与抢话必须对照着说——两者最容易混，只分开讲模型会在边界上摇摆。"""
    prompt = prompt_for(control=ControlDimensions(allow_speaking_for_user=False))

    assert "转述只处理用户已经写出的" in prompt
    assert "抢话是替用户写尚未发生的" in prompt


def test_no_speaking_still_means_advancing() -> None:
    """禁止抢话不等于停在原地等指令——要给出可执行的**停止条件**。"""
    prompt = prompt_for(control=ControlDimensions(allow_speaking_for_user=False))

    assert "直到制造出需要用户回应的局面" in prompt
    assert "允许替用户的角色说话" not in prompt

    allowed = prompt_for(control=ControlDimensions(allow_speaking_for_user=True))
    assert "允许替用户的角色说话" in allowed


def test_state_pack_inlines_small_values_and_defers_large_ones() -> None:
    """与 history / worldinfo 同一模式（D63 全量索引 + 按需展开）：
    键名全给，体量小的值内联，大的只给体量，由模型按需 state_query 展开。
    """
    state = replay(
        [
            StateDelta(
                set={
                    "地点": "客厅",
                    "时间": "第三日 傍晚",
                    "林.性格": "谨慎，话少",
                    "林与陈.关系": "非常长的一段关系描述" * 30,
                },
                append={"林.知情": ["照片"]},
            )
        ]
    )
    prompt = prompt_for(state=state)

    assert "地点：客厅" in prompt
    assert "时间：第三日 傍晚" in prompt
    assert "性格：谨慎，话少" in prompt
    # 清单要**看得出是清单**——渲染成裸的顿号串时它和字符串没有区别，
    # 模型据此写回就会把它拍平，此后无法单独增删其中一项（D96）。
    assert "知情：清单：照片" in prompt
    # 大值只给体量，键名仍在——模型知道它存在，可以展开（D62）。
    assert "林与陈" in prompt
    assert "非常长的一段关系描述" not in prompt
    assert "state_query 展开" in prompt


def test_state_pack_groups_by_dotted_prefix() -> None:
    """`.` 仅用于渲染分组，系统不赋予它语义——不预设"角色"这类实体。"""
    state = replay([StateDelta(set={"时间": "傍晚", "林.在场": True, "林.性格": "谨慎"})])
    prompt = prompt_for(state=state)

    assert "时间：傍晚" in prompt
    assert "\n林\n" in prompt
    assert "  在场：是" in prompt


# -- 能力开关对 prompt 的影响 --------------------------------------------------


def test_state_off_removes_the_state_section_and_step() -> None:
    prompt = prompt_for(
        state=replay([StateDelta(set={"地点": "客厅"})]),
        capabilities=Capabilities(state=False),
    )
    assert "地点：客厅" not in prompt
    assert "state_write" not in prompt


def test_always_worldinfo_is_injected_on_demand_is_indexed() -> None:
    long_content = "".join(f"第{i}段设定。" for i in range(200))
    entries = [
        WorldbookEntry(name="主角设定", content="林，记者。", mode=WorldbookMode.ALWAYS),
        WorldbookEntry(name="教团", content=long_content, mode=WorldbookMode.ON_DEMAND),
    ]
    prompt = prompt_for(worldinfo=entries)

    # 常驻的全量注入。
    assert "林，记者。" in prompt

    # 按需的只进索引：名称、体量、摘要头部在，全文不在（D63）。
    assert "教团" in prompt
    assert f"{len(long_content)} 字" in prompt
    assert "第199段设定。" not in prompt
    assert "worldinfo_read" in prompt


def test_no_lookup_tools_means_no_lookup_step() -> None:
    prompt = prompt_for(
        capabilities=Capabilities(state=False, history_search=False, ask_question=False)
    )
    assert "用工具查" not in prompt
    assert "submit_outline" in prompt


def test_empty_state_renders_cleanly() -> None:
    prompt = prompt_for(capabilities=Capabilities(state=True))
    assert "尚无记录" in prompt


def test_density_reaches_both_sides_in_opposite_directions() -> None:
    """展开密度同时影响两侧，但含义相反：铺陈多 → 正文写得开，细纲少推进几件事。"""
    rich = ControlDimensions(expansion_density=ExpansionDensity.RICH)
    assert "充分铺陈" in writer_system_prompt(SessionMeta(), rich)
    assert "少推进几件事" in prompt_for(control=rich)

    sparse = ControlDimensions(expansion_density=ExpansionDensity.SPARSE)
    assert "克制铺陈" in writer_system_prompt(SessionMeta(), sparse)
    assert "多推进几件事" in prompt_for(control=sparse)


def test_description_belongs_to_writer_facts_belong_to_director() -> None:
    """outline 与 prose 差异过小的根因：director 拿到了篇幅要求，只能靠描写去凑。

    篇幅是**正文**的属性，只讲给 writer；director 那侧换成事件密度。
    这条分工要在两边都说明——单侧声明拦不住越界（同 D35 的理由）。
    """
    d = prompt_for()
    w = writer_system_prompt(make_meta(), ControlDimensions())

    # 篇幅分级只在 writer 一侧
    assert "篇幅跟着场景性质走" in w
    assert "篇幅跟着场景性质走" not in d

    # 同一组对照写在两边：事实归 director，描写归 writer
    assert "只写事实，不做描写" in d
    assert "只写事实，不做描写" in w
    assert "座钟" in d and "座钟" in w

    # director 被明确告知描写不归它管，且写了也白写
    assert "只会照抄" in d
    # writer 被明确告知这是展开而非润色
    assert "不是润色，是展开" in w


def test_user_actions_are_attempts_not_accomplished_facts() -> None:
    """**转述规则与世界规则会正面冲突**，必须明说谁让谁（D104）。

    实测：数据化世界里用户写「我再来一记御剑斩」，而御剑斩还在冷却，
    否定项又明写着"不要让冷却中的法术生效"。两条规则都成立，指向相反。

    模型当时选对了（写成催动失败），但**设计里没有任何地方这么规定过**——
    它是蒙对的。规则定为：动作是锚点，结果归世界；落空要写得可见、有据。
    """
    d = prompt_for()

    assert "用户写出的是他做了什么，不是这件事成没成" in d
    assert "结果由世界决定" in d
    # 只说"可以失败"不够——不写出失败的过程，就退化成了偷偷换动作
    assert "写清他确实试了、以及为什么没成" in d


def test_dialogue_wording_belongs_to_writer_meaning_belongs_to_director() -> None:
    """**语言描写也是描写**（D97）。

    细纲写"说了什么意思"，正文写"这句话怎么出口"。此前细纲直接写定台词，
    writer 只能照抄——`character_voices` 因此完全是死的：语言习惯没有落点。

    引号约定原先两侧都写着，后来判定为不必要的约束，已删。
    这里断言的是**职责划分**本身，不是任何一条具体措辞。
    """
    d = prompt_for()
    w = writer_system_prompt(make_meta(), ControlDimensions())

    assert "对白写意思，不写原话" in d
    assert "台词是你写的" in w

    # 放开措辞不等于放开内容——具体的东西两侧都要钉死
    assert "必须写死" in d
    assert "话里的信息不能变" in w


def test_anti_formula_lives_on_the_writer_side_only() -> None:
    """避开定式是**怎么写**的问题，只该在 writer 一侧。

    原先这条还断言 writer 里有"停在最后一件真的发生的事上"与"某人在等"两条禁令，
    它们已按判定删除——那是 director 的停止判据，writer 不需要重复一遍。
    """
    w = writer_system_prompt(make_meta(), ControlDimensions())
    d = prompt_for(control=ControlDimensions(allow_speaking_for_user=False))

    assert "同一个写法，一篇里不用第二次" in w
    assert "结尾必须落在角色一个可见的动作上" in w
    assert "同一个写法" not in d

    # director 侧保留停止判据：它要回答"推进到哪里就收"，那是写什么的问题。
    assert "细纲停在最后一件真的发生的事上" in d


def test_waiting_is_a_stop_condition_not_a_beat_to_write() -> None:
    """结尾定式的**根在 director**，不在 writer（D98）。

    抢话规则原先把停止条件列成"做出了等待回应的动作"，模型据此把「等」写进细纲，
    writer 只是忠实渲染——四份样本 4/4 以同一画面收尾。只在 writer 侧说"别老这么收尾"
    治不了：那是细纲给的事实，writer 照写才是对的。
    """
    d = prompt_for(control=ControlDimensions(allow_speaking_for_user=False))

    assert "细纲停在最后一件真的发生的事上" in d
    # 停止条件说「推进到需要用户回应为止」，**不描述那个局面长什么样**——
    # 一旦描述，它就从判据退化成内容模板（D124：列举里排第一的会变成默认选项）。
    assert "直到制造出需要用户回应的局面" in d
    assert "做出了等待回应的动作" not in d

    # 「不要用"某人在等"收尾」那一段已删：它是在治症状，而根因是上面那条
    # 停止条件曾经把局面描述了出来。判据留着，禁令不必留。
    assert "谁在等、等着谁开口" not in d


def test_speaking_rule_reaches_the_writer_too() -> None:
    """抢话是两侧共有的越界方式，规则也必须两侧都在（D35）。

    writer 只做照抄时够不着这条线；一旦它真的在展开，就会顺手把用户角色
    往前推一步——所以这条规则跟着「展开」一起加。
    """
    strict = ControlDimensions(allow_speaking_for_user=False)
    w = writer_system_prompt(make_meta(), strict)
    assert "不要替用户的角色新增动作" in w
    assert "细纲在哪里收住，正文就在哪里收住" in w

    # 允许抢话时这一节整体消失，而不是留个空标题
    loose = ControlDimensions(allow_speaking_for_user=True)
    assert "## 边界" not in writer_system_prompt(make_meta(), loose)


async def test_the_nudge_stops_asking_for_a_write_already_done(tmp_path: Path) -> None:
    """催促词只提**还差的那一步**。

    以前它无条件写着「先 state_write，再 submit_outline」。模型照做，又写一次
    `state_write`，然后又回到纯文本，于是再被推一次——实测 `pulp` 第 4 轮这样
    空转了 12 个来回，**69 万 input token，占整场的 84%**，靠硬上限才收住。
    催促词把做完的一步也算进去，它就成了一条把模型引回原地的指令。
    """
    llm = ScriptedLLM(
        tool("state_write", set={"地点": "站里"}),
        text("我先想想。"),  # 空手，触发催促
        tool("submit_outline", outline="他站住了。"),
    )
    ctx, tracer, registry = make_ctx(tmp_path, llm)
    await run_director(ctx, registry, tracer, UserInput(text="我推开门"))

    nudge = llm.messages_of(2)[-1]["content"]
    assert "submit_outline" in nudge
    assert "state_write" not in nudge, "已经写过了，不该再把它列进来"


async def test_the_nudge_still_asks_for_the_write_when_it_is_missing(tmp_path: Path) -> None:
    """反面：一次都还没写过，那 E0 那一步就必须提。"""
    llm = ScriptedLLM(
        text("我先想想。"),
        tool("state_write"),
        tool("submit_outline", outline="他站住了。"),
    )
    ctx, tracer, registry = make_ctx(tmp_path, llm)
    await run_director(ctx, registry, tracer, UserInput(text="我推开门"))

    nudge = llm.messages_of(1)[-1]["content"]
    assert "state_write" in nudge
