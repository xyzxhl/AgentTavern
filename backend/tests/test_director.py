"""director 的 tool loop 与上下文装配。"""

from pathlib import Path

import pytest
from conftest import ScriptedLLM, text, tool, tools

from agenttavern.agents import DirectorFailed, run_director
from agenttavern.llm import Message, Role
from agenttavern.models import (
    Capabilities,
    ControlDimensions,
    ExpansionDensity,
    InputHandling,
    ModelAuthority,
    Session,
    SessionMeta,
    StateDelta,
    UserInput,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
    replay,
)
from agenttavern.prompting import assembler, director_prompt, writer_prompt
from agenttavern.prompting.assembler import choice as _real_choice
from agenttavern.prompting.assembler import text as _real_text
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


def joined(messages: list[Message]) -> str:
    """把装配出来的一串消息拼成一整块文字。

    **内容断言用这个，位置断言不要用。** 哪一节在哪一侧、有没有出现，是内容问题；
    它是第几条消息、什么 role，是另一回事，那些直接断言消息本身（见文件末尾）。
    """
    return "\n\n".join(m.content or "" for m in messages)


def director_messages(
    meta: SessionMeta | None = None,
    control: ControlDimensions | None = None,
    state: WorldState | None = None,
    capabilities: Capabilities | None = None,
    worldinfo: list[WorldbookEntry] | None = None,
) -> list[Message]:
    return director_prompt(
        meta or SessionMeta(),
        control or ControlDimensions(),
        state or WorldState(),
        None,
        capabilities=capabilities or ALL_ON,
        worldinfo=worldinfo,
    ).messages


def prompt_for(**kwargs) -> str:
    """director 收到的全部文字。"""
    return joined(director_messages(**kwargs))


def writer_text(meta: SessionMeta, control: ControlDimensions) -> str:
    return joined(writer_prompt(meta, control).messages)


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
    prompt = writer_text(make_meta(), ControlDimensions())

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

    # **不转述时整节不注入**——省下的是每 run 都要付的 token。
    # 断言的是这一节在不在，不是它里面写了什么（D136）。
    narration = _real_choice("director", "narration", "narrate_only")
    assert narration and narration in only
    assert "## 怎么处理用户输入" not in advance


def test_speaking_rule_is_a_choice_not_a_toggle_in_code() -> None:
    """抢话在两侧都是**选项组**，选项名就是 allowed / forbidden（D136）。

    以前这是代码里的 if：allowed 返回一段、forbidden 返回另一段，YAML 那边
    看不出还有第二种可能。现在两档都写在 YAML 里，改一档不用碰 Python。
    """
    for side, prompt_of in (
        ("director", lambda c: prompt_for(control=c)),
        ("writer", lambda c: writer_text(make_meta(), c)),
    ):
        forbidden = _real_choice(side, "speaking_rule", "forbidden")
        allowed = _real_choice(side, "speaking_rule", "allowed")

        strict = prompt_of(ControlDimensions(allow_speaking_for_user=False))
        assert forbidden and forbidden in strict
        assert not allowed or allowed not in strict

        loose = prompt_of(ControlDimensions(allow_speaking_for_user=True))
        assert forbidden not in loose


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
    """展开密度**同时影响两侧，而两侧读的是两组不同的文本**。

    writer 读「铺陈多少」，director 读「推进几件事」——同一个控制维度的
    正反两面（D93：director 不该知道篇幅）。断言的是两组文本各自到位，
    不是它们此刻的措辞。
    """
    for value in ("sparse", "normal", "rich"):
        control = ControlDimensions(expansion_density=ExpansionDensity(value))
        w = _real_choice("writer", "density", value)
        d = _real_choice("director", "event_density", value)

        assert w and w in writer_text(SessionMeta(), control)
        assert d and d in prompt_for(control=control)
        # 两组文本必须不同——一样就说明有一侧被另一侧的措辞污染了。
        assert w != d
        assert w not in prompt_for(control=control)


