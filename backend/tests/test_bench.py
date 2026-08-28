"""采集与归因工具（docs/Optimization.md §4 的第 1、3、4 步）。"""

from pathlib import Path

from conftest import ScriptedLLM, option, text, tool, tools

from agenttavern.bench import SCENARIOS, Attribution, Variant, Verdict, grade_turn, run_scenario
from agenttavern.bench.grader import parse
from agenttavern.bench.report import attribution_table, render
from agenttavern.bench.scenarios import Scenario
from agenttavern.config import Settings
from agenttavern.models import InputSource, StateDelta
from agenttavern.models.state import paths_of, walk


def one_run(outline: str = "她推开门。", prose: str = "门轴响了。"):
    """一轮的两次模型调用。状态那一次由 `ScriptedLLM(state=...)` 应答（S7）。"""
    return [tool("submit_outline", outline=outline), text(prose)]


# -- 回归集 ------------------------------------------------------------------


def test_scenarios_are_deliberately_opposite() -> None:
    """回归集必须包含一个**约束性质相反**的世界。

    只在一边成立的规则是那个场景的经验，不是规则——`remove` 的缺口（S32）与
    "存续时长不是判据"（S34）都是被数据化世界揭穿的，两条在悬疑场景上都看着正常。
    """
    noir, data = SCENARIOS["noir"], SCENARIOS["data"]

    # noir 的初始状态里没有可增减的量；data 有清单、数字和冷却。
    # **看的是叶子**——初始状态是嵌套的，顶层往往只是个角色名（S31）。
    def leaves(scenario) -> list[object]:
        out = []
        for entry in scenario.worldbook:
            for path in paths_of(entry.values):
                out.append(walk(entry.values, path))
        return out

    assert not any(isinstance(v, int | float) and not isinstance(v, bool) for v in leaves(noir))
    assert any(isinstance(v, int) and not isinstance(v, bool) for v in leaves(data))
    assert any(isinstance(v, list) for v in leaves(data))


def test_every_scenario_has_a_recorded_input_sequence() -> None:
    """回归集固定**输入**，不固定期望输出——创作输出不确定，
    把某一次生成当标准答案是错的（§6）。

    **goal 驱动的场景是例外**：除了种子那一条，输入由 goal_manager 每轮现写
    （S92）。它固定的是**目标**——那才是那种场景要复现的东西。
    """
    for name, scenario in SCENARIOS.items():
        assert scenario.worldbook
        if scenario.make_session().meta.goal is not None:
            assert scenario.inputs, f"{name} 至少要有一条种子输入"
        else:
            assert len(scenario.inputs) >= 3, name


def test_scenarios_span_two_ends_of_prose_style() -> None:
    """两个克制文学向的场景**分不开**"writer 在跟文风走"和"writer 一直用同一副
    默认腔调"——它们产出的正文节奏本来就该相近。

    因此回归集里必须有一个文风反差最大的场景。它同时是"表达自然、
    达到主流网文水平"这条目标的量尺：拿克制文学向的样本去量网文水平是量错了尺子。
    """
    # `prose_style` 是**档位名**，档位全文在 writer.yaml 里（S76）；而**当前选
    # 哪一档也在那儿**（R31）。场景要换文风就带一份 `PromptSetup`——它改的是
    # 提示词副本，不是 session（那上面已经没有配置了，S80）。
    styles = {name: s.prompts.choices["writer"]["prose_style"] for name, s in SCENARIOS.items()}
    bodies = {name: option("writer", "prose_style", value) for name, value in styles.items()}

    # 断言的是**两极真的对立**，不是某一档的措辞：档位正文随时会改，
    # 「回归集里必须有反差最大的一对」才是不变量。
    assert styles["noir"] != styles["pulp"]
    assert "网文" in bodies["pulp"]
    assert "克制" in bodies["noir"]


