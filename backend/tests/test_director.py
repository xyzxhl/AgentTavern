"""director 的 tool loop 与上下文装配。"""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import (
    ScriptedLLM,
    option,
    special_body,
    text,
    tool,
    using,
)

from agenttavern.agents import DirectorFailed, run_director
from agenttavern.llm import Message, Role
from agenttavern.models import (
    EntryKind,
    Node,
    Session,
    StateDelta,
    UserInput,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
    replay,
)
from agenttavern.prompting import assembler, director_prompt, writer_prompt
from agenttavern.storage import Store
from agenttavern.tools import RunContext, ToolRegistry, build_tools
from agenttavern.tracing import Tracer


def make_ctx(tmp_path: Path, llm: ScriptedLLM, session: Session | None = None):
    repo = Store(tmp_path).create_session(session or Session())
    ctx = RunContext(
        run_id="run_1",
        repo=repo,
        session=repo.load_session(),
        gateway=llm.gateway(),
    )
    registry = ToolRegistry.for_session(repo.worldbook())
    return ctx, Tracer(repo, "run_1", persist=True), registry


def joined(messages: list[Message]) -> str:
    """把装配出来的一串消息拼成一整块文字。

    **内容断言用这个，位置断言不要用。** 哪一节在哪一侧、有没有出现，是内容问题；
    它是第几条消息、什么 role，是另一回事，那些直接断言消息本身（见文件末尾）。
    """
    return "\n\n".join(m.content or "" for m in messages)


def director_messages(
    state: WorldState | None = None,
    worldinfo: list[WorldbookEntry] | None = None,
) -> list[Message]:
    return director_prompt(state or WorldState(), None, worldinfo=worldinfo).messages


def prompt_for(**kwargs) -> str:
    """director 收到的全部文字。"""
    return joined(director_messages(**kwargs))


def _state_block(prompt: str) -> str:
    """从整段提示词里抠出「当前世界状态」那一节的 JSON。

    状态现在整节就是一份 JSON（S119），所以断言直接 `json.loads` 它——
    **比钉渲染出来的句子稳**：改一句措辞不该让状态测试变红。
    """
    # **装配器不再包标签**（R31），所以这里靠 JSON 自己的边界抠：状态整节
    # 就是一份 JSON，而它是提示词里唯一一大块以 `{` 开头的东西。
    return prompt[prompt.index("{\n") : prompt.rindex("}") + 1]


def writer_text() -> str:
    return joined(writer_prompt().messages)


async def test_happy_path(tmp_path: Path) -> None:
    """**一轮就完**：无工具调用的非空最终文本就是细纲（S174）。"""
    llm = ScriptedLLM(text("她推开门，屋里坐着陈。"))
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))

    assert result.outline == "她推开门，屋里坐着陈。"
    assert result.rounds == 1
    assert result.usage.tokens_in == 10


