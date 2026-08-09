"""director 的 tool loop 与上下文装配。"""

from pathlib import Path

import pytest
from conftest import ScriptedLLM, text, tool, tools

from agenttavern.agents import DirectorFailed, prompt_text, run_director
from agenttavern.agents.prompt_text import text as _real_text
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


def test_user_actions_are_attempts_not_accomplished_facts() -> None:
    """**转述规则与世界规则会正面冲突**，必须明说谁让谁（D104）。

    实测：数据化世界里用户写「我再来一记御剑斩」，而御剑斩还在冷却，
    否定项又明写着"不要让冷却中的法术生效"。两条规则都成立，指向相反。

    模型当时选对了（写成催动失败），但**设计里没有任何地方这么规定过**——
    它是蒙对的。规则定为：动作是锚点，结果归世界；落空要写得可见、有据。
    """
    d = prompt_for()

    assert "用户写出的是他的角色做了什么，不是这件事成没成" in d
    assert "结果由世界决定" in d
    # 只说"可以失败"不够——不写出失败的过程，就退化成了偷偷换动作
    assert "写清用户的角色试过了，以及为什么没成" in d


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


# -- 情色指导与破限：YAML 槽位，默认空 -----------------------------------------


def test_empty_optional_injects_no_section(monkeypatch) -> None:
    """**空则整节不注入**：连标题都不出现。

    断言的是**机制**，不是 `optional.yaml` 里当前恰好填了什么——那份文件是
    部署方的内容，随时会变（也确实变过）。**钉出厂内容的测试保护不了行为，
    只会在别人填内容时红。**
    """
    # 打 `prompt_text.optional`：装配器在同一个模块里按名字取它，
    # 所以替换模块全局就够（D135 之后 `prompts.py` 已经不直接引用它了）。
    monkeypatch.setattr(prompt_text, "optional", lambda side, key: "")

    meta = make_meta()
    for prompt in (writer_system_prompt(meta, ControlDimensions()), prompt_for(meta=meta)):
        assert "## 情色与身体" not in prompt


def test_nsfw_and_jailbreak_reach_both_agents(monkeypatch) -> None:
    """**两侧都要**（同 D35 的理由）。

    director 决定这一轮发生什么，writer 决定怎么落笔；只给一侧，
    另一侧会按自己的默认来，两边对不上。破限则是因为 director 与 writer
    是两次独立的模型调用——只解开一半等于没解开。
    """
    filled = {"nsfw": "情色指导占位", "jailbreak": "破限占位"}
    monkeypatch.setattr(prompt_text, "optional", lambda side, key: filled.get(key, ""))

    meta = make_meta()
    w = writer_system_prompt(meta, ControlDimensions())
    d = director_static_prefix(meta, ControlDimensions(), capabilities=ALL_ON, worldinfo=[])

    for prompt in (w, d):
        assert "情色指导占位" in prompt
        # 位置是约定的一部分：破限在**系统提示词的最后一段**。
        #
        # 与 SillyTavern 的约定有一处不同：那边放在整个请求的最后一条消息
        # （历史之后），取最高的近因权重。我们放系统提示词末尾，因为对话跨轮
        # 持续（D114），system 是缓存前缀，放这里只付一次钱；而且 writer 那侧
        # 最后一条消息是细纲，放那儿两侧的装配方式就不一致了。
        # **哪个更有效没有实测过**，这里先取一致性。
        assert prompt.rstrip().endswith("破限占位")