def test_chapters_scenario_declares_boundaries_without_leaking_them() -> None:
    """**分界准确率是少数几个 program grader 能直接算的语义指标**——
    分界点是位置，位置对不对是二值的，不需要品味。

    但预期分界**不能喂给模型**，否则量的就是"它会不会照抄"。
    """
    chapters = SCENARIOS["chapters"]

    assert sum(chapters.segments) == len(chapters.inputs)
    assert chapters.boundaries == {3, 6, 9}, "末尾不算分界——那是序列结束，不是判断"

    # 输入里不能出现段号、"第一段"这类提示
    joined = "".join(chapters.inputs)
    for leak in ("第一段", "第二段", "分界", "段落"):
        assert leak not in joined


def test_boundary_score_separates_recall_from_precision() -> None:
    """切得太碎（查准低）和切不出来（查全低）是两种不同的失败，不能混成一个分。"""
    from agenttavern.bench.runner import BenchResult

    result = BenchResult(scenario="x", variant="y", session_id="z")
    result.expected_boundaries = {3, 6, 9}
    result.actual_boundaries = {3, 4, 6}

    score = result.boundary_score()
    assert score is not None
    assert score["命中"] == [3, 6]
    assert score["查全"] == 0.67
    assert score["查准"] == 0.67

    # 没有预期分界的场景不该硬算一个分出来
    assert BenchResult(scenario="x", variant="y", session_id="z").boundary_score() is None


def test_a_scenario_is_long_enough_to_trigger_progressive_disclosure() -> None:
    """短场景走不到渐进式披露那条路径，`history_search` 与 `summarize` 就等于没测过。

    探针的形状同样要守住：**早埋一个具体的东西，晚了再要它**。
    只是"轮数够多"不够——还得有个东西会滑出全文窗口而后被需要。
    """
    long = SCENARIOS["noir_long"]
    assert len(long.inputs) >= 12, "要够长才走得到压缩那条路径"

    planted_at, probe_at = 1, len(long.inputs) - 2
    planted, probe = long.inputs[planted_at], long.inputs[probe_at]

    # 埋点必须远早于全文窗口，否则模型不必检索也能看见
    assert probe_at - planted_at >= 8, "埋点要远早于追问，中间足够发生压缩"

    # **埋的东西当时必须不要紧。** 摘要保留"人物、地点、关键事件与因果"，
    # 所以埋点要落在这四样之外——第三版埋的是中心问题的直接答案（"你那天在哪"），
    # 摘要完整保住了它，检索因此正确地闲置，探针什么也没测到。
    assert "台账" in planted, "埋点要是个背景细节，不是当轮的答案"
    assert "7月12日" in planted

    # **探针的问句里不能含有答案。** 第一版写成"三年前七月那天你在哪"，
    # 把要找的事实又说了一遍。现在只给指代。
    assert "7月12日" not in probe, "追问不能复述埋下的事实"
    assert "台账" in probe, "追问要靠指代去取"

    # **被测的能力必须真的打开。** `history_search` 默认关闭，关闭时两个工具
    # 都不注册——第一次跑这个场景时忘了开，于是"12 轮一次都没调用"
    # 这个观察什么也说明不了：工具当时根本不存在。开关现在在提示词里（S80），
    # 所以场景把它写进自己那份 `PromptSetup`。
    assert long.prompts.tools["director"]["history_search"] is True


# -- runner ------------------------------------------------------------------


async def test_run_scenario_chains_turns_and_keeps_going_after_a_failure(tmp_path: Path) -> None:
    """一轮失败不终止整条序列——否则第一轮的偶发错误会让整批样本作废，
    而我们要的正是失败样本。"""
    llm = ScriptedLLM(
        # 第 1 轮：正常
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        # 第 2 轮：director 只回文本、永不提交，耗尽轮数 → 失败
        text("我在想"),
        state=dict(set={"地点": "客厅"}),
    )
    settings = Settings(data_dir=tmp_path, llm_model="test")
    scenario = SCENARIOS["noir"]

    result = await run_scenario(scenario, llm.gateway(), settings, limit=2)

    assert len(result.turns) == 2
    assert result.turns[0].node is not None
    assert result.turns[1].failure is not None
    assert result.turns[1].node is None

    totals = result.totals()
    assert totals["turns"] == 2
    assert totals["failed"] == 1