async def test_the_director_cannot_touch_state(tmp_path: Path) -> None:
    """**读得到，写不了**（S7）。

    工具面上根本没有 `state_write`——不注册比"注册了但拒绝"强（P9）。调用它
    得到的是可恢复错误，这一轮不该因此失败。
    """
    llm = ScriptedLLM(
        tool("state_write", set={"地点": "不该生效"}),
        text("她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))

    assert "state_write" not in reg.names
    assert "state_query" in reg.names
    assert ctx.staged_delta.is_empty()
    assert result.outline == "她推开门。"


async def test_text_only_reply_is_the_outline(tmp_path: Path) -> None:
    """非空文本就是细纲，不需要再复制进一个终止工具参数（S174）。"""
    llm = ScriptedLLM(text("她推开门。"))
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert result.outline == "她推开门。"
    assert len(llm.requests) == 1


async def test_an_empty_final_reply_is_pushed_back(tmp_path: Path) -> None:
    """空文本不构成细纲；loop 应明确催促一次再继续（S174）。"""
    llm = ScriptedLLM(text(""), text("她推开门。"))
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert result.outline == "她推开门。"
    assert result.rounds == 2

    nudge = llm.messages_of(1)[-1]
    assert nudge["role"] == "user"
    assert "直接输出" in nudge["content"]


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
        text("她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)

    result = await run_director(ctx, reg, tracer, UserInput(text="我推开门"))
    assert result.outline == "她推开门。"


async def test_trace_carries_readable_labels(tmp_path: Path) -> None:
    """S111：trace 必须带人类可读语义标签，UI 才渲染得出"确认了她现在在哪"。"""
    llm = ScriptedLLM(
        tool("state_query", paths=[["地点"]]),
        text("她推开门。"),
    )
    ctx, tracer, reg = make_ctx(tmp_path, llm)
    # 状态得由**前一轮**带过来——director 自己写不了了（S7）。
    ctx.repo.save_node(Node(id="run_0", state_delta=StateDelta(set={"地点": "客厅"})))
    ctx.parent_id = "run_0"

    await run_director(ctx, reg, tracer, UserInput(text="我推开门"))

    labels = [e.label for e in ctx.repo.read_trace("run_1")]
    assert "展开了状态 地点" in labels
    assert "细纲完成" in labels
    assert all(e.label for e in ctx.repo.read_trace("run_1"))


# -- 上下文装配：拓扑的地基 --------------------------------------------------


STYLE = "jishi"
"""测试里用的文风档位。**文风归控制维度**，不归 meta——它是读者偏好，不是世界属性。"""


def make_book() -> list[WorldbookEntry]:
    """整份设定就是**一串条目**（S64）：题材是 `WORLD` 条目，语言习惯在
    `CHARACTER` 条目的 `voice` 上（S65）——两样都没有第二个入口。"""
    return [
        WorldbookEntry(name="题材与基调", kind=EntryKind.WORLD, content="悬疑"),
        WorldbookEntry(name="陈", kind=EntryKind.CHARACTER, voice="说话总是绕圈子"),
    ]


def test_director_never_sees_style_or_prose(tmp_path: Path) -> None:
    """S3 的隔离基础：文风与角色语言习惯只进 writer，正文只进 writer。"""
    prompt = prompt_for(worldinfo=make_book())

    assert option("writer", "prose_style", STYLE) not in prompt
    assert "说话总是绕圈子" not in prompt
    # 「正文不进 director」现在由 test_run.py 守着——对话跨轮持续之后（S60），
    # 前情在**重放的消息**里而不在提示词里，那条断言必须看全部上下文才有意义。


def test_writer_never_sees_state_or_history(tmp_path: Path) -> None:
    """writer 只承担文风。它拿不到状态值、世界设定、用户原始指令——都已在细纲里。"""
    with using(choices={"writer": {"prose_style": STYLE}}):
        prompt = joined(writer_prompt(worldinfo=make_book()).messages)
        assert option("writer", "prose_style", STYLE) in prompt
        assert "说话总是绕圈子" in prompt
        # 题材是 director 的事——它是 `WORLD` 条目，只进 worldinfo（S64）。
        assert "悬疑" not in prompt
        assert "state" not in prompt.lower()

        # 落笔手法归 writer——它管怎么写；推进与转述归 director——它管写什么。
        #
        # 断言的是**这一段在不在**，不是它的措辞。原来这里钉的是「情感落在外在」，
        # 而那句话已经被删了：它把「不要用情绪词概括」和「一律外化成身体动作」
        # 混成了一条，后半句会压过 session 自己的 `prose_style`（`pulp` 明写
        # 「心理活动直接写出来」，产出里却一个念头都没有）。**钉措辞会让提示词改不动。**
        # **装配器不再包标签**（R31），所以钉的是那一节的头一句——
        # 仍然是「这一段在不在」，不是它的措辞。
        assert "# 人物思绪描写" in prompt


def test_repetition_guards_reach_the_responsible_agents() -> None:
    """S178：director 防剧情重复，writer 防把用于衔接的正文上文重新写一遍。"""
    director = prompt_for()
    writer = joined(writer_prompt(upstream="上一轮正文").messages)

    assert "包括开场白" in director
    assert "本轮细纲必须从最后一份正文的最后一句之后开始" in director
    assert "转述本轮顶层用户输入" in director
    assert "包括开场白" in writer
    assert "即使细纲误收了上文事件，也不要再写一次" in writer


def test_writer_receives_the_outline_endpoint_guard() -> None:
    """S181：writer 的展开自由不能变成继续规划下一幕。"""
    writer = joined(writer_prompt().messages)

    assert "细纲也规定了本轮正文的终点" in writer
    assert "完成其中明确列出的场景、动作、信息与结果后立即结束" in writer


def test_writer_receives_the_timeline_guard() -> None:
    """S183：相对时间的自洽由 writer 在渲染前守住。"""
    writer = joined(writer_prompt().messages)

    assert "# 时间连续性" in writer
    assert "当天／次日／明日" in writer
    assert "正文的开场时刻与后续推进都必须照写" in writer
    assert "相邻时段、模糊说法或另一套日期" in writer


def test_writer_receives_the_fixed_count_guard() -> None:
    """S89：列举的成员与总数不能在同一篇正文里互相打架。"""
    writer = joined(writer_prompt().messages)

    assert "当前细纲是本轮正文的施工蓝图" in writer
    assert "不要复用旧措辞" in writer
    assert "细纲提供的精确数字必须保留" in writer
    assert "数量、组成或名单" in writer
    assert "用另一个总数称呼它们" in writer
    assert "逐项清点同时出现的总数与名单" in writer


def test_state_manager_receives_the_ambiguous_time_guard() -> None:
    """S184：正文有歧义时 state_manager 不能自行选一个精确日期。"""
    from agenttavern.prompting import state_manager_prompt

    prompt = joined(state_manager_prompt(WorldState()).messages)
    assert "无法唯一确定" in prompt
    assert "不要自行选择一个精确时间" in prompt


def test_in_turn_questions_require_an_unavoidable_current_choice() -> None:
    """提问不能把普通的未来可能性变成越过本轮边界的授权。"""
    director = prompt_for()

    assert "不能留到下一轮" in director
    assert "用户已经给出可执行动作时" in director
    assert "从前提、执行过程到本轮结果" in director
    assert "为以后越界做准备" in director


def test_authority_maps_to_ask_question_eagerness() -> None:
    """S24：控制权直接反映为 ask_question 的调用积极性——越高越少问。

    **这段话现在写在那个工具的描述里，不在提示词里**（S10）：读到「这一档不要
    调用它」的地方就该是 `ask_question` 自己。控制维度没变，变的是它落在哪。
    """

    def ask_question_desc(level: str) -> str:
        with using(tool_choices={"director": {"ask_question": level}}):
            tools = {t.name: t for t in build_tools()}
            return tools["ask_question"].description

    assert "不要调用" in ask_question_desc("high")
    assert "一轮内可以多次" in ask_question_desc("low")

    # 而 director 的提示词里**不再有这一节**。
    with using(tool_choices={"director": {"ask_question": "low"}}):
        assert "一轮内可以多次" not in prompt_for()


def test_input_handling_three_ways() -> None:
    """三档各注入各的那一段，**互不串味**。

    断言的是哪一档进去了，不是它里面写了什么（S76）。以前这是两节——静态前缀
    一节讲「转述和推进分别是什么」，每轮一节讲「这一轮做哪个」——现在合成了一节，
    排在前情之后（它每轮都可能变，不能进缓存前缀）。
    """
    picks = ("narrate_and_advance", "narrate_only", "advance_only")
    for value in picks:
        with using(choices={"director": {"narration_and_advancement": value}}):
            prompt = prompt_for()
        for other in picks:
            body = option("director", "narration_and_advancement", other)
            assert body
            assert (body in prompt) is (other == value), f"{value} 这一轮混进了 {other}"


def test_speaking_rule_is_a_choice_not_a_toggle_in_code() -> None:
    """抢话是个**选项组**，选项名就是 allowed / forbidden（S76）。

    以前这是代码里的 if：allowed 返回一段、forbidden 返回另一段，YAML 那边
    看不出还有第二种可能。现在两档都写在 YAML 里，改一档不用碰 Python。

    **只有 director 侧有这一节。** writer 侧那条曾经也在，
    后来输入契约收紧成「除非细纲已经给出，否则不要自行增加新行为、新决定、
    新心理」，用户的角色被一并盖住，再单给一条就是重复。
    """
    forbidden = option("director", "speaking_rule", "forbidden")
    allowed = option("director", "speaking_rule", "allowed")

    with using(choices={"director": {"speaking_rule": "forbidden"}}):
        strict = prompt_for()
    assert forbidden and forbidden in strict
    assert not allowed or allowed not in strict

    with using(choices={"director": {"speaking_rule": "allowed"}}):
        assert forbidden not in prompt_for()


def test_state_pack_does_not_fold_strings_by_size() -> None:
    """字符串长度不决定可见性；状态只按叶端结构折叠（S33/S157）。"""
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
    packed = json.loads(_state_block(prompt))

    assert packed["地点"] == "客厅"
    assert packed["时间"] == "第三日 傍晚"
    assert packed["林.性格"] == "谨慎，话少"
    assert packed["林与陈.关系"] == "非常长的一段关系描述" * 30


def test_state_is_json_so_the_shape_needs_no_marker() -> None:
    """**形状标记只要落在值这一侧，就是可抄的。**

    此前清单渲染成 `清单：照片、信`，那个前缀是 S32 的修法——不加的话清单和
    字符串长得一模一样。**但实测（`data` 场景第 5 轮）模型把「清单：」当成了
    值的开头抄了回来**，写回一个以那四个字打头的字符串，形状照样塌。

    JSON 自带区分，一个标记都不用：`[…]` 不是 `"…"`，`3` 不是 `"3"`，
    `{}` 不是 `""`。而且它和 `state_write` 的参数是同一套写法，中间没有翻译。
    """
    state = replay(
        [
            StateDelta(
                set={"时间": "傍晚", "林.在场": True, "沈砚.灵石": 3, "沈砚.冷却": {}},
                append={"林.知情": ["照片"]},
            )
        ]
    )
    packed = json.loads(_state_block(prompt_for(state=state)))

    assert packed["林.知情"] == ["照片"], "清单还是清单"
    assert packed["沈砚.灵石"] == 3, "数字还是数字，不是 '3'"
    assert packed["林.在场"] is True, "布尔还是布尔，不是「是」"
    assert packed["沈砚.冷却"] == {}, "空字典还是空字典，不是「（无，可逐项填）」"
    assert "清单" not in json.dumps(packed, ensure_ascii=False), "不该再有任何形状标记"


# -- 能力开关对 prompt 的影响 --------------------------------------------------


def test_state_off_removes_the_state_section_and_step() -> None:
    """关掉 state_manager 这个 agent，director 就不再收到状态那一节（S80）。

    以前这是 `Capabilities.state`。现在它是 `state_manager.yaml` 顶上的
    `enabled`——**它从来就是「这个 agent 跑不跑」**，而不是一条独立的能力：
    没有 state_manager 就没人写状态，那一节自然也不必给。
    """
    with using(agents={"state_manager": False}):
        prompt = prompt_for(state=replay([StateDelta(set={"地点": "客厅"})]))

    assert "客厅" not in prompt
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

    # 按需的只进索引：名称、体量、摘要头部在，全文不在（S53）。
    assert "教团" in prompt
    assert f"{len(long_content)} 字" in prompt
    assert "第199段设定。" not in prompt
    assert "worldinfo_read" in prompt


def test_the_flow_does_not_reference_a_terminal_tool() -> None:
    """流程句只描述收口方式，不虚构一个不存在的终止工具（S174）。"""
    prompt = prompt_for()

    assert "submit_outline" not in prompt
    assert "直接输出本轮细纲" in prompt
    assert "submit_outline" not in {t.name for t in build_tools()}


def test_empty_state_renders_cleanly() -> None:
    prompt = prompt_for()
    assert "尚无记录" in prompt


def test_density_reaches_both_sides_in_opposite_directions() -> None:
    """展开密度**同时影响两侧，而两侧读的是两组不同的文本**。

    writer 读「铺陈多少」，director 读「推进几件事」——同一个控制维度的
    正反两面（S14：director 不该知道篇幅）。断言的是两组文本各自到位，
    不是它们此刻的措辞。
    """
    for value in ("sparse", "normal", "rich"):
        w = option("writer", "density", value)
        d = option("director", "event_density", value)

        # **两侧现在是两个独立的 `selected`**（S80）：代码不再保证它们一致。
        # 那既是代价也是收益——现在做得到「director 紧凑、writer 铺陈」。
        with using(choices={"writer": {"density": value}, "director": {"event_density": value}}):
            assert w and w in writer_text()
            assert d and d in prompt_for()
            # 两组文本必须不同——一样就说明有一侧被另一侧的措辞污染了。
            assert w != d
            assert w not in prompt_for()


def test_the_three_density_choices_are_independent_and_service_wide() -> None:
    """S81/S83：三种密度分别住在三个 agent 的 YAML，不落进 session。"""
    picks = {
        "director": ("event_density", "sparse"),
        "writer": ("density", "rich"),
        "goal_manager": ("pace", "normal"),
    }
    with using(choices={agent: {name: value} for agent, (name, value) in picks.items()}):
        for agent, (name, value) in picks.items():
            groups = {
                str(item.get("name")): item
                for item in assembler.load(agent).get("sections") or []
                if isinstance(item, dict) and item.get("type") == "choice"
            }
            assert groups[name]["selected"] == value

    assert not ({"event_density", "density", "pace"} & set(Session.model_fields))


def test_the_six_control_choices_live_in_their_agent_files() -> None:
    """S81：六项控制维度各有独立地址，不靠 session 或代码开关补齐。"""
    expected = {
        "director": {"narration_and_advancement", "speaking_rule", "event_density"},
        "writer": {"density"},
        "goal_manager": {"pace"},
    }
    for agent, names in expected.items():
        actual = {
            str(item.get("name"))
            for item in assembler.load(agent).get("sections") or []
            if isinstance(item, dict) and item.get("type") == "choice"
        }
        assert names <= actual
    authority = assembler.load("director")["tools"]["ask_question"]
    assert authority["selected"] in authority["options"]
    controls = set().union(*expected.values(), {"authority"})
    assert not (controls & set(Session.model_fields))


def test_writer_and_state_history_windows_are_independent_yaml_values(tmp_path: Path) -> None:
    """S119：正文上文与状态示范是两条 context 的参数，不能互相绑死。"""
    for name in assembler.PROMPT_FILES:
        shutil.copy2(assembler.path_of(name), tmp_path / f"{name}.yaml")

    def set_runs(name: str, source: str, runs: int) -> None:
        path = tmp_path / f"{name}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        item = next(section for section in data["sections"] if section.get("source") == source)
        item["runs"] = runs
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    set_runs("writer", "upstream", 1)
    set_runs("state_manager", "recent", 7)
    assembler.use_dir(tmp_path)
    try:
        assert assembler.param("writer", "upstream", "runs", 3) == 1
        assert assembler.param("state_manager", "recent", "runs", 3) == 7
    finally:
        assembler.use_dir(None)

    assert not ({"upstream_runs", "state_recent_runs"} & set(Session.model_fields))


def test_writer_receives_the_selected_speaking_rule_without_a_second_choice() -> None:
    """S91：一个服务级选项同时约束两次调用，不在 writer 里复制第二组选项。"""
    for pick in ("allowed", "forbidden"):
        selected = option("director", "speaking_rule", pick)
        other = option(
            "director", "speaking_rule", "allowed" if pick == "forbidden" else "forbidden"
        )
        with using(choices={"director": {"speaking_rule": pick}}):
            messages = writer_prompt(upstream="上一轮正文").messages
            w = joined(messages)
        assert selected and selected in w
        assert not other or other not in w
        assert sum(selected in (m.content or "") for m in messages) == 1

        upstream_index = next(i for i, m in enumerate(messages) if m.content == "上一轮正文")
        rule_index = next(i for i, m in enumerate(messages) if selected in (m.content or ""))
        assert rule_index > upstream_index, "玩家控制边界必须压在正文上文之后"

    writer_choices = {
        item.get("name")
        for item in assembler.load("writer").get("sections", [])
        if isinstance(item, dict) and item.get("type") == "choice"
    }
    assert "speaking_rule" not in writer_choices


async def test_the_nudge_only_asks_for_the_one_remaining_step(tmp_path: Path) -> None:
    """催促词只要求直接输出细纲，不引入不存在的工具或状态写入。"""
    llm = ScriptedLLM(
        text(""),  # 空响应，触发催促
        text("他站住了。"),
    )
    ctx, tracer, registry = make_ctx(tmp_path, llm)
    await run_director(ctx, registry, tracer, UserInput(text="我推开门"))

    nudge = llm.messages_of(1)[-1]["content"]
    assert "直接输出" in nudge
    assert "submit_outline" not in nudge
    assert "state_write" not in nudge, "director 根本没有这个工具，提它只会把模型带沟里"


def test_each_side_gets_its_own_sections_and_none_of_the_others() -> None:
    """**隔离的断言必须钉结构，不能钉措辞。**

    这条测试换过三版。前两版断言的是具体句子（"情感落在外在"、"只写事实，不做描写"、
    "对白写意思，不写原话"），于是每改一次提示词就红一次——而提示词是这个项目里
    改得最频繁的东西。**钉措辞的测试不保护不变量，只是把文案冻住。**

    真正的不变量是 S3：哪一节进哪一侧。写法、措辞、例子都可以随便改，
    但 writer 永远拿不到 director 的推进与转述规则，director 永远拿不到文风。
    """
    w = writer_text()
    d = prompt_for(worldinfo=make_book())

    # **顶层命名块没了**（R31：`key` 引用删掉，正文内联进 section），所以
    # 认的是每一节的头一句。仍然钉的是**结构不是措辞**：哪一节进哪一侧。
    writer_only = ["你收到的细纲描述的是", "# 人物思绪描写", "细纲中的对白不是台词草稿"]
    director_only = ["细纲不是压缩版正文", "# 人物驱动情节"]

    for head in writer_only:
        assert head in w, f"writer.yaml 的「{head}」那一节没进 writer"
        assert head not in d, f"writer.yaml 的「{head}」那一节漏进了 director"

    for head in director_only:
        assert head in d, f"director.yaml 的「{head}」那一节没进 director"
        assert head not in w, f"director.yaml 的「{head}」那一节漏进了 writer"

    narration = option("director", "narration_and_advancement", "narrate_and_advance")
    assert narration and narration in d and narration not in w


def test_style_and_prose_never_reach_the_director() -> None:
    """S3 的隔离基础，用 session 自己的配置来验——它不随提示词改动而变。"""
    d = prompt_for(worldinfo=make_book())

    assert option("writer", "prose_style", STYLE) not in d
    assert "说话总是绕圈子" not in d
    # 否定项那个字段已经整个删掉了（S91），所以这里没有它的断言——
    # 「不许写什么」现在只有两个入口：世界设定（数据）和 YAML（提示词）。


# -- 提示词组装器（S75 / S76 / S9） -----------------------------------------


def fake_yaml(monkeypatch, doc: dict) -> None:
    monkeypatch.setattr(assembler, "load", lambda name: doc)


def test_sections_order_comes_from_yaml(monkeypatch) -> None:
    """**顺序由 YAML 决定，不在代码里。** 移动条目就换了位置。"""
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"type": "text", "role": "system", "body": "第二段乙"},
                {"type": "text", "role": "system", "body": "第一段甲"},
            ]
        },
    )
    out = joined(assembler.assemble("writer").messages)
    assert out.index("乙") < out.index("甲")


