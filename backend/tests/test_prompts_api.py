"""提示词管理的读写接口（R31）。

判据是：**所有被输入模型的内容，用户都能审查和修改。** 这份文件钉的是那条判据
的三个落点——工具描述、附属提示词、渲染文案——以及写盘前那道**试装配**。
"""

from pathlib import Path

import pytest
from conftest import ScriptedLLM
from fastapi.testclient import TestClient

from agenttavern.config import Settings
from agenttavern.prompting import AGENTS, CALLS, PROMPT_FILES, assembler
from agenttavern.server import create_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    return TestClient(create_app(settings, ScriptedLLM().gateway()))


@pytest.fixture(autouse=True)
def sandbox(tmp_path: Path):
    """**写盘的测试必须写在副本上。** 直接写仓库里那几份 YAML，一个失败的用例
    就把提示词改坏了，而那是这个项目里最不该被测试碰的东西。
    """
    import shutil

    room = tmp_path / "prompts"
    shutil.copytree(assembler._DIR, room, ignore=shutil.ignore_patterns("__pycache__"))
    assembler.use_dir(room)
    yield room
    assembler.use_dir(None)


def test_the_list_comes_from_the_yaml_files(client) -> None:
    """有哪几份提示词**不在前端写死**——加一份就是加一个名字加一个文件。"""
    names = [a["name"] for a in client.get("/prompts").json()]
    assert names == list(PROMPT_FILES)


def test_the_prompt_page_separates_agents_from_single_calls(client) -> None:
    """**接口自己带分类**（S147），不让前端按名字猜。

    agent 会推演、会产出剧情、有自己的工具面；单次调用输入固定、输出固定、
    不参与剧情。前端按名字硬编码分类的话，加一份 YAML 就要同时改两处——
    而漏改的症状是它悄悄出现在错误的那一类里。
    """
    groups = {a["name"]: a["group"] for a in client.get("/prompts").json()}
    assert {n for n, g in groups.items() if g == "agent"} == set(AGENTS)
    assert {n for n, g in groups.items() if g == "call"} == set(CALLS)


def test_a_tool_carries_its_description_and_its_schema(client) -> None:
    """**描述可编，参数只读**（R31，落地 S10 那条担忧）。

    S10 判过「工具描述不搬进 YAML」，理由是描述跟参数是同一件事、拆开必然对不上。
    那个风险是真的，所以参数 schema 仍由 Python 生成，在页面上与描述**并排显示**
    ——漂移摆在人眼前，而不是藏在两个文件里。
    """
    doc = client.get("/prompts/director").json()

    assert "提交本轮细纲" in doc["data"]["tools"]["submit_outline"]["body"]
    # `ask_question` 的描述本身是一个 `choice`：控制权三档讲的正是「这个工具该
    # 多积极地调用」（S10②），三类不必为工具再加第四类。
    assert set(doc["data"]["tools"]["ask_question"]["options"]) == {"low", "medium", "high"}

    schema = doc["params"]["submit_outline"]
    assert "outline" in schema["properties"], "参数由 Python 生成，只读"


def test_the_specials_and_the_rendering_strings_are_reachable(client) -> None:
    """**归段追问和渲染文案也在这份文件里**（R31 那次审计新发现的两处）。

    `_CLOSE_SEGMENT` 曾经是 `postrun.py` 里一段四百多字的 Python 字符串——模型
    每轮都会读到它，而它从界面完全够不着。渲染文案更隐蔽：`用 worldinfo_read 按
    ref 展开。` 是一句**明确的操作指令**，改了工具名却忘了改它，模型会去调一个
    不存在的工具。
    """
    data = client.get("/prompts/director").json()["data"]

    segment = data["specials"]["segment"]["sections"][0]["body"]
    # 正文归用户，不钉具体措辞；这里只守住代码会填的两个变量和工具接线。
    assert "{{open_runs}}" in segment
    assert "{{listing}}" in segment
    assert "close_segment" in segment
    assert data["rendering"]["worldinfo_expand"] == "用 worldinfo_read 按名字展开。"
    # 前情那两句同理：它们是折起来的那一段摆出来的话，其中一句还带着工具名。
    assert "history_read" in data["rendering"]["history_folded"]


def test_saving_runs_a_dry_assembly_first(client) -> None:
    """**装不出来就不写盘**（R31）。

    装配器对未知的 context、缺值的 `{{var}}`、没写 role、`selected` 不在 options
    里一律报错（S75 刻意如此）。不先跑一遍的话，用户改坏一个字，下一轮生成直接
    500，而错误信息离原因很远。
    """
    doc = client.get("/prompts/writer").json()["data"]
    doc["sections"].append({"type": "context", "role": "user", "source": "根本没有这个"})

    res = client.put("/prompts/writer", json={"data": doc})
    assert res.status_code == 422
    assert "根本没有这个" in res.json()["detail"]

    # 没写盘：读回来还是原来那份。
    assert client.get("/prompts/writer").json()["data"]["sections"][-1]["source"] == "outline"