async def test_turns_are_chained_so_state_accumulates(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        tool("submit_outline", outline="她看着他。"),
        text("她看着他。灯很暗。"),
        state=[dict(set={"地点": "客厅"}), dict(append={"林昭": {"知情": ["陈撒谎了"]}})],
    )
    settings = Settings(data_dir=tmp_path, llm_model="test")

    result = await run_scenario(SCENARIOS["noir"], llm.gateway(), settings, limit=2)

    assert [t.node.parent_id for t in result.turns if t.node] == [
        None,
        result.turns[0].node.id,  # type: ignore[union-attr]
    ]
    # 第二轮开始时的状态里应当已有第一轮写入的键，以及 worldbook 种子
    assert result.turns[1].state_before["地点"] == "客厅"
    assert result.turns[1].state_before["陈屿"]["在场"] is True


def test_a_variant_layers_on_top_of_the_scenario() -> None:
    """变体只改配置，不改题材与输入——否则比的不是同一件事（P19）。

    **它改的是提示词，不是 session**（S80）：`capabilities` / `control` 整体
    上调到了提示词层之后，往 session 上写配置这条路失去了全部着力点。补法是
    换一份 YAML 副本（`prompts_as`），不是开一个 `overrides` 后门——一个
    "只有程序能用"的第二取值入口迟早会有第二个调用者。

    **场景先定，变体再改**：变体提到的那几档盖掉场景的，没提到的沿用——
    换 `history_search` 不该顺手把文风也换了。
    """
    from agenttavern.prompting import PromptSetup

    scenario = SCENARIOS["noir"]
    variant = Variant(name="no-state", prompts=PromptSetup(agents={"state_manager": False}))
    merged = variant.over(scenario.prompts)

    assert merged.agents == {"state_manager": False}
    assert merged.choices["writer"]["prose_style"] == "jishi", "场景那一档要留着"

    # 空变体 = 场景原样。
    assert Variant(name="baseline").over(scenario.prompts).choices == scenario.prompts.choices


# -- grader ------------------------------------------------------------------


def test_parse_reads_the_two_line_format() -> None:
    assert parse("判定：没问题\n证据：无") == (Verdict.OK, "")
    assert parse("判定：有问题\n证据：细纲说他在场，状态里他不在。") == (
        Verdict.ISSUE,
        "细纲说他在场，状态里他不在。",
    )
    # 前缀符号与半角冒号都要容得下
    assert parse("- 判定: 有问题\n- 证据: 花掉的钱超过持有量")[0] is Verdict.ISSUE


def test_unparseable_output_is_unknown_not_a_guess() -> None:
    """**判不出来就说判不出来。** 混进假数据的 grader 比没有 grader 更糟——
    它的错误会直接误导后续所有决策。"""
    assert parse("这段写得挺好的")[0] is Verdict.UNKNOWN
    assert parse("")[0] is Verdict.UNKNOWN


async def test_each_class_is_graded_in_its_own_clean_context() -> None:
    """**干净上下文**（P20）：每类只看自己的参照物 + 待判对象。

    给全了它会往"整体看没问题"的方向读。这条只能靠断言 prompt 里**没有**
    不该出现的东西来守——这正是最容易在重构中悄悄失效的性质。
    """
    llm = ScriptedLLM(text("判定：没问题\n证据：无"))

    await grade_turn(
        llm.gateway(),
        user_text="我推开门",
        outline="她推开门，屋里坐着陈。",
        prose="门轴响了一声。屋里坐着陈。",
        state_before={"陈屿.在场": True},
        delta=StateDelta(set={"地点": "客厅"}),
    )

    bodies = [r["messages"][-1]["content"] for r in llm.requests]
    assert len(bodies) == 5

    retrieval, reasoning, deviation, write_miss, _noise = bodies

    # 检索不足只看 state + 细纲：不该看见用户输入或正文
    assert "陈屿.在场" in retrieval and "她推开门" in retrieval
    assert "我推开门" not in retrieval and "门轴响了一声" not in retrieval

    # 推演错只看用户输入 + 细纲
    assert "我推开门" in reasoning
    assert "陈屿.在场" not in reasoning and "门轴响了一声" not in reasoning

    # 偏离只看细纲 + 正文
    assert "门轴响了一声" in deviation and "她推开门" in deviation
    assert "陈屿.在场" not in deviation

    # 写入遗漏只看正文 + 增量
    assert "门轴响了一声" in write_miss and "地点" in write_miss
    assert "我推开门" not in write_miss