def test_a_new_section_needs_only_yaml(monkeypatch) -> None:
    """新增一节**只写 YAML**，不动代码——这正是这个组装器存在的理由。"""
    fake_yaml(
        monkeypatch,
        {"sections": [{"type": "text", "role": "user", "body": "随便写点"}]},
    )
    # **装配出来的就是你写的那几个字**（R31）：装配器不再加标题标签。
    assert assembler.assemble("writer").messages == [Message(role=Role.USER, content="随便写点")]


def test_each_section_is_one_message_and_carries_its_own_role(monkeypatch) -> None:
    """**一个条目 = 一条消息，role 写在条目上**（S9）。

    以前整份提示词是一大块 system 文本，「这一段用什么 role」「角色控制规则放在上文
    之后还是之前」这类决定只能在代码里改。现在它们都在 YAML 里。
    """
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"type": "text", "role": "system", "body": "你是谁"},
                {"type": "text", "role": "user", "body": "这一轮的输入"},
                {"type": "text", "role": "assistant", "body": "好的"},
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
    fake_yaml(monkeypatch, {"sections": [{"type": "text", "body": "没写 role"}]})
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
                {"type": "text", "role": "system", "body": "在"},
                {"type": "context", "role": "system", "source": "nothing"},
            ]
        },
    )
    assert assembler.assemble("writer", contexts={"nothing": "  "}).messages == [
        Message(role=Role.SYSTEM, content="在")
    ]