def test_a_missing_rendering_string_is_refused(client) -> None:
    """`rendering` 少一条 = **运行时 KeyError**，而它藏在渲染函数深处。

    删掉一条不会有任何静态检查拦住，直到某一轮真的走到那一句——所以在写盘那一刻
    就拦下来（同 P29：没生效的东西不该悄悄没生效）。
    """
    doc = client.get("/prompts/director").json()["data"]
    doc["rendering"].pop("state_empty")

    res = client.put("/prompts/director", json={"data": doc})
    assert res.status_code == 422
    assert "state_empty" in res.json()["detail"]


def test_a_good_edit_lands_and_takes_effect_at_once(client, sandbox) -> None:
    """写盘之后**当轮就生效**：`load` 有进程内缓存，不清的话新内容要等重启。

    那是"改了没反应"这类问题里最难想到的一种。
    """
    doc = client.get("/prompts/writer").json()["data"]
    doc["sections"][1]["body"] = "改过的开场白。"

    assert client.put("/prompts/writer", json={"data": doc}).status_code == 200

    from agenttavern.prompting import writer_prompt

    assert any(m.content == "改过的开场白。" for m in writer_prompt().messages)
    assert "改过的开场白。" in (sandbox / "writer.yaml").read_text(encoding="utf-8")


def test_an_unknown_prompt_is_a_404(client) -> None:
    assert client.get("/prompts/nope").status_code == 404
    assert client.put("/prompts/nope", json={"data": {}}).status_code == 404


def test_complete_prompt_sets_can_be_copied_switched_renamed_and_deleted(client) -> None:
    """一套就是完整五份 YAML；切换不靠稀疏覆盖或 session 级选择（S169）。"""
    assert client.get("/prompt-sets").json() == [{"id": "default", "name": "默认", "active": True}]

    copied = client.post("/prompt-sets", json={"source_id": "default"}).json()
    assert copied["active"] is True
    assert copied["name"] == "默认 副本"

    custom = client.get("/prompts/writer").json()["data"]
    custom["sections"][1]["body"] = "只属于副本。"
    assert client.put("/prompts/writer", json={"data": custom}).status_code == 200

    assert client.put("/prompt-sets/default/active").status_code == 200
    default = client.get("/prompts/writer").json()["data"]
    assert default["sections"][1]["body"] != "只属于副本。"

    assert client.put(f"/prompt-sets/{copied['id']}/active").status_code == 200
    assert client.get("/prompts/writer").json()["data"]["sections"][1]["body"] == "只属于副本。"

    renamed = client.patch(f"/prompt-sets/{copied['id']}", json={"name": "长篇慢节奏"}).json()
    assert renamed["name"] == "长篇慢节奏"

    deleted = client.delete(f"/prompt-sets/{copied['id']}").json()
    assert deleted == {"deleted": copied["id"], "active": "default"}
    assert client.get("/prompt-sets").json() == [{"id": "default", "name": "默认", "active": True}]


def test_switching_prompt_sets_does_not_change_an_existing_turn_snapshot(client) -> None:
    """套装选择是服务级的，但已经开始的 turn 只认开始时那份完整快照（S169）。"""
    from agenttavern.prompting import call_of, capture_snapshot, using_snapshot

    default_model = call_of("writer").model
    copied = client.post("/prompt-sets", json={"source_id": "default"}).json()
    custom = client.get("/prompts/writer").json()["data"]
    custom["model"] = "只属于自定义套装的模型"
    assert client.put("/prompts/writer", json={"data": custom}).status_code == 200
    frozen = capture_snapshot()

    assert client.put("/prompt-sets/default/active").status_code == 200
    assert call_of("writer").model == default_model
    with using_snapshot(frozen):
        assert call_of("writer").model == "只属于自定义套装的模型"

    assert client.delete(f"/prompt-sets/{copied['id']}").status_code == 200


def test_saving_a_prompt_file_changes_nothing() -> None:
    """**内容未改时从界面存一次，文件一个字节都不该变**（S79）。

    这不是洁癖。保存那条路走 `yaml.safe_dump`，**转一圈之后注释全没了**，
    而且不会有任何地方说一声。这几份文件的头部曾经各带着几十行「这一份为什么
    这么摆」——用户去「调用」标签换一下模型，那几十行就消失了。

    所以那些理由搬进了 `prompting/README.md` 和读它们的那段代码。服务端先比较
    解析后的内容；相同就不写盘。这也保护用户合法的手工排版，不会仅因按一次保存
    被统一格式重排。
    """
    from agenttavern.prompting import load, save
    from agenttavern.prompting.assembler import path_of

    for name in PROMPT_FILES:
        raw = path_of(name).read_text(encoding="utf-8")
        save(name, load(name))
        assert path_of(name).read_text(encoding="utf-8") == raw, f"{name}.yaml 空保存却被改写"


# -- S159 调用旋钮：模型、流式、额外请求参数 -----------------------------------