async def test_prose_less_turns_skip_the_two_classes_that_need_it() -> None:
    """run 失败或被打断时正文缺失，偏离与写入遗漏无从判起——跳过而非硬判。

    写入冗余仍然判得了：它只对着判据看增量，不需要正文。
    """
    llm = ScriptedLLM(text("判定：没问题\n证据：无"))

    results = await grade_turn(
        llm.gateway(),
        user_text="我推开门",
        outline="她推开门。",
        prose="",
        state_before={},
        delta=StateDelta(),
    )

    assert [j.attribution for j in results] == [
        Attribution.RETRIEVAL,
        Attribution.REASONING,
        Attribution.WRITE_NOISE,
    ]


async def test_write_miss_sees_prior_state_so_it_can_tell_missing_from_already_known() -> None:
    """**首批实测的误报**：种子里写着"与一个已死之人站在一起"，模型本轮只记了死者
    的名字，grader 却报"未记录此人已死"。

    参照物原本定的是「增量 vs 正文」——不给本轮之前的 state，它分不清
    "漏记"和"早就记过"。这一条是文档里那张参照物表本身不完整。
    """
    llm = ScriptedLLM(text("判定：没问题\n证据：无"))

    await grade_turn(
        llm.gateway(),
        user_text="问他是谁",
        outline="他说那人叫何平。",
        prose="“何平。”他说。“死了有些年头了。”",
        state_before={"林昭.知情": ["陈屿与一个已死之人站在一起"]},
        delta=StateDelta(append={"林昭.知情": ["照片里另一个人叫何平"]}),
    )

    bodies = [r["messages"][-1]["content"] for r in llm.requests]
    write_miss = bodies[3]
    assert "已死之人" in write_miss, "写入遗漏必须看得见本轮之前的状态"


async def test_write_noise_judges_the_delta_against_the_criterion_alone() -> None:
    """**S12 的五类里没有"记了不该记的"**——写入遗漏只查缺不查多。

    首批实测里 `照片.状态: "柜台，正面朝上"` 被四类一致判为没问题，
    而它正是 S34 点名不该记的东西。

    这一类只看增量，**不给正文**：给了会诱它去论证"这条确实发生了"，
    而"确实发生了"和"值得记"是两回事。
    """
    llm = ScriptedLLM(text("判定：没问题\n证据：无"))

    await grade_turn(
        llm.gateway(),
        user_text="她把照片翻了过来",
        outline="她把照片翻过来，正面朝上。",
        prose="她伸手，捏住照片边角，翻了过来。",
        state_before={},
        delta=StateDelta(set={"照片.状态": "柜台，正面朝上"}),
    )

    noise = [r["messages"][-1]["content"] for r in llm.requests][4]
    assert "照片.状态" in noise
    assert "捏住照片边角" not in noise, "写入冗余不该看见正文"
    assert "她把照片翻了过来" not in noise


async def test_repeat_keeps_only_findings_that_reproduce() -> None:
    """**随机 grader 的信号是复现率，不是单次判定**（P19）。

    实测：同一批素材连判三次（温度已是 0），跨次稳定复现的三条全是真问题，
    只出现一次的四条全是误报。温度设 0 消不掉这种波动。

    因此多次判定时要过半数才记为有问题——否则噪声会直接灌进分布，
    而分布正是决定"改哪一层"的唯一依据。
    """
    # 每类判 3 次：第 1 次说有问题，之后都说没问题 → 1/3，不过半
    llm = ScriptedLLM(
        text("判定：有问题\n证据：偶发的一条"),
        text("判定：没问题\n证据：无"),
    )

    results = await grade_turn(
        llm.gateway(),
        user_text="x",
        outline="y",
        prose="z",
        state_before={},
        delta=StateDelta(set={"a": 1}),
        repeat=3,
    )

    first = results[0]
    assert first.runs == 3
    assert first.hits == 1
    assert first.verdict is Verdict.OK, "1/3 是噪声，不该记为有问题"
    assert not first.stable