def test_three_kinds_of_content_are_declared_separately(monkeypatch) -> None:
    """一节的内容**只有三类，而且类型是显式字段**（S76/R31）。

    `type` 写出来而不是靠「出现了哪个键」去猜——拖动排序的界面要先读出这一条
    是什么类型才画得出来。三类之外的都删掉了（`key` / `title` / `intro` / `outro`）。

    **`selected` 住在条目自己身上**：以前「有哪些档」在 YAML、「选哪一档」
    由代码通过 `options` 参数传进来，改一档文风要动两个地方。
    """
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"type": "context", "role": "system", "source": "worldinfo"},
                {"type": "text", "role": "system", "body": "视角：{{pov}}"},
                {
                    "type": "choice",
                    "role": "system",
                    "name": "density",
                    "selected": "rich",
                    "options": {"sparse": "少写点", "rich": "多写点"},
                },
            ],
        },
    )
    out = joined(
        assembler.assemble(
            "writer",
            contexts={"worldinfo": "### 教团\n信奉旧神。"},
            values={"pov": "第三人称限知"},
        ).messages
    )
    assert "### 教团" in out
    assert "视角：第三人称限知" in out
    assert "多写点" in out
    assert "少写点" not in out


def test_a_context_can_be_a_run_of_messages(monkeypatch) -> None:
    """前情是唯一一个**一条顶多条**的 context——它本来就是对话本身。

    摘要、历史轮次各自带着自己的 role，压不成一条；所以这种条目上
    **不写 role**，写了也不生效，那就直接报错（同 P29）。
    """
    history = [
        Message(role=Role.USER, content="上一轮的输入"),
        Message(role=Role.ASSISTANT, content="上一轮的细纲"),
    ]
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"type": "context", "source": "history"},
                {"type": "text", "role": "user", "body": "现在"},
            ]
        },
    )
    prompt = assembler.assemble("writer", contexts={"history": history})
    assert prompt.messages[:2] == history
    assert prompt.starts["history"] == 0

    fake_yaml(
        monkeypatch,
        {"sections": [{"type": "context", "role": "user", "source": "history"}]},
    )
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
                {"type": "text", "role": "system", "body": "题材：{{genre}}\n视角：{{pov}}"},
                {"type": "text", "role": "system", "body": "本次目标：{{goal}}。"},
            ]
        },
    )
    prompt = assembler.assemble("writer", values={"genre": "悬疑", "pov": "", "goal": ""})
    assert joined(prompt.messages) == "题材：悬疑"


