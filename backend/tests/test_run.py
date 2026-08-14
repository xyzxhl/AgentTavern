"""完整的一个 run：director → writer → 校验 → 落节点。"""

import asyncio
import json
from pathlib import Path

import pytest
from conftest import STATE_MARK, CancelWriter, ScriptedLLM, text, tool

from agenttavern.models import (
    Capabilities,
    ControlOverride,
    ExpansionDensity,
    InputHandling,
    RunStage,
    RunStatus,
    Session,
    UserInput,
    WorldbookEntry,
)
from agenttavern.orchestration import execute_run
from agenttavern.prompting import choice
from agenttavern.storage import Store
from agenttavern.tools import ToolRegistry
from agenttavern.tracing import Tracer


def happy_llm(prose: str = "门轴发出一声长长的呻吟。") -> ScriptedLLM:
    return ScriptedLLM(
        tool("submit_outline", outline="她推开门，屋里坐着陈。"),
        text(prose),
        state=dict(set={"地点": "客厅", "林": {"在场": True}, "陈": {"在场": True}}),
    )


def setup(tmp_path: Path, llm: ScriptedLLM, session: Session | None = None):
    repo = Store(tmp_path).create_session(session or Session())
    return repo, repo.load_session(), llm.gateway(), ToolRegistry()


async def test_the_run_hands_the_control_dimensions_to_the_tool_face(tmp_path: Path) -> None:
    """**控制维度必须传到工具面**（D155）。

    `ask_question` 的调用积极性写在它自己的工具描述里，按 `model_authority` 分三档
    （D39/D58）。`execute_run` 组装工具面时漏掉了 `control`，于是不管控制维度调成
    什么，模型读到的永远是 `medium` 那档——`low` 档「遇到任何值得由用户决定的分岔
    都调用它征询」从来没真正发出去过。

    **原来那条测试测不出这个**：它直接调 `build_tools` 并显式传档位，验的是机制；
    漏的是接线。所以这条从 `execute_run` 进去，断言真正发给模型的那份 spec。
    """
    from agenttavern.models import ControlDimensions, ModelAuthority, SessionMeta

    session = Session(
        meta=SessionMeta(),
        control=ControlDimensions(model_authority=ModelAuthority.LOW),
        capabilities=Capabilities(ask_question=True),
    )
    llm = happy_llm()
    repo = Store(tmp_path).create_session(session)

    # `registry=None`，走 `execute_run` 自己那条组装路径——接线就在那里。
    await execute_run(repo, repo.load_session(), llm.gateway(), user_input=UserInput(text="我推门"))

    specs = next(r["tools"] for r in llm.requests if r.get("tools"))
    ask = next(s for s in specs if s["function"]["name"] == "ask_question")
    assert "一轮内可以多次" in ask["function"]["description"], "low 档的措辞没发出去"


async def test_full_run_produces_a_node(tmp_path: Path) -> None:
    llm = happy_llm()
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    node = outcome.node

    assert node.outline == "她推开门，屋里坐着陈。"
    assert node.prose == "门轴发出一声长长的呻吟。"
    assert node.state_delta.set["地点"] == "客厅"
    assert node.status is RunStatus.COMPLETE
    assert repo.load_node(node.id).prose == node.prose
    assert repo.load_session().active_leaf_id == node.id


async def test_state_lands_only_when_run_completes(tmp_path: Path) -> None:
    """暂存的增量随节点一并落盘——细纲与增量同时抵达，节点是原子的（D56）。"""
    llm = happy_llm()
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert repo.state_at(outcome.node.id).values["地点"] == "客厅"