def test_the_writer_side_speaking_rule_disappears_when_allowed() -> None:
    """允许抢话时这一节**整体消失**，而不是留个空标题。

    writer 侧这条规则本身是 D35 的实例（否定项两边都要）：writer 只做照抄时
    够不着这条线，一旦它真的在展开，就会顺手把用户角色往前推一步。
    """
    loose = ControlDimensions(allow_speaking_for_user=True)
    assert "## 边界" not in writer_text(make_meta(), loose)


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


def test_nsfw_defaults_to_empty_and_injects_nothing() -> None:
    """情色指导**出厂为空**，空则整节不注入——连标题都不出现。

    这条钉的是「不预设内容」这个决定本身：它是部署方按自己的模型填的东西，
    我们在仓库里写任何示例都会被当成推荐值。
    """
    for side in ("writer", "director"):
        assert _real_text(side, "nsfw") == ""

    meta = make_meta()
    for prompt in (writer_text(meta, ControlDimensions()), prompt_for(meta=meta)):
        assert "## 情色与身体" not in prompt


def test_nsfw_and_jailbreak_reach_both_agents(monkeypatch) -> None:
    """**两侧都要**（同 D35 的理由）。

    director 决定这一轮发生什么，writer 决定怎么落笔；只给一侧，
    另一侧会按自己的默认来，两边对不上。破限则是因为 director 与 writer
    是两次独立的模型调用——只解开一半等于没解开。
    """
    real = assembler.load
    filled = {"nsfw": "情色指导占位", "jailbreak": "破限占位"}
    monkeypatch.setattr(assembler, "load", lambda name: {**real(name), **filled})

    meta = make_meta()
    both = (writer_prompt(meta, ControlDimensions()).messages, director_messages(meta=meta))

    for messages in both:
        assert "情色指导占位" in joined(messages)
        # 位置是约定的一部分：破限是**最后一条 system 消息**。
        #
        # 与 SillyTavern 的约定有一处不同：那边压在整个请求的最后（历史之后），
        # 取最高的近因权重。我们放在系统消息的末尾，因为对话跨轮持续（D114），
        # 那一段是缓存前缀，放这里只付一次钱。**哪个更有效没有实测过。**
        # 想换成 ST 的做法，把 YAML 里那一条移到 history 之后即可——
        # 这正是 D137 把 role 写进条目要换来的东西。
        system = [m for m in messages if m.role is Role.SYSTEM]
        assert system[-1].content == "破限占位"


def test_each_side_gets_its_own_sections_and_none_of_the_others() -> None:
    """**隔离的断言必须钉结构，不能钉措辞。**

    这条测试换过三版。前两版断言的是具体句子（"情感落在外在"、"只写事实，不做描写"、
    "对白写意思，不写原话"），于是每改一次提示词就红一次——而提示词是这个项目里
    改得最频繁的东西。**钉措辞的测试不保护不变量，只是把文案冻住。**

    真正的不变量是 D33：哪一节进哪一侧。写法、措辞、例子都可以随便改，
    但 writer 永远拿不到 director 的推进与转述规则，director 永远拿不到文风。
    """
    meta = make_meta()
    w = writer_text(meta, ControlDimensions())
    d = prompt_for(meta=meta)

    writer_only = ["voice", "craft", "dialogue", "anti_formula"]
    director_only = ["facts_not_prose", "advancement", "boundaries"]

    for key in writer_only:
        body = _real_text("writer", key)
        assert body and body in w, f"writer.yaml 的 {key} 没进 writer"
        assert body not in d, f"writer.yaml 的 {key} 漏进了 director"

    for key in director_only:
        body = _real_text("director", key)
        assert body and body in d, f"director.yaml 的 {key} 没进 director"
        assert body not in w, f"director.yaml 的 {key} 漏进了 writer"

    narration = _real_choice("director", "narration", "narrate_and_advance")
    assert narration and narration in d and narration not in w