def test_injected_context_is_not_parsed_as_a_template(monkeypatch) -> None:
    """**注入进来的是数据，不是模板。**

    用户的世界设定里完全可能出现 `{{` ——按模板解析它，轻则报错，
    重则把用户的设定当成变量名吞掉。
    """
    fake_yaml(
        monkeypatch,
        {"sections": [{"type": "context", "role": "system", "source": "worldinfo"}]},
    )
    prompt = assembler.assemble("writer", contexts={"worldinfo": "{{不是变量}}"})
    assert prompt.messages[0].content == "{{不是变量}}"


def test_names_that_do_not_line_up_raise(monkeypatch) -> None:
    """名字对不上**直接报错**，不静默跳过。

    静默跳过会让一整节悄悄消失，而产出看起来只是“变差了一点”——
    那是最难查的一类问题（同 P29：没生效的东西不会报错）。
    """
    cases: list[dict] = [
        {"sections": [{"type": "context", "role": "system", "source": "根本没有这个"}]},
        {"sections": [{"type": "text", "role": "system", "body": "{{没人给值}}"}]},
        {
            "sections": [
                {
                    "type": "choice",
                    "role": "system",
                    "name": "density",
                    "selected": "根本没这一档",
                    "options": {"rich": "多写点"},
                }
            ]
        },
        {"sections": [{"role": "system", "body": "没写 type"}]},
    ]
    for doc in cases:
        fake_yaml(monkeypatch, doc)
        with pytest.raises(KeyError):
            assembler.assemble("writer")