async def test_a_single_run_still_counts_a_hit() -> None:
    """``repeat=1`` 时没有复现率可言，退回"判了就是有问题"——
    否则默认用法会把所有发现都吞掉。"""
    llm = ScriptedLLM(text("判定：有问题\n证据：某处矛盾"))

    results = await grade_turn(
        llm.gateway(),
        user_text="x",
        outline="y",
        prose="z",
        state_before={},
        delta=StateDelta(),
    )

    assert results[0].verdict is Verdict.ISSUE
    assert results[0].runs == 1


def test_calibrations_from_the_first_two_batches_are_pinned() -> None:
    """校准出来的豁免条款必须钉住——它们每一条都对应一次实测误报，
    删掉任何一条那个误报就会回来。

    这三条也是同一件事的三个面：**grader 拿不到人设与写作约定**（P20 干净上下文
    的代价），所以合法的写法会被读成矛盾。解法不是把上下文补全（那会让它开始
    为一切辩护），而是把**具体的合法模式**逐条写进判据。
    """
    from agenttavern.bench.grader import _PROMPTS

    # 首批：陈屿知道照片来源却反问出处 → 那正是他的人设（说话绕、用反问代替回答）
    assert "故意装作不知道" in _PROMPTS[Attribution.RETRIEVAL]
    # 首批：用户说"钟表铺"、细纲写"旧货店"，同一家店
    assert "另一种叫法不算换含义" in _PROMPTS[Attribution.REASONING]
    # 重判：把 set null（删除键）读成了"记录未明状态"，而删除正是 S36 鼓励的
    assert "是删除这个键" in _PROMPTS[Attribution.WRITE_NOISE]


async def test_grader_failure_becomes_unknown_not_a_crash() -> None:
    class Boom:
        async def complete(self, *a: object, **k: object) -> object:
            raise RuntimeError("端点炸了")

    results = await grade_turn(
        Boom(),  # type: ignore[arg-type]
        user_text="x",
        outline="y",
        prose="z",
        state_before={},
        delta=StateDelta(),
    )

    assert len(results) == 5
    assert all(j.verdict is Verdict.UNKNOWN for j in results)


# -- report ------------------------------------------------------------------


def test_attribution_table_excludes_the_class_with_no_referent() -> None:
    """「writer 文风失败」没有参照物，AI 判不了（P19）。
    它不该出现在归因表里，但报告必须显式提醒它仍待人工判断。"""
    table = attribution_table([])

    assert "writer 文风失败" in table  # 以"不在表内"的形式出现
    assert "只能人看" in table
    for attribution in Attribution:
        assert attribution.value in table


def test_unknown_is_counted_separately_from_both_sides() -> None:
    """unknown 不能被算进"有问题"或"没问题"任何一边，否则分布会失真。"""
    from agenttavern.bench.grader import Judgement

    table = attribution_table(
        [
            [
                Judgement(Attribution.RETRIEVAL, Verdict.ISSUE, "矛盾"),
                Judgement(Attribution.REASONING, Verdict.UNKNOWN, ""),
            ]
        ]
    )

    # 检索不足：1 有问题 / 0 没问题 → 100%
    assert "**1**（100%）" in table