def test_every_prompt_file_carries_its_own_call_knobs() -> None:
    """**三个旋钮按 agent 各设一份**（S159），写在那份 YAML 顶层。

    `model` 和 `stream` 以前在 `.env` 里、全局一份。上调的判据和 S80 那次是同
    一条：**它们全都在改「这一次调用长什么样」**，而那正是这份文件管的事——
    推演用贵的模型、判定用便宜的，正文流着看、状态维护不必流。

    **每一份都得写 model**：没有全局兜底了。留一个兜底的坏处很具体——某份忘了
    写，它照样跑得好好的、用着别人的那个，而没有任何地方会说一声。
    """
    from agenttavern.prompting import call_of

    for name in PROMPT_FILES:
        call = call_of(name)
        assert call.model, f"{name}.yaml 顶上没写 model"
        assert isinstance(call.extra, dict)


def test_the_knobs_take_effect_on_the_next_turn(client, sandbox) -> None:
    """保存立即更新磁盘，但已开始 turn 继续读取开始时的冻结快照。

    `extra` **原样透传**：这里存什么，请求体里就是什么。不定义"思考三档"那种
    可移植的抽象——各家拼法根本不同，定一套就要按家数映射，而猜错的症状是
    **不报错、只是没生效**（同 P29）。
    """
    from agenttavern.prompting import call_of, capture_snapshot, using_snapshot

    frozen = capture_snapshot()
    old_model = call_of("writer").model
    doc = client.get("/prompts/writer").json()["data"]
    doc["model"] = "换一个模型"
    doc["stream"] = False
    doc["extra"] = {"reasoning_effort": "high"}

    assert client.put("/prompts/writer", json={"data": doc}).status_code == 200

    with using_snapshot(frozen):
        assert call_of("writer").model == old_model

    call = call_of("writer")
    assert call.model == "换一个模型"
    assert call.stream is False
    assert call.extra == {"reasoning_effort": "high"}
    assert "换一个模型" in (sandbox / "writer.yaml").read_text(encoding="utf-8")


def test_the_interjection_marker_is_frozen_per_turn_too(client) -> None:
    """运行时结构标记属于提示词配置，不能在进程启动时被永久写死。"""
    from agenttavern.agents.replay import interjection_mark
    from agenttavern.prompting import capture_snapshot, using_snapshot

    frozen = capture_snapshot()
    old = interjection_mark()
    new = "<本轮插话>"
    doc = client.get("/prompts/director").json()["data"]
    append = doc["specials"]["append"]
    append["mark"] = new
    for section in append["sections"]:
        section["body"] = (
            section["body"]
            .replace(old, new)
            .replace(old.replace("<", "</", 1), new.replace("<", "</", 1))
        )
    assert client.put("/prompts/director", json={"data": doc}).status_code == 200

    with using_snapshot(frozen):
        assert interjection_mark() == old
    assert interjection_mark() == new


def test_a_blank_model_is_refused_at_the_call_not_silently_swapped() -> None:
    """**空模型当场报错**，不回落到任何全局值（S159）。

    这一条是那个已经删掉的 `.env` 兜底留下的反面：有兜底的时候，"某份 YAML
    忘了写模型"和"写对了"跑起来一模一样。
    """
    import pytest as _pytest

    from agenttavern.config import Settings
    from agenttavern.llm import Call, LLMError, LLMGateway, Message, Role

    gateway = LLMGateway(Settings())
    with _pytest.raises(LLMError, match="模型名"):
        import asyncio

        asyncio.run(gateway.complete([Message(role=Role.USER, content="hi")], call=Call(model="")))


# -- S147 提示词分两类：agent 与单次调用 ---------------------------------------
#
# 这两条原先在 `test_corpus_rerank.py` 里，跟着那份文件一起过来的——
# chat 模型重排删掉之后（S141）那份文件整个没了，但这两条钉的是提示词的分类，
# 和 rerank 只是碰巧同住。


def test_an_agent_is_a_noun_and_a_single_call_is_a_verb() -> None:
    """**名字就是分界线**（S147）。

    agent 是一个角色——会推演、会产出剧情、有自己的上下文与工具面，跨轮次一直
    在那儿，所以用名词。单次调用是一次动作——输入固定、输出固定、不参与剧情，
    做完就没了，所以用动词。

    `summarize` 曾经叫 `summarizer`，于是一直被算作「第五个 agent」。**那个误解
    会影响判断**：比如以为该给它工具面、以为它该读世界设定。

    这条断言钉死两份名单，是**故意的路障**：加一份 YAML 时它会红，读到这里
    才知道该用哪种词性、该进哪一类。

    **单次调用现在只剩一个成员**（`rerank` 随 S141 删掉了）。分类照留——
    它区分的是两种东西，不是两份文件。
    """
    assert set(AGENTS) == {"director", "writer", "state_manager", "goal_manager"}
    assert set(CALLS) == {"summarize"}
    assert not set(AGENTS) & set(CALLS)
    assert (*AGENTS, *CALLS) == PROMPT_FILES


def test_a_single_call_is_still_editable() -> None:
    """两类都可编辑——R31 管的是「模型会读到的字」，不分是谁在读。"""
    from agenttavern.prompting import load

    for name in CALLS:
        assert load(name).get("sections"), f"{name} 应当有可编辑的 sections"