def test_the_turn_starts_after_the_history(monkeypatch) -> None:
    """`starts` 是给调用方定位用的：**本轮从哪一条开始**。

    director 靠它把这一轮新增的消息单独存进节点（S60 存增量不存快照）。
    草稿（S48）跟着前情一起进那个位置，所以起点要再往前挪 `len(prior)`——
    续跑的那几条属于**同一个节点**，不是新的一轮。
    """
    prior = [Message(role=Role.USER, content="上一轮")]
    carried = [Message(role=Role.ASSISTANT, content="被打断那半轮")]
    fake_yaml(
        monkeypatch,
        {
            "sections": [
                {"type": "text", "role": "system", "body": "前缀"},
                {"type": "context", "source": "history"},
                {"type": "text", "role": "user", "body": "本轮输入"},
            ]
        },
    )
    prompt = assembler.assemble("writer", contexts={"history": [*prior, *carried]})
    assert prompt.starts["history"] == 1
    assert prompt.messages[prompt.starts["history"] + len(prior) :] == [
        *carried,
        Message(role=Role.USER, content="本轮输入"),
    ]


# -- 追加输入：说明之后再追加（S119） -----------------------------------------


def test_an_appended_input_is_wrapped_with_an_explanation() -> None:
    """引导 / 打断插进来的那条**不能裸着塞**。

    裸文本塞进对话，模型读到的是一条突然冒出来的用户发言，看不出它和这一轮
    已经进行到一半的推演是什么关系。措辞在 `director.yaml` 的 `specials.append`
    里（R31：**一份文件 = 一个 agent 收到的全部文字**），不写死在 Python 里。
    """
    from agenttavern.agents.replay import append_message, interjection_mark

    message = append_message("等等，先别问他")

    assert message.role is Role.USER
    assert "等等，先别问他" in (message.content or "")
    # **记号由用户自己写在 body 里，`mark` 声明它是哪一串**（R31：装配器不再
    # 自动包标签）。两处对不上就当场报错——对不上的后果是插话在重放时被当成
    # 编排层的催促悄悄丢掉（同 P29）。
    assert interjection_mark() in (message.content or "")