async def test_second_run_builds_on_the_first(tmp_path: Path) -> None:
    repo, session, gw, reg = setup(tmp_path, happy_llm())
    first = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    llm2 = ScriptedLLM(
        tool("submit_outline", outline="两人走到阳台。"),
        text("风把窗帘掀起来。"),
        state=dict(set={"地点": "阳台"}),
    )
    second = await execute_run(
        repo,
        repo.load_session(),
        llm2.gateway(),
        reg,
        user_input=UserInput(text="我们出去"),
        parent_id=first.node.id,
    )

    assert second.node.parent_id == first.node.id
    assert repo.state_at(second.node.id).values["地点"] == "阳台"

    # 第二轮的 director 应当看到第一轮的细纲，而**看不到**第一轮的正文（D54）。
    # 对话跨轮持续之后（D114），细纲在重放的消息里（上一轮的 submit_outline 调用），
    # 不再在 system prompt 里——**这个断言因此比原来更强**：它现在检查的是
    # director 收到的**全部**上下文，而不只是开头那一段。
    everything = str(llm2.messages_of(0))
    assert "她推开门，屋里坐着陈。" in everything
    assert "门轴" not in everything


async def test_writer_sees_prose_upstream_director_does_not(tmp_path: Path) -> None:
    """writer 读正文上文以保证衔接——这正是 D33 那一刀能切开的原因。"""
    repo, session, gw, reg = setup(tmp_path, happy_llm())
    first = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    llm2 = ScriptedLLM(
        tool("submit_outline", outline="两人走到阳台。"),
        text("风把窗帘掀起来。"),
    )
    await execute_run(
        repo,
        repo.load_session(),
        llm2.gateway(),
        reg,
        user_input=UserInput(text="我们出去"),
        parent_id=first.node.id,
    )

    writer_messages = str(llm2.messages_of(2))
    assert "门轴" in writer_messages


async def test_deviation_report_is_extracted(tmp_path: Path) -> None:
    """writer 不改写细纲，只报告实质增删（D57）。"""
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。\n---偏离---\n补了一句她的犹豫。"),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert outcome.node.prose == "门开了。"
    assert outcome.node.deviation_report == "补了一句她的犹豫。"
    assert any("偏离" in n for n in outcome.notes)


async def test_control_override_applies_for_this_run_only(tmp_path: Path) -> None:
    """单轮覆盖：未指定的项沿用常驻设置（D45）。"""
    llm = happy_llm()
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(
        repo,
        session,
        gw,
        reg,
        user_input=UserInput(text="我推开门"),
        control_override=ControlOverride(input_handling=InputHandling.NARRATE_ONLY),
    )

    assert outcome.node.control.input_handling is InputHandling.NARRATE_ONLY
    assert outcome.node.control.expansion_density is ExpansionDensity.NORMAL
    assert repo.load_session().control.input_handling is InputHandling.NARRATE_AND_ADVANCE
    # 控制维度**每轮都可能变**，所以它排在前情之后、跟这一轮的输入在一起，
    # 不进跨轮不变的那一段——那段是持续对话的缓存前缀，放会变的东西进去，
    # 前缀每轮都失效（D114）。
    directive = choice("director", "narration_and_advancement", "narrate_only")
    sent = llm.messages_of(0)
    assert any(directive in (m["content"] or "") for m in sent)
    assert all(m["role"] != "system" for m in sent if directive in (m["content"] or ""))


# -- 打断：哪一段被掐掉，哪一段就作废（D142） ----------------------------------


async def test_interrupt_during_director_lands_no_node(tmp_path: Path) -> None:
    """director 阶段被打断：细纲未成型，**不落节点**。

    早先这条测的是"什么都不留"，那是 D60 的原始语义。**现在推演本身会被存成
    草稿等续跑**（D118）——不落节点这一点没变，变的是被截断的推演去了哪。
    挂起与续跑的行为见 `test_branch.py`。
    """

    class Cancelling(ScriptedLLM):
        def _handle(self, request):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError

    repo, session, gw, reg = setup(tmp_path, Cancelling(text("x")))

    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    assert repo.all_nodes() == {}
    # 第一次 complete 就被掐掉，模型什么都没产出，所以 `messages` 是空的——
    # **它只装 loop 里产出的东西，不装提示词各节**（D145）。但草稿仍然要存：
    # 那条用户输入是**唯一不可再生的东西**，丢了用户的下一句话就没有指代了，
    # 所以它有自己的字段。
    draft = repo.load_draft()
    assert draft is not None
    assert draft.stage is RunStage.DIRECTOR
    assert draft.messages == []
    assert draft.user_input is not None and draft.user_input.text == "我推开门"