async def test_regrade_rebuilds_the_same_material_from_disk(tmp_path: Path) -> None:
    """**改了 grader 就要能重判同一批素材。**

    否则每次校准都得重新生成——既贵，而且换了一批生成结果，
    grader 前后的差异和模型本身的随机性就混在一起了。
    """
    from agenttavern.bench import load_result

    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        tool("submit_outline", outline="她看着他。"),
        text("她看着他。灯很暗。"),
        state=[dict(set={"地点": "客厅"}), dict(append={"林昭.知情": ["陈撒谎了"]})],
    )
    settings = Settings(data_dir=tmp_path, llm_model="test")
    original = await run_scenario(SCENARIOS["noir"], llm.gateway(), settings, limit=2)

    reloaded = load_result(settings, original.session_id)

    assert [t.user_text for t in reloaded.turns] == [t.user_text for t in original.turns]
    assert [t.outline for t in reloaded.turns] == [t.outline for t in original.turns]
    assert [t.prose for t in reloaded.turns] == [t.prose for t in original.turns]
    # 每一轮**开始时**的状态也要重建对——写入遗漏那一类靠它分辨"早就记过"
    assert [t.state_before for t in reloaded.turns] == [t.state_before for t in original.turns]


async def test_report_renders_without_grading(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        state=dict(set={"地点": "客厅"}),
    )
    settings = Settings(data_dir=tmp_path, llm_model="test")
    result = await run_scenario(SCENARIOS["noir"], llm.gateway(), settings, limit=1)

    markdown = render(result, [], model="m", grader_model="g")

    assert "她推开门。" in markdown
    assert "待人工判断" in markdown


async def test_a_goal_scenario_is_driven_by_the_goal_manager(tmp_path: Path) -> None:
    """**种子之后每一轮的输入都由 goal_manager 写**（S92）。

    bench 以前直接调 `execute_run`，`_check_goal` 在 `SessionRunner` 里，
    于是整套 goal 机制从来没进过回归集——"没跑过"和"跑过没问题"看起来一样。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["他说出那个名字"]}),
            ("steer", {"next_input": "我把照片翻过来。"}),
        ),
        text("先推到这里。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        tools(
            ("check", {"reached": [1]}),
            ("finish", {"outcome": "done", "reason": "逐条核对：名字已经说出来了"}),
        ),
        text("完了。"),
    )
    settings = Settings(data_dir=tmp_path, llm_model="test")

    result = await run_scenario(SCENARIOS["noir_goal"], llm.gateway(), settings)

    assert [t.user_text for t in result.turns] == [
        SCENARIOS["noir_goal"].inputs[0],
        "我把照片翻过来。",
    ]
    assert result.turns[1].node is not None
    assert result.turns[1].node.user_input is not None
    assert result.turns[1].node.user_input.source is InputSource.SIMULATED

    assert result.goal_outcome == "done"
    assert "名字" in result.goal_reason
    assert result.goal_plan and "已成立" in result.goal_plan[0]


async def test_a_goal_scenario_stops_when_the_budget_runs_out(tmp_path: Path) -> None:
    """预算到顶 → **暂停，不是失败**（S96）。bench 记下来就收，不再往下跑。"""
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["他说出那个名字"]}),
            ("steer", {"next_input": "我把照片翻过来。"}),
        ),
        text("先推到这里。"),
    )
    settings = Settings(data_dir=tmp_path, llm_model="test")
    scenario = SCENARIOS["noir_goal"]
    stingy = Variant(name="穷", note="只给一轮预算")

    session = scenario.make_session()
    assert session.meta.goal is not None
    session.meta.goal.max_runs = 1
    result = await run_scenario(
        Scenario(
            name=scenario.name,
            make_session=lambda: session,
            worldbook=scenario.worldbook,
            inputs=scenario.inputs,
        ),
        llm.gateway(),
        settings,
        variant=stingy,
    )

    assert result.goal_outcome == "paused"
    assert "1 轮" in result.goal_reason
    assert len(result.turns) == 1


def test_every_defined_scenario_is_registered() -> None:
    """**定义了却没注册**是这文件里最容易犯的错，而且现有测试一个都抓不到——
    它们全都遍历 `SCENARIOS`，漏掉的那个自然不在里面。

    症状是 `--scenario` 直接报 invalid choice，白等一次跑。
    """
    from agenttavern.bench import scenarios as module

    defined = {
        value.name
        for name, value in vars(module).items()
        if isinstance(value, Scenario) and not name.startswith("_")
    }
    assert defined == set(SCENARIOS), f"没注册：{defined - set(SCENARIOS)}"