def test_the_attached_prompts_live_with_their_agent() -> None:
    """**片段文件没了**（R31）：追加输入并进了 `director.yaml` 的 `specials`。

    它们不在 director 的消息序列里，是**运行时另外追加的整条消息**——但那不是
    「另一份文件」的理由。判据是 S3：**一份文件 = 一个 agent 收到的全部文字**，
    而这几条同样是它收到的字。顺带删掉了装配器里「顶层直接是列表」那条特例，
    它本来就只有这一个用户。
    """
    specials = assembler.load("director")["specials"]
    assert {"append", "nudge", "nudge_final", "converge", "segment", "no_input"} <= set(specials)
    assert special_body("director", "nudge"), "空回催促的措辞得在 YAML 里"
    # 归段追问以前是 `postrun.py` 里一段四百多字的 Python 字符串——**模型每轮
    # 都会读到它，而它从界面完全够不着**（R31 那次审计新发现的）。正文归用户，
    # 所以这里只钉代码要填的变量与工具接线，不钉某一句怎么说。
    segment = special_body("director", "segment")
    assert "{{open_runs}}" in segment
    assert "{{listing}}" in segment
    assert "close_segment" in segment


def test_the_tool_list_is_not_a_message() -> None:
    """**工具清单不进消息**（S11）。

    它走 API 请求的 `tools` 字段，模型本来就看得见。再放一条「可用的工具」的
    消息，是同一份清单发了两遍——多花一份体量，还多一处会和真工具面对不上的地方。

    这条容易反复：那一节当初是有意加的（S10，为了让「有哪些工具」在提示词的顺序
    里看得见），加的时候没意识到工具根本不在 messages 里。
    """
    prompt = prompt_for()
    for name in ToolRegistry.for_session().names:
        assert name not in prompt, f"{name} 不该出现在消息里"

    assert "可用的工具" not in prompt