async def test_interrupt_during_writer_drops_the_half_written_prose(tmp_path: Path) -> None:
    """**半截正文不留**（D142，推翻 D60 的后半句）。

    D60 原来说「保留已生成的正文片段——那段剧情确实发生了」。那是把一个失败的
    阶段当成了产出：下一轮会接着那半句往下写，而用户按停止恰恰是因为不想要它。
    细纲留着，它是上游、已经写完了。
    """
    llm = CancelWriter(tool("submit_outline", outline="她推开门。"))
    repo, session, gw, reg = setup(tmp_path, llm)

    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    assert repo.all_nodes() == {}, "阶段失败就不落节点"

    draft = repo.load_draft()
    assert draft is not None
    assert draft.stage is RunStage.WRITER
    assert draft.outline == "她推开门。", "细纲是上游，已经完成了"
    assert draft.prose == ""
    assert draft.user_input is not None and draft.user_input.text == "我推开门"


async def test_an_empty_input_reruns_only_the_writer(tmp_path: Path) -> None:
    """空输入 = 「刚才那步没跑完，接着跑」——细纲不重写，director 根本不启动。"""
    repo, session, gw, reg = setup(
        tmp_path, CancelWriter(tool("submit_outline", outline="她推开门。"))
    )
    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    resume = ScriptedLLM(text("门轴响了一声。"), state=dict(set={"地点": "客厅"}))
    outcome = await execute_run(repo, repo.load_session(), resume.gateway(), reg)

    assert outcome.node.outline == "她推开门。", "细纲原样用，没有重写"
    assert outcome.node.prose == "门轴响了一声。"
    assert outcome.node.user_input is not None
    assert outcome.node.user_input.text == "我推开门", "被打断那一轮的输入不可再生"
    assert repo.state_at(outcome.node.id).values["地点"] == "客厅"
    assert repo.load_draft() is None

    # director 一次都没被叫起来——它的提示词特征不在任何一次请求里。
    assert not any("输出契约" in str(r["messages"]) for r in resume.requests)


async def test_a_new_input_goes_back_to_the_director(tmp_path: Path) -> None:
    """**非空输入 → 回 director**，旧细纲作为一条 context 摆给它看。

    判据是生成内容的依赖关系：用户又说了话，细纲就可能要改，那正文和 state
    都不作数了。
    """
    repo, session, gw, reg = setup(
        tmp_path, CancelWriter(tool("submit_outline", outline="她推开门。"))
    )
    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    resume = ScriptedLLM(
        tool("submit_outline", outline="她停在门口，没有推。"),
        text("她的手停在门板上。"),
    )
    outcome = await execute_run(
        repo,
        repo.load_session(),
        resume.gateway(),
        reg,
        user_input=UserInput(text="等等，她其实没推开"),
    )

    assert outcome.node.outline == "她停在门口，没有推。"

    # **旧细纲不必另开一节说明——它就在接回来的对话里。**上一次那句
    # `submit_outline` 连同它交出的细纲一起被草稿带了过来，所以 director
    # 看得见自己写过什么，也看得见当时的输入。
    context = str(resume.messages_of(0))
    assert "她推开门。" in context, "旧细纲要在，不能让它假装没写过"
    assert "我推开门" in context, "当时的输入也要在，否则旧细纲没有由来"
    assert "等等，她其实没推开" in context