def test_style_and_prose_never_reach_the_director() -> None:
    """D33 的隔离基础，用 session 自己的配置来验——它不随提示词改动而变。"""
    meta = make_meta()
    d = prompt_for(meta=meta)

    assert meta.prose_style not in d
    assert "说话总是绕圈子" not in d
    # 否定项是唯一两边都给的（D35）
    assert "不要提前揭示照片的来源" in d


# -- 提示词组装器（D135 / D136 / D137） -----------------------------------------


def fake_yaml(monkeypatch, doc: dict) -> None:
    monkeypatch.setattr(assembler, "load", lambda name: doc)


def test_sections_order_comes_from_yaml(monkeypatch) -> None:
    """**顺序由 YAML 决定，不在代码里。** 移动条目就换了位置。"""
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"role": "system", "title": "乙", "text": "第二段"},
                {"role": "system", "title": "甲", "text": "第一段"},
            ]
        },
    )
    out = joined(assembler.assemble("writer").messages)
    assert out.index("乙") < out.index("甲")


def test_a_new_section_needs_only_yaml(monkeypatch) -> None:
    """新增一节**只写 YAML**，不动代码——这正是这个组装器存在的理由。"""
    fake_yaml(monkeypatch, {"sections": [{"role": "user", "title": "新加的", "text": "随便写点"}]})
    assert assembler.assemble("writer").messages == [
        Message(role=Role.USER, content="## 新加的\n随便写点")
    ]


def test_each_section_is_one_message_and_carries_its_own_role(monkeypatch) -> None:
    """**一个条目 = 一条消息，role 写在条目上**（D137）。

    以前整份提示词是一大块 system 文本，「这一段用什么 role」「破限压在历史
    之后还是之前」这类决定只能在代码里改。现在它们都在 YAML 里。
    """
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"role": "system", "text": "你是谁"},
                {"role": "user", "text": "这一轮的输入"},
                {"role": "assistant", "text": "好的"},
            ]
        },
    )
    assert [m.role for m in assembler.assemble("writer").messages] == [
        Role.SYSTEM,
        Role.USER,
        Role.ASSISTANT,
    ]


def test_a_section_without_a_role_raises(monkeypatch) -> None:
    """**role 必须写出来。** 默认成 system 会让人忘了这是个决定。"""
    fake_yaml(monkeypatch, {"sections": [{"text": "没写 role"}]})
    with pytest.raises(KeyError):
        assembler.assemble("writer")


def test_empty_section_is_skipped_message_and_all(monkeypatch) -> None:
    """正文为空的条目**整条跳过**，标题、这条消息都不出现。

    空消息白占体量，还会让模型以为这里本该有内容。
    """
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"role": "system", "title": "有内容", "text": "在"},
                {"role": "system", "title": "空的", "context": "nothing"},
            ]
        },
    )
    assert assembler.assemble("writer", contexts={"nothing": "  "}).messages == [
        Message(role=Role.SYSTEM, content="## 有内容\n在")
    ]


def test_three_kinds_of_content_are_declared_separately(monkeypatch) -> None:
    """一节的内容有三种来源，YAML 里**分别声明**（D136）。

    看一眼 sections 就知道这一节是系统控制的 context、模板替换，还是按选项切换——
    以前它们全叫 `slot`，从 YAML 那侧分不出来。
    """
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"role": "system", "title": "系统控制的 context", "context": "worldinfo"},
                {"role": "system", "title": "模板替换", "text": "视角：{{pov}}"},
                {"role": "system", "title": "选择控制", "choice": "density"},
            ],
            "density": {"sparse": "少写点", "rich": "多写点"},
        },
    )
    out = joined(
        assembler.assemble(
            "writer",
            contexts={"worldinfo": "### 教团\n信奉旧神。"},
            values={"pov": "第三人称限知"},
            options={"density": "rich"},
        ).messages
    )
    assert "### 教团" in out
    assert "视角：第三人称限知" in out
    assert "多写点" in out
    assert "少写点" not in out