def test_each_side_gets_its_own_sections_and_none_of_the_others() -> None:
    """**隔离的断言必须钉结构，不能钉措辞。**

    这条测试换过三版。前两版断言的是具体句子（"情感落在外在"、"只写事实，不做描写"、
    "对白写意思，不写原话"），于是每改一次提示词就红一次——而提示词是这个项目里
    改得最频繁的东西。**钉措辞的测试不保护不变量，只是把文案冻住。**

    真正的不变量是 D33：哪一节进哪一侧。写法、措辞、例子都可以随便改，
    但 writer 永远拿不到 director 的推进与转述规则，director 永远拿不到文风。
    """
    meta = make_meta()
    w = writer_system_prompt(meta, ControlDimensions())
    d = prompt_for(meta=meta)

    writer_only = ["voice", "craft", "dialogue", "anti_formula", "pacing"]
    director_only = ["facts_not_prose", "narration", "advancement", "boundaries"]

    for key in writer_only:
        body = _real_text("writer", key)
        assert body and body in w, f"writer.yaml 的 {key} 没进 writer"
        assert body not in d, f"writer.yaml 的 {key} 漏进了 director"

    for key in director_only:
        body = _real_text("director", key)
        assert body and body in d, f"director.yaml 的 {key} 没进 director"
        assert body not in w, f"director.yaml 的 {key} 漏进了 writer"


def test_style_and_prose_never_reach_the_director() -> None:
    """D33 的隔离基础，用 session 自己的配置来验——它不随提示词改动而变。"""
    meta = make_meta()
    d = prompt_for(meta=meta)

    assert meta.prose_style not in d
    assert "说话总是绕圈子" not in d
    # 否定项是唯一两边都给的（D35）
    assert "不要提前揭示照片的来源" in d


def test_missing_optional_file_is_not_an_error(monkeypatch, tmp_path: Path) -> None:
    """`optional.yaml` **不存在时系统照常跑**。

    这与主文件缺键必须报错是故意相反的：主文件少一个键说明装配和文案脱节了，
    那是 bug；而这份文件本来就可能被 `.gitignore` 掉、或者部署方根本没建过，
    **“没有”是它的正常状态之一**。
    """
    from agenttavern.agents import prompt_text

    prompt_text._optional.cache_clear()
    monkeypatch.setattr(prompt_text, "_DIR", tmp_path)
    try:
        assert prompt_text.optional("writer", "jailbreak") == ""
        assert prompt_text.optional("director", "nsfw") == ""
    finally:
        prompt_text._optional.cache_clear()


# -- 可编排的装配器（D135） -----------------------------------------------------


def test_sections_order_comes_from_yaml(monkeypatch) -> None:
    """**顺序由 YAML 决定，不在代码里。** 移动条目就换了位置。"""
    monkeypatch.setattr(
        prompt_text,
        "load",
        lambda name: {
            "sections": [
                {"title": "乙", "text": "第二段"},
                {"title": "甲", "text": "第一段"},
            ]
        },
    )
    out = prompt_text.assemble("writer", {})
    assert out.index("乙") < out.index("甲")


def test_a_new_section_needs_only_yaml(monkeypatch) -> None:
    """新增一节**只写 YAML**，不动代码——这正是这个装配器存在的理由。"""
    monkeypatch.setattr(
        prompt_text,
        "load",
        lambda name: {"sections": [{"title": "新加的", "text": "随便写点什么"}]},
    )
    assert prompt_text.assemble("writer", {}) == "## 新加的\n随便写点什么"


def test_empty_section_is_skipped_title_and_all(monkeypatch) -> None:
    """正文为空的条目**整条跳过**，标题也不出现。

    空标题白占体量，还会让模型以为这里本该有内容。
    """
    monkeypatch.setattr(
        prompt_text,
        "load",
        lambda name: {
            "sections": [
                {"title": "有内容", "text": "在"},
                {"title": "空的", "slot": "nothing"},
            ]
        },
    )
    assert prompt_text.assemble("writer", {"nothing": "  "}) == "## 有内容\n在"


def test_unknown_slot_raises(monkeypatch) -> None:
    """未知 slot **直接报错**，不静默跳过。

    静默跳过会让一整节悄悄消失，而产出看起来只是“变差了一点”——
    那是最难查的一类问题（同 D125：没生效的东西不会报错）。
    """
    monkeypatch.setattr(prompt_text, "load", lambda name: {"sections": [{"slot": "根本没有这个"}]})
    with pytest.raises(KeyError):
        prompt_text.assemble("writer", {})