async def test_interrupt_during_state_keeps_outline_and_prose(tmp_path: Path) -> None:
    """state 阶段被打断：上游两样都完成了，草稿一并带上，空输入时正文不重写。"""

    class Stopped(ScriptedLLM):
        def _handle(self, request):  # type: ignore[no-untyped-def]
            if STATE_MARK in json.loads(request.content)["messages"][0].get("content", ""):
                raise asyncio.CancelledError
            return super()._handle(request)

    llm = Stopped(tool("submit_outline", outline="她推开门。"), text("门轴响了。"))
    repo, session, gw, reg = setup(tmp_path, llm)

    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    draft = repo.load_draft()
    assert draft is not None
    assert draft.stage is RunStage.STATE
    assert draft.outline == "她推开门。"
    assert draft.prose == "门轴响了。"

    resume = ScriptedLLM(state=dict(set={"地点": "客厅"}))
    outcome = await execute_run(repo, repo.load_session(), resume.gateway(), reg)

    assert outcome.node.prose == "门轴响了。", "正文是上游，不该重写"
    assert repo.state_at(outcome.node.id).values["地点"] == "客厅"


# -- 交叉比对：只提示不拦截（D43） --------------------------------------------


async def test_absent_character_gaining_knowledge_is_not_flagged(tmp_path: Path) -> None:
    """**不做结构性检查。** "不在场就不该知情"看似成立，实则不可靠——信息可以
    通过转述、书信、监听获得。硬做只会得到假警报，而假警报比不检查更糟。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门开了。"),
        state=dict(set={"林.在场": True}, append={"陈.知情": ["林起疑了"]}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    assert not any("在场" in n for n in outcome.notes)
    assert repo.state_at(outcome.node.id).values["陈.知情"] == ["林起疑了"]


async def test_long_prose_with_empty_delta_is_flagged(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("很长的一段正文。" * 60),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert any("遗漏" in n for n in outcome.notes)


async def test_prose_barely_longer_than_outline_is_flagged(tmp_path: Path) -> None:
    """正文/细纲长度比是**隔离是否生效**最廉价的指标（D93）。

    比值贴近 1 说明 writer 在照抄——通常是因为 director 越界写了描写，
    正文侧已无事可做。这不是质量判断，只是把样本挑出来送审（D43/D44）。
    """
    outline = "她推开门，屋里坐着陈。" * 10
    llm = ScriptedLLM(
        tool("submit_outline", outline=outline),
        text(outline + "又一句。"),
        state=dict(set={"地点": "客厅"}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert any("照抄" in n for n in outcome.notes)


async def test_state_value_stuffed_with_a_biography_is_flagged(tmp_path: Path) -> None:
    """state 的每个键应当是**一条可被违反的约束**，不是一段小传。

    塞满分句的值既不好改也不好读，等于把细纲复述了一遍。只看形状不看内容——
    代码判断不了"这条该不该记"，只能报告"这个值不像一条约束"。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门，屋里坐着陈。"),
        text("蓝布帘子垂在门框里，颜色褪得发白。" * 8),
        state=dict(set={"许平": "已故男子，照片中的另一人；名字由陈屿说出；与陈屿相识；死因未明"}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert any("许平" in n and "拆成几个键" in n for n in outcome.notes)


async def test_collapsing_a_list_into_a_string_is_flagged(tmp_path: Path) -> None:
    """数据化世界实测：种子给的是清单和数字，模型写回了字符串。

    清单一旦被拍平就无法再单独增删其中一项，**且不可逆**。只比形状不比内容——
    形状变化有时是合理的，所以这是采样标记而非约束。
    """
    entry = WorldbookEntry(
        name="沈砚",
        content="散修。",
        seeds={"沈砚.随身": ["青锋剑", "止血符 ×2"], "沈砚.灵石": 3},
    )
    llm = ScriptedLLM(
        tool("submit_outline", outline="他把灵石倒在柜台上。"),
        text("三块灵石滚落台面，叮叮响了几声。" * 6),
        state=dict(set={"沈砚.随身": "青锋剑、止血符 ×4", "沈砚.灵石": "1"}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)
    repo.save_worldbook_entry(entry)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我买符"))
    flag = next(n for n in outcome.notes if "另一种形状" in n)
    assert "沈砚.随身" in flag and "沈砚.灵石" in flag


def _script(runs: int, outline: str, *tail, closes: list[str] | None = None, followup: bool = True):
    """每轮的调用序列：state_write / submit_outline / writer（/ 归段追问）。

    追问**每轮都发生**——判定在生成时，不等它滑出窗口（D113）。
    """
    answers = closes or ["0"] * runs
    body: list = []
    for i in range(runs):
        body += [
            tool("state_write", set={"地点": "客厅"}),
            tool("submit_outline", outline=outline),
            text("门开了。"),
        ]
        if followup:
            body.append(text(answers[i]))
    return ScriptedLLM(*body, *tail, auto_close=None if followup else "0")


async def _drive(repo, session, gateway, reg, runs: int):
    parent = None
    for _ in range(runs):
        outcome = await execute_run(repo, session, gateway, reg, parent_id=parent)
        parent = outcome.node.id
    return parent


async def test_segment_closes_when_the_director_says_it_closed(tmp_path: Path) -> None:
    """**归段是"生成后"的动作，而且要问 director 本人**（D112）。

    它对本轮产出毫无帮助，所以不能放进 tool loop；但"这场戏完了没有"是语义判断，
    需要 state、前情和故事走向——冷调用给不出来，只能接在刚写完的那段对话后面追问。
    """
    runs = 4
    long_outline = "她推开门，屋里坐着陈。" * 30

    llm = _script(
        runs,
        long_outline,
        text("她进了店，陈在屋里。"),
        closes=["0", "0", "0", "4"],
    )
    repo, session, gw, reg = setup(tmp_path, llm)
    parent = await _drive(repo, session, gw, reg, runs)

    end = repo.path_to(parent)[-1]
    assert end.closes_segment, "结尾标记在生成时当场打上"
    assert end.outline_summary == "她进了店，陈在屋里。"
    assert end.segment_span == runs, "摘要要记住自己覆盖了几轮"


async def test_open_run_count_resets_at_the_previous_close(tmp_path: Path) -> None:
    """追问要带上**这一段已经开了几轮**，而那是"上一个结尾之后有多少轮"。

    不能用"待归段有多少轮"——那只含已滑出全文窗口的部分，拿它去问会让模型
    以为这一段才刚开始，于是每轮都答"收"。实测那一版查准只有 0.25（D113）。
    """
    from agenttavern.orchestration.run import _open_segment

    runs = 5
    llm = _script(runs, "她推开门。", closes=["0", "2", "0", "0", "0"])
    repo, session, gw, reg = setup(tmp_path, llm)
    parent = await _drive(repo, session, gw, reg, runs)

    path = repo.path_to(parent)
    assert path[1].closes_segment
    # 第 2 轮是结尾，所以第 5 轮时这一段（3、4、5）已经开了 3 轮
    assert len(_open_segment(repo, parent)) == 3


async def test_open_segment_waits_another_run(tmp_path: Path) -> None:
    """回 `open` 表示这一段还没完——**那就再等一轮，不要硬压**。

    段落一压成摘要就不可逆了，宁可晚一轮。
    """

    runs = 4
    llm = _script(runs, "她推开门，屋里坐着陈。" * 30)
    repo, session, gw, reg = setup(tmp_path, llm)
    parent = await _drive(repo, session, gw, reg, runs)

    assert all(n.outline_summary is None for n in repo.path_to(parent))


async def test_every_segment_goes_through_the_summarizer(tmp_path: Path) -> None:
    """**一律压，短段也压**（D154）。

    这里以前有条近路：整段拼起来还比摘要短就原样留下。它的阈值是
    `summary_max_chars`，而那个数已经删掉了——它说了不算（48 条落盘摘要里 16 条
    超标，最坏 3.9 倍），换个名字留在这里只是把同一个问题藏起来。
    近路本来也几乎不触发：一段细纲动辄上千字。
    """
    runs = 4
    short = "她推开门。"

    # 最后那条 `text` 是压缩那次调用的回复。近路还在的话它根本不会被用到。
    llm = _script(runs, short, text("她进了店。"), closes=["0", "0", "0", "4"])
    repo, session, gw, reg = setup(tmp_path, llm)
    parent = await _drive(repo, session, gw, reg, runs)

    assert repo.path_to(parent)[-1].outline_summary == "她进了店。"


async def test_manual_close_does_not_ask_the_model(tmp_path: Path) -> None:
    """手动归段是第三条支路：**用户说这一段收了，就是收了**，不再追问。

    这条路在 MANUAL 模式下是唯一的归段方式；AUTO 下也保留——自动追问
    判早了或判晚了，用户随时可以自己断。
    """
    from agenttavern.models import Capabilities, SummarizeMode
    from agenttavern.orchestration import close_segment

    runs = 4
    session = Session(capabilities=Capabilities(summarize=SummarizeMode.MANUAL))
    llm = _script(runs, "她推开门。", text("四轮压成这一句。"), followup=False)
    repo, session, gw, reg = setup(tmp_path, llm, session)

    parent = await _drive(repo, session, gw, reg, runs)
    # MANUAL 下不追问，所以什么也没归段
    assert all(n.outline_summary is None for n in repo.path_to(parent))

    end = await close_segment(repo, gw, parent, Tracer(repo, parent), forced=True)
    assert end is not None
    assert end.outline_summary == "四轮压成这一句。"
    assert end.segment_span == runs


async def test_backfill_failure_does_not_fail_the_run(tmp_path: Path) -> None:
    """归段失败只该回退到头部截断——这一轮已经落盘了，不能因此报错。"""

    runs = 4
    llm = _script(runs, "她推开门，屋里坐着陈。" * 30, closes=["0", "0", "0", "4"])
    repo, session, gw, reg = setup(tmp_path, llm)

    class Exploding:
        def __init__(self, inner):
            self._inner = inner
            self.calls = 0

        async def complete(self, messages, **kwargs):
            # 追问接在 director 对话之后（认最后一条 user 消息），压缩是独立调用
            # （认 system 提示词）。**匹配串要挑独一份的**——writer 的提示词里
            # 也有"结尾"二字，拿它做判据会把 writer 一起炸掉。
            if "本轮已经提交" in messages[-1].content or "压缩到" in messages[0].content:
                self.calls += 1
                raise RuntimeError("摘要端点炸了")
            return await self._inner.complete(messages, **kwargs)

    gateway = Exploding(gw)
    parent = await _drive(repo, session, gateway, reg, runs)

    assert gateway.calls > 0, "应当真的尝试过"
    assert all(n.outline_summary is None for n in repo.path_to(parent))


async def test_a_bare_key_shadowing_a_namespaced_one_is_flagged(tmp_path: Path) -> None:
    """实测：``沈砚 › 冷却`` 已存在，模型另起了一个顶层的 ``冷却``。

    那一轮 state 里两个冷却键并存且值不同。模型下一轮自己发现并删掉了，
    但中间那一轮是脏的，而**当时没有任何检查报得出来**——值的形状没变、
    没有超长、也不算漏记。提示词里那句"新建键前先看有没有已在说同一件事的键"
    没能拦住它。
    """
    entry = WorldbookEntry(name="沈砚", content="散修。", seeds={"沈砚": {"冷却": "御剑斩剩3回合"}})
    llm = ScriptedLLM(
        tool("submit_outline", outline="他又催了一次剑。"),
        text("剑没有起来。" * 10),
        state=dict(set={"冷却": "御剑斩剩2回合"}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)
    repo.save_worldbook_entry(entry)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="再来一记"))
    flag = next(n for n in outcome.notes if "只差一层归属" in n)
    assert "冷却" in flag and "沈砚 › 冷却" in flag


async def test_sibling_keys_under_different_owners_are_not_flagged(tmp_path: Path) -> None:
    """``林昭 › 知情`` 与 ``陈屿 › 知情`` 末段相同却都是对的——**两边都有归属
    就不是歧义**。误报这种会让标记立刻失去价值。"""
    entry = WorldbookEntry(name="林昭", content="记者。", seeds={"林昭": {"知情": ["照片"]}})
    llm = ScriptedLLM(
        tool("submit_outline", outline="他记住了她。"),
        text("他看着她走出去。" * 8),
        state=dict(append={"陈屿": {"知情": ["她来过"]}}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)
    repo.save_worldbook_entry(entry)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我走了"))
    assert not any("只差一个前缀" in n for n in outcome.notes)


async def test_ordinary_state_values_are_not_flagged(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门，屋里坐着陈。"),
        text("蓝布帘子垂在门框里，颜色褪得发白。" * 8),
        state=dict(set={"许平.生死": "已死", "陈屿.在场": True}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert not any("拆成几个键" in n for n in outcome.notes)


async def test_properly_expanded_prose_is_not_flagged(tmp_path: Path) -> None:
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门，屋里坐着陈。"),
        text("蓝布帘子垂在门框里，颜色褪得发白。" * 8),
        state=dict(set={"地点": "客厅"}),
    )
    repo, session, gw, reg = setup(tmp_path, llm)

    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))
    assert not any("照抄" in n for n in outcome.notes)


# -- trace -------------------------------------------------------------------


async def test_trace_is_not_persisted_by_default(tmp_path: Path) -> None:
    """trace 服务于 eval，是**开发者模式**功能，默认关闭——不落盘。"""
    repo, session, gw, reg = setup(tmp_path, happy_llm())
    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    assert repo.read_trace(outcome.node.id) == []
    assert not repo.trace_path(outcome.node.id).exists()


async def test_trace_covers_the_whole_run(tmp_path: Path) -> None:
    """开发者模式下落盘，且**以 node 为单位**——一个 run 一个文件，
    记录从用户输入到最终输出之间发生的一切。
    """
    llm = happy_llm()
    repo, session, gw, reg = setup(tmp_path, llm, Session(capabilities=Capabilities(trace=True)))
    outcome = await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    events = repo.read_trace(outcome.node.id)
    labels = [e.label for e in events]
    assert all(e.run_id == outcome.node.id for e in events)

    assert "开始新的一轮" in labels
    assert "细纲完成" in labels
    assert "正文完成" in labels
    assert "本轮结束" in labels
    assert [e.seq for e in events] == list(range(len(events)))
    assert all(e.label for e in events)
    assert outcome.usage.tokens_in > 0


async def test_going_back_to_the_director_starts_the_round_count_over(tmp_path: Path) -> None:
    """**回 director 重来是一次新的推演，不是续跑**，所以轮数归零（D142）。

    硬上限防的是「反复打断、把同一段推演无限续下去」，所以 director 阶段续跑时
    用掉的额度不退。但正文阶段被打断后回来，交的是**另一份细纲**——把额度算在
    它头上的话，在正文阶段反复打断几次就能把 24 轮提前吃光。
    """
    repo, session, gw, reg = setup(
        tmp_path,
        CancelWriter(tool("submit_outline", outline="她推开门。")),
        Session(capabilities=Capabilities(trace=True)),
    )
    with pytest.raises(asyncio.CancelledError):
        await execute_run(repo, session, gw, reg, user_input=UserInput(text="我推开门"))

    draft = repo.load_draft()
    assert draft is not None and draft.rounds > 0, "director 那一轮确实用掉了额度"

    resume = ScriptedLLM(tool("submit_outline", outline="她停在门口。"), text("她没有推。"))
    outcome = await execute_run(
        repo,
        repo.load_session(),
        resume.gateway(),
        reg,
        user_input=UserInput(text="等等，她其实没推开"),
    )
    assert outcome.node.outline == "她停在门口。"

    # **从「第 1 轮」重新数。** 接着数的话这里会是「第 2 轮」——那正是在正文阶段
    # 反复打断几次就能把 24 轮吃光的形态。
    labels = [e.label for e in repo.read_trace(outcome.node.id)]
    assert "director 第 1 轮思考" in labels
    assert "director 第 2 轮思考" not in labels
