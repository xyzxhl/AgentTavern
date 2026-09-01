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
    settings = Settings(data_dir=tmp_path, llm_model="m")
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

    assert "断在第几条之后" in data["specials"]["segment"]["sections"][0]["body"]
    assert data["rendering"]["worldinfo_expand"] == "用 worldinfo_read 按 ref 展开。"


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
