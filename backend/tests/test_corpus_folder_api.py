"""文件夹那条路走到底：丢文件 → 界面标出来 → 用户按下 → 生效（S149–S154）。

**系统自己不动手**是这一份的主线：每一条都先确认"没按之前什么都没发生"。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ScriptedLLM
from fastapi.testclient import TestClient

from agenttavern.config import Settings
from agenttavern.models.corpus import BuildState
from agenttavern.server import create_app

TANG = "尚书省掌管六部政令，长官为尚书令。中书省负责起草诏令，长官为中书令。"


@pytest.fixture
def app(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, llm_model="m")
    with TestClient(create_app(settings, ScriptedLLM().gateway())) as client:
        yield client, tmp_path / "corpus_files"


def drop(folder: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(text.encode(encoding))
    return path


def pending(client: TestClient) -> dict[str, str]:
    return {p["source_name"]: p["kind"] for p in client.get("/corpora").json()["pending"]}


def wait_ready(client: TestClient, corpus_id: str) -> dict:
    for _ in range(200):
        corpus = client.get(f"/corpora/{corpus_id}").json()["corpus"]
        if corpus["build"]["state"] not in (BuildState.PENDING, BuildState.BUILDING):
            return corpus
    raise AssertionError("建索引一直没结束")


# -- S149 文件夹存在，位置固定 -------------------------------------------------


def test_the_source_folder_is_created_on_startup(app) -> None:
    """**一个不存在的文件夹没法往里丢东西**（S149）。"""
    client, folder = app
    assert folder.is_dir()
    assert client.get("/corpora").json()["folder"].endswith("corpus_files")


# -- S151 四种状态，按下之前什么都不发生 ---------------------------------------


def test_nothing_is_processed_until_the_user_asks(app) -> None:
    """**丢进去只是「待处理」，不会自动建**（S151）。

    自动同步等于让编辑器的每一次保存都触发一次重建。
    """
    client, folder = app
    drop(folder, "唐代官制.txt", TANG)

    assert pending(client) == {"唐代官制.txt": "new"}
    assert client.get("/corpora").json()["corpora"] == [], "还没有任何一份资料"


def test_importing_a_dropped_file_names_it_after_the_file(app) -> None:
    """**文件名就是资料的名字**（R46）——不用再填一次表。"""
    client, folder = app
    drop(folder, "唐代官制.txt", TANG)

    created = client.post("/corpora/import", json={"source_name": "唐代官制.txt"}).json()
    assert created["name"] == "唐代官制"
    assert created["source_name"] == "唐代官制.txt"
    assert created["encoding"] == "utf-8"
    assert created["source_hash"], "指纹要记下来，否则下次对不出改没改"
    assert created["build"]["state"] == BuildState.PENDING

    wait_ready(client, created["corpus_id"])
    assert pending(client) == {}, "收进来之后就不该再出现在待办里"


def test_an_edited_file_is_flagged_but_the_old_index_still_serves(app) -> None:
    """改了只是**标出来**，旧索引照旧能查（S151）。

    它对应的快照还在，区间仍然指得准——**它只是旧，不是错**。
    拒绝检索等于因为用户改了个错别字就让这局故事当场少一条检索路。
    """
    client, folder = app
    path = drop(folder, "唐代官制.txt", TANG)
    corpus_id = client.post("/corpora/import", json={"source_name": "唐代官制.txt"}).json()[
        "corpus_id"
    ]
    wait_ready(client, corpus_id)

    path.write_text(TANG + "门下省负责审核封驳。", encoding="utf-8")
    assert pending(client) == {"唐代官制.txt": "modified"}

    corpus = client.get(f"/corpora/{corpus_id}").json()["corpus"]
    assert corpus["build"]["state"] == BuildState.READY, "旧索引仍然可用"
    assert "门下省" not in client.get(f"/corpora/{corpus_id}").json()["head"], "快照还是旧的"


def test_rebuilding_pulls_the_edited_file_into_the_snapshot(app) -> None:
    """**「重新处理」= 把源文件重新读进快照，再建一遍索引**（S151）。"""
    client, folder = app
    path = drop(folder, "唐代官制.txt", TANG)
    corpus_id = client.post("/corpora/import", json={"source_name": "唐代官制.txt"}).json()[
        "corpus_id"
    ]
    wait_ready(client, corpus_id)

    path.write_text(TANG + "门下省负责审核封驳。", encoding="utf-8")
    assert client.post(f"/corpora/{corpus_id}/rebuild").status_code == 200
    wait_ready(client, corpus_id)

    assert "门下省" in client.get(f"/corpora/{corpus_id}").json()["head"]
    assert pending(client) == {}, "处理完就对上了"


def test_a_renamed_file_only_updates_the_record(app) -> None:
    """改名**只更新记录，不重建**（S151）。

    内容没变索引就还是对的，重建一遍纯属浪费那几十秒和那笔钱。
    """
    client, folder = app
    path = drop(folder, "旧名.txt", TANG)
    corpus_id = client.post("/corpora/import", json={"source_name": "旧名.txt"}).json()["corpus_id"]
    wait_ready(client, corpus_id)
    built_at = client.get(f"/corpora/{corpus_id}").json()["corpus"]["build"]

    path.rename(folder / "新名.txt")
    assert pending(client) == {"新名.txt": "renamed"}

    renamed = client.post(f"/corpora/{corpus_id}/rename", json={"source_name": "新名.txt"}).json()
    assert renamed["source_name"] == "新名.txt"
    assert renamed["name"] == "新名"
    assert renamed["build"] == built_at, "索引一个字节都没动"
    assert pending(client) == {}


def test_renaming_refuses_when_the_content_changed_too(app) -> None:
    """内容也变了**就不是改名**——让调用方走重新处理那条路。"""
    client, folder = app
    path = drop(folder, "旧名.txt", TANG)
    corpus_id = client.post("/corpora/import", json={"source_name": "旧名.txt"}).json()["corpus_id"]
    wait_ready(client, corpus_id)

    path.rename(folder / "新名.txt")
    (folder / "新名.txt").write_text("完全换了内容。", encoding="utf-8")

    res = client.post(f"/corpora/{corpus_id}/rename", json={"source_name": "新名.txt"})
    assert res.status_code == 409


def test_applying_a_deletion_removes_it_from_every_session(app) -> None:
    """源文件没了、用户确认之后，**下一次检索不该再查到它**（S151/P36）。

    索引是源的缓存，不是归档。
    """
    client, folder = app
    path = drop(folder, "唐代官制.txt", TANG)
    corpus_id = client.post("/corpora/import", json={"source_name": "唐代官制.txt"}).json()[
        "corpus_id"
    ]
    wait_ready(client, corpus_id)
    sid = client.post("/sessions").json()["id"]
    client.put(f"/sessions/{sid}/corpora/{corpus_id}")

    path.unlink()
    [change] = client.get("/corpora").json()["pending"]
    assert change["kind"] == "deleted"
    assert change["sessions"] == [sid], "**按下之前就要看到影响了哪几局**"

    assert client.delete(f"/corpora/{corpus_id}").json()["affected_sessions"] == [sid]
    assert client.get(f"/sessions/{sid}/corpora").json() == []
    assert pending(client) == {}


# -- S153 / S154 读不出来的，界面上要看得见 ------------------------------------


def test_a_gbk_file_is_imported_and_its_encoding_is_shown(app) -> None:
    """GBK 的旧 txt 一样能读，**而且要说清是按哪种编码读的**（S153）。"""
    client, folder = app
    drop(folder, "旧的.txt", TANG, encoding="gb18030")

    created = client.post("/corpora/import", json={"source_name": "旧的.txt"}).json()
    assert created["encoding"] == "gb18030"
    wait_ready(client, created["corpus_id"])
    assert TANG[:6] in client.get(f"/corpora/{created['corpus_id']}").json()["head"]


def test_an_unreadable_file_is_refused_with_a_sentence_for_the_user(app) -> None:
    """读不出来当场 400，**那句话原样给界面显示**（S153/S154）。"""
    client, folder = app
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "坏的.txt").write_bytes(b"\xff\xfe\x00\x01\xff\xff\xfe\xfe")

    res = client.post("/corpora/import", json={"source_name": "坏的.txt"})
    assert res.status_code == 400
    assert "编码" in res.json()["detail"]
    assert client.get("/corpora").json()["corpora"] == [], "读不出来就不该建出半份"


def test_an_unsupported_file_is_listed_as_such_not_hidden(app) -> None:
    """认不得的后缀**列出来标成不支持**（S154）。

    悄悄跳过的话，用户放了个 `.doc` 会一直等一个永远不出现的条目。
    """
    client, folder = app
    drop(folder, "封面.png", "x")

    [change] = client.get("/corpora").json()["pending"]
    assert change["kind"] == "unsupported"
    assert change["reason"]


def test_importing_a_file_that_is_not_there_is_a_404(app) -> None:
    client, _ = app
    assert client.post("/corpora/import", json={"source_name": "没有.txt"}).status_code == 404


# -- 两条入口互不干扰 ----------------------------------------------------------


def test_a_corpus_created_through_the_api_never_shows_as_deleted(app) -> None:
    """`POST /corpora` 建的资料**没有源文件，所以不参与对账**。

    不排除的话它会永远显示成「源已删除」——而文件夹里本来就没有它。
    """
    client, folder = app
    folder.mkdir(parents=True, exist_ok=True)
    created = client.post("/corpora", json={"name": "手工的", "text": TANG}).json()
    wait_ready(client, created["corpus_id"])

    assert created["source_name"] == ""
    assert client.get("/corpora").json()["pending"] == []