# -- 角色设定（S65） -----------------------------------------------------------


def test_the_player_is_a_character_not_a_fifth_kind() -> None:
    """**用户扮演的角色是一般角色的子类**（S65）。

    凡是对角色成立的（正文、语言习惯、可展开），对它一样成立；它只是多回答一个
    「用户是谁」。做成独立的一类，就得在每一处 `CHARACTER` 分支旁边记得再写一个
    `PLAYER`——那种「记得」迟早会漏。
    """
    from agenttavern.models import EntryKind, WorldbookEntry
    from agenttavern.prompting.prompts import player_character, render_voices

    book = [
        WorldbookEntry(name="林昭", kind=EntryKind.CHARACTER, is_player=True, voice="话少，先观察"),
        WorldbookEntry(name="陈屿", kind=EntryKind.CHARACTER, voice="说话绕，很少给准话"),
        WorldbookEntry(name="旧货店", kind=EntryKind.WORLD, content="窄巷尽头。"),
    ]

    assert player_character(book) == "林昭"
    # 用户扮演的那个也是角色，**语言习惯照样进 writer**——漏掉它的话，
    # 用户的角色会是全场唯一一个不按设定说话的人。
    voices = render_voices(book)
    assert "林昭：话少，先观察" in voices
    assert "陈屿：说话绕，很少给准话" in voices
    # 世界设定没有语言习惯这回事。
    assert "旧货店" not in voices


def test_voices_have_exactly_one_source(monkeypatch) -> None:
    """**语言习惯只有一个入口：角色条目上的 `voice`**（S65）。

    以前还有一个 `名字 → 说话方式` 的字典，靠名字和条目对上。两个入口的代价不是
    多写一份，是**它们迟早对不上，而对不上的时候不报错**——只是那个角色忽然不再
    那样说话。字典已经删了；这条钉的是"没有第二个来源"这件事本身：整份 worldbook
    里没有 `voice` 就渲染不出任何东西，不管别处写了什么。
    """
    from agenttavern.models import EntryKind, WorldbookEntry
    from agenttavern.prompting.prompts import render_voices

    assert render_voices([WorldbookEntry(name="陈屿", kind=EntryKind.CHARACTER)]) == ""
    assert render_voices([WorldbookEntry(name="旧货店", voice="不该被读到")]) == "", (
        "只有角色条目有语言习惯这回事"
    )
    from agenttavern.models import Worldbook

    assert not hasattr(Worldbook(), "meta"), "worldbook 上没有字段了（S64）"
    assert set(Worldbook.model_fields) == {"id", "name"}