def test_a_context_can_be_a_run_of_messages(monkeypatch) -> None:
    """前情是唯一一个**一条顶多条**的 context——它本来就是对话本身。

    摘要、历史轮次各自带着自己的 role，压不成一条；所以这种条目上
    **不写 role**，写了也不生效，那就直接报错（同 D125）。
    """
    history = [
        Message(role=Role.USER, content="上一轮的输入"),
        Message(role=Role.ASSISTANT, content="上一轮的细纲"),
    ]
    fake_yaml(monkeypatch, {"sections": [{"context": "history"}, {"role": "user", "text": "现在"}]})
    prompt = assembler.assemble("writer", contexts={"history": history})
    assert prompt.messages[:2] == history
    assert prompt.starts["history"] == 0

    fake_yaml(monkeypatch, {"sections": [{"role": "user", "context": "history"}]})
    with pytest.raises(KeyError):
        assembler.assemble("writer", contexts={"history": history})


def test_an_empty_variable_takes_its_line_with_it(monkeypatch) -> None:
    """变量为空 → **整行删掉**；一行不剩 → 整节不出现。

    不这么做就会留下「视角：」这种半截的行，比没有更糟：
    模型会以为这里本该有内容而自己补一个。
    """
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"role": "system", "title": "设定", "text": "题材：{{genre}}\n视角：{{pov}}"},
                {"role": "system", "title": "目标", "text": "本次目标：{{goal}}。"},
            ]
        },
    )
    prompt = assembler.assemble("writer", values={"genre": "悬疑", "pov": "", "goal": ""})
    assert joined(prompt.messages) == "## 设定\n题材：悬疑"


def test_injected_context_is_not_parsed_as_a_template(monkeypatch) -> None:
    """**注入进来的是数据，不是模板。**

    用户的世界设定里完全可能出现 `{{` ——按模板解析它，轻则报错，
    重则把用户的设定当成变量名吞掉。
    """
    fake_yaml(monkeypatch, {"sections": [{"role": "system", "context": "worldinfo"}]})
    prompt = assembler.assemble("writer", contexts={"worldinfo": "{{不是变量}}"})
    assert prompt.messages[0].content == "{{不是变量}}"


def test_names_that_do_not_line_up_raise(monkeypatch) -> None:
    """名字对不上**直接报错**，不静默跳过。

    静默跳过会让一整节悄悄消失，而产出看起来只是“变差了一点”——
    那是最难查的一类问题（同 D125：没生效的东西不会报错）。
    """
    cases: list[dict] = [
        {"sections": [{"role": "system", "context": "根本没有这个"}]},
        {"sections": [{"role": "system", "text": "{{没人给值}}"}]},
        {"sections": [{"role": "system", "choice": "density"}], "density": {"rich": "多写点"}},
    ]
    for doc in cases:
        fake_yaml(monkeypatch, doc)
        with pytest.raises(KeyError):
            assembler.assemble("writer")


def test_the_turn_starts_after_the_history(monkeypatch) -> None:
    """`starts` 是给调用方定位用的：**本轮从哪一条开始**。

    director 靠它把这一轮新增的消息单独存进节点（D114 存增量不存快照）。
    草稿（D118）跟着前情一起进那个位置，所以起点要再往前挪 `len(prior)`——
    续跑的那几条属于**同一个节点**，不是新的一轮。
    """
    prior = [Message(role=Role.USER, content="上一轮")]
    carried = [Message(role=Role.ASSISTANT, content="被打断那半轮")]
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"role": "system", "text": "前缀"},
                {"context": "history"},
                {"role": "user", "text": "本轮输入"},
            ]
        },
    )
    prompt = assembler.assemble("writer", contexts={"history": [*prior, *carried]})
    assert prompt.starts["history"] == 1
    assert prompt.messages[prompt.starts["history"] + len(prior) :] == [
        *carried,
        Message(role=Role.USER, content="本轮输入"),
    ]
