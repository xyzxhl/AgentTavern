"""资料的导入、建索引、挂载（S136 / S143 / S144）。

这一份钉的是**人这一侧**：切片 4 之前模型能查，但用户导不进东西。

一条贯穿的原则：**「还没好」和「没有」必须分得开**。
返回空列表、静默跳过、永远停在 building——三种都会被读成「查过了，没有」，
而它们全都是「这次根本没查」（同 S139 那条罪）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import ScriptedLLM
from fastapi.testclient import TestClient

from agenttavern.config import Settings
from agenttavern.corpus.build import build_index, sweep_stale
from agenttavern.models.corpus import BuildState, BuildStatus, Corpus
from agenttavern.server import create_app
from agenttavern.storage import Store
from agenttavern.storage.corpora import CorpusStore

TEXT = (
    "尚书省掌管六部政令，长官为尚书令。"
    "中书省负责起草诏令，长官为中书令。"
    "门下省负责审核封驳，长官为侍中。给事中有封驳之权。"
)


class FakeEmbedder:
    """按批次记账的替身。**记下每批多大**——分批本身是要被钉住的行为。"""

    def __init__(self, *, fails: bool = False, dim: int = 4) -> None:
        self.fails = fails
        self.dim = dim
        self.batches: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        if self.fails:
            raise RuntimeError("端点不可用")
        return [[float(len(t) % 7), 1.0, 0.0, 0.0] for t in texts]


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings, ScriptedLLM().gateway())) as c:
        yield c


def import_corpus(client: TestClient, name: str = "唐代官制", text: str = TEXT) -> str:
    res = client.post("/corpora", json={"name": name, "text": text})
    assert res.status_code == 200, res.text
    return res.json()["corpus_id"]


def wait_ready(client: TestClient, corpus_id: str) -> dict:
    """轮到不再是 building 为止。

    **TestClient 里后台任务是在请求之间跑的**（同一个事件循环），所以这里
    发几次请求就等于让出几次控制权——不必 sleep，也不该 sleep。
    """
    for _ in range(200):
        corpus = client.get(f"/corpora/{corpus_id}").json()["corpus"]
        if corpus["build"]["state"] not in (BuildState.PENDING, BuildState.BUILDING):
            return corpus
    raise AssertionError("建索引一直没结束")


# -- S144 导入立刻返回 ---------------------------------------------------------


def test_import_returns_before_the_index_is_built(client) -> None:
    """**导入立刻返回，索引在后台建**（S144）。

    一本三十万字的小说切几千块、每块过一次 embedding 要分钟级——同步做的话
    导入看起来就是卡死了。

    返回的状态是 `pending`，**不是骗人的 `ready`**：这一刻它确实还查不了，
    而界面要照着这个状态显示进度。
    """
    res = client.post("/corpora", json={"name": "唐代官制", "text": TEXT})
    assert res.json()["build"]["state"] == BuildState.PENDING
    assert res.json()["chars"] == len(TEXT), "原文当场落盘，字数当场算得出"

    assert wait_ready(client, res.json()["corpus_id"])["build"]["state"] == BuildState.READY


def test_progress_is_visible_while_building(client, tmp_path: Path) -> None:
    """**进度看得见**（S144）：`chunks_done / chunks_total`。

    没有进度的等待和卡死分不出来——用户唯一能做的判断就是"它还在动吗"。
    """
    corpus = wait_ready(client, import_corpus(client))
    build = corpus["build"]
    assert build["chunks_total"] > 0, "切了几块要说出来"
    assert build["chunks_done"] == build["chunks_total"]


async def test_a_failed_build_is_visible_not_silent(tmp_path: Path) -> None:
    """建砸了要**看得见**，不能静默停在 building 上（S144）。

    停在 building 上的话用户会一直等一个不会好的东西，而失败至少能按重建。
    """
    repo = CorpusStore(tmp_path).create(Corpus(name="坏的"), text=TEXT)
    repo.text_path.unlink()  # 原文没了 —— 切块那一步必炸

    corpus = await build_index(repo)
    assert corpus.build.state is BuildState.FAILED
    assert corpus.build.error, "**必须说清为什么**，否则用户只看到一个红点"
    assert repo.load().build.state is BuildState.FAILED, "失败也要落盘"


# -- 建索引的分工：哪些失败算失败 ---------------------------------------------


async def test_embeddings_failing_still_leaves_a_searchable_corpus(tmp_path: Path) -> None:
    """**embeddings 挂掉不算建失败**（S140）。

    BM25 不依赖任何外部模型，它成了这份资料就能查。把 embedding 失败当成建失败，
    后果是**一次网络抖动让整份资料变成不可用**，而它本来有一半能用。
    """
    repo = CorpusStore(tmp_path).create(Corpus(name="半截"), text=TEXT)
    corpus = await build_index(repo, embedder=FakeEmbedder(fails=True))

    assert corpus.build.state is BuildState.READY, "BM25 建成了就能查"
    assert corpus.build.embedded is False
    assert corpus.build.error, "降级的理由要留给用户看"
    assert repo.source() is not None, "查得了"
    assert repo.source().vectors is None, "但没有向量那一路"


async def test_a_half_written_vector_file_is_removed(tmp_path: Path) -> None:
    """向量那一路半路挂掉时**把半截文件删掉**。

    留着的话下次读到的是一个条数和块数对不上的矩阵，而那**要等到检索时才炸**
    ——离原因隔着一次重启。
    """
    repo = CorpusStore(tmp_path).create(Corpus(name="半截"), text=TEXT)
    repo.vectors_path.write_bytes("上一次留下的垃圾".encode())

    await build_index(repo, embedder=FakeEmbedder(fails=True))
    assert not repo.vectors_path.exists()


async def test_embedding_goes_in_batches(tmp_path: Path) -> None:
    """**分批发**（`corpus.embed_batch`）。

    端点有每次的条数上限（实测 DashScope 是 20），一次性发几千块会被拒——
    而那个拒绝发生在建索引跑到一半的时候。
    """
    long_text = TEXT * 20
    repo = CorpusStore(tmp_path).create(Corpus(name="长的"), text=long_text)
    embedder = FakeEmbedder()

    corpus = await build_index(repo, embedder=embedder, batch=3)

    assert corpus.build.embedded is True
    assert max(embedder.batches) <= 3, "不能超过批次上限"
    assert sum(embedder.batches) == corpus.build.chunks_total, "一块都不能漏"


# -- S143 索引落盘 -------------------------------------------------------------


async def test_the_index_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    """建完关掉、下次起来还能查（S143）。

    每次起服务重建一遍索引的话，embedding 那笔钱要反复付。
    """
    repo = CorpusStore(tmp_path).create(Corpus(name="唐代官制"), text=TEXT)
    await build_index(repo, embedder=FakeEmbedder())

    # **换一个全新的 Store 对象**：模拟重启，不复用任何进程内状态。
    again = CorpusStore(tmp_path).repo(repo.corpus_id)
    source = again.source()
    assert source is not None
    assert source.bm25.total == len(again.chunks())
    assert source.vectors is not None and source.vectors.count == source.bm25.total


async def test_files_are_human_readable(tmp_path: Path) -> None:
    """**用户可直接编辑**是这个项目的取向（S143）——原文纯文本，块表是 JSON。

    向量是 `.npy`，那一份不可读也没关系：它是**可以从原文重新生成的**，
    而原文和块表不是。
    """
    repo = CorpusStore(tmp_path).create(Corpus(name="唐代官制"), text=TEXT)
    await build_index(repo, embedder=FakeEmbedder())

    assert repo.text_path.read_text(encoding="utf-8") == TEXT
    chunks = json.loads(repo.chunks_path.read_text(encoding="utf-8"))
    assert chunks and all("text" in c and "start" in c for c in chunks)
    assert "尚书省" in repo.chunks_path.read_text(encoding="utf-8"), "中文不能被转义成 \\uXXXX"


# -- 进程中途死掉 -------------------------------------------------------------


def test_a_build_orphaned_by_a_restart_is_marked_failed(tmp_path: Path) -> None:
    """**没人在建的 `building` 标成失败**（S144）。

    进程在建索引途中被杀（改代码重启、机器关机），状态会永远停在 building 上。
    **那比失败更糟**：永远的"建索引中 47%"会让用户一直等，而失败至少能按重建。
    """
    store = CorpusStore(tmp_path)
    repo = store.create(Corpus(name="没建完"), text=TEXT)
    repo.set_build(BuildStatus(state=BuildState.BUILDING, chunks_total=9, chunks_done=4))

    assert sweep_stale(store) == [repo.corpus_id]
    build = repo.load().build
    assert build.state is BuildState.FAILED
    assert "重建" in build.error, "要说清下一步能做什么"


def test_sweeping_leaves_healthy_corpora_alone(tmp_path: Path) -> None:
    """已经建好的、还没开始的，都不动。"""
    store = CorpusStore(tmp_path)
    ready = store.create(Corpus(name="好的"), text=TEXT)
    ready.set_build(BuildStatus(state=BuildState.READY))
    pending = store.create(Corpus(name="排队的"), text=TEXT)

    assert sweep_stale(store) == []
    assert ready.load().build.state is BuildState.READY
    assert pending.load().build.state is BuildState.PENDING


# -- S136 一级资产：挂载、共享、删除 -------------------------------------------


def test_a_corpus_can_be_attached_to_several_sessions(client) -> None:
    """**一份资料可以被很多局故事挂**（S136）。

    这是把它独立出来要买的东西：改一次、所有故事都跟着。
    """
    corpus_id = import_corpus(client)
    wait_ready(client, corpus_id)
    one = client.post("/sessions").json()["id"]
    two = client.post("/sessions").json()["id"]

    for sid in (one, two):
        res = client.put(f"/sessions/{sid}/corpora/{corpus_id}")
        assert res.status_code == 200
        assert [c["corpus_id"] for c in res.json()] == [corpus_id]

    assert set(client.get(f"/corpora/{corpus_id}").json()["sessions"]) == {one, two}


def test_a_session_can_hold_several_corpora(client) -> None:
    """反过来也成立：**一局故事挂零到多份**（S136）。"""
    sid = client.post("/sessions").json()["id"]
    assert client.get(f"/sessions/{sid}/corpora").json() == [], "零份是正常状态"

    first, second = import_corpus(client, "唐代官制"), import_corpus(client, "海船考")
    client.put(f"/sessions/{sid}/corpora/{first}")
    attached = client.put(f"/sessions/{sid}/corpora/{second}").json()
    assert {c["corpus_id"] for c in attached} == {first, second}


def test_attaching_twice_is_a_no_op(client) -> None:
    """重复挂不报错，也不挂两遍——挂两遍的话检索会把同一段返回两次。"""
    sid = client.post("/sessions").json()["id"]
    corpus_id = import_corpus(client)
    client.put(f"/sessions/{sid}/corpora/{corpus_id}")
    assert len(client.put(f"/sessions/{sid}/corpora/{corpus_id}").json()) == 1


def test_attaching_a_corpus_that_does_not_exist_is_refused(client) -> None:
    """**挂一份不存在的当场拒绝**（P29）。

    留下一条悬空 id 的话，列表会宽容地跳过它，于是用户看到"挂上了"
    而检索永远查不到它——**一个没生效的操作不该悄悄没生效**。
    """
    sid = client.post("/sessions").json()["id"]
    assert client.put(f"/sessions/{sid}/corpora/cp_nope").status_code == 404
    assert client.get(f"/sessions/{sid}/corpora").json() == []


def test_detaching_leaves_the_corpus_itself_alone(client) -> None:
    """摘下来**只改这局故事**——资料是一级资产，别的故事可能还在用（S136）。"""
    corpus_id = import_corpus(client)
    one = client.post("/sessions").json()["id"]
    two = client.post("/sessions").json()["id"]
    client.put(f"/sessions/{one}/corpora/{corpus_id}")
    client.put(f"/sessions/{two}/corpora/{corpus_id}")

    assert client.delete(f"/sessions/{one}/corpora/{corpus_id}").json() == []
    assert client.get(f"/corpora/{corpus_id}").json()["sessions"] == [two]


def test_deleting_a_corpus_in_use_says_who_is_affected_and_proceeds(client) -> None:
    """**有故事挂着也照删，只把受影响的报回去**（S136）。

    和设定库不对称是有意的：设定是故事的地基（删了角色、初始状态、世界全没），
    资料只是可选的增援（删了少一条检索路，故事照跑）。**代价差着一档，
    闸门的松紧就该不一样。**

    而且删除多半是从文件夹那一侧发起的——用户已经把源文件删了，
    再拦一次是替他做决定。**界面负责在按下之前把这几局摆出来。**
    """
    corpus_id = import_corpus(client)
    sid = client.post("/sessions").json()["id"]
    client.put(f"/sessions/{sid}/corpora/{corpus_id}")

    res = client.delete(f"/corpora/{corpus_id}")
    assert res.status_code == 200
    assert res.json()["affected_sessions"] == [sid], "要说清影响了哪几局"
    assert client.get(f"/corpora/{corpus_id}").status_code == 404


def test_deleting_a_corpus_detaches_it_from_every_session(client) -> None:
    """删完要把这个 id 从每一局的 `corpus_ids` 里摘掉（S136）。

    留着悬空 id 的话，列表会宽容地跳过它（同 S72）——**那就成了静默的缺失**，
    用户看不出自己少了一条检索路。
    """
    corpus_id = import_corpus(client)
    one = client.post("/sessions").json()["id"]
    two = client.post("/sessions").json()["id"]
    for sid in (one, two):
        client.put(f"/sessions/{sid}/corpora/{corpus_id}")

    assert set(client.delete(f"/corpora/{corpus_id}").json()["affected_sessions"]) == {one, two}

    # **断言 `corpus_ids` 本身，不是那个列表接口。**
    # `corpora()` 会宽容地跳过悬空 id（同 S72），于是摘没摘干净它都返回空——
    # 断言在那层宽容之后，等于什么都没钉住。**这条是变异测试抓出来的。**
    for sid in (one, two):
        assert client.get(f"/sessions/{sid}").json()["session"]["corpus_ids"] == []
        assert client.get(f"/sessions/{sid}/corpora").json() == []


# -- 列表的宽容（同 S72） ------------------------------------------------------


def test_a_broken_corpus_is_reported_not_swallowed(client, tmp_path: Path) -> None:
    """**一份读不出来的资料不该让整张列表 500**，但也不能悄悄跳过。

    500 的症状是"点了没反应"，离原因隔着三层；悄悄跳过更糟——
    用户根本不知道自己丢了什么（P13）。
    """
    good = import_corpus(client)
    broken_dir = tmp_path / "corpora" / "cp_broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "corpus.json").write_text("{ 这不是 JSON", encoding="utf-8")

    body = client.get("/corpora").json()
    assert [c["corpus"]["corpus_id"] for c in body["corpora"]] == [good]
    assert body["broken"] == ["cp_broken"], "坏的要点名"


def test_the_listing_says_who_is_using_each_corpus(client) -> None:
    """列表带上「谁在用」——**删之前必须看得见这件事**（S136）。"""
    corpus_id = import_corpus(client)
    sid = client.post("/sessions").json()["id"]
    client.put(f"/sessions/{sid}/corpora/{corpus_id}")

    [row] = client.get("/corpora").json()["corpora"]
    assert row["sessions"] == [sid]


# -- 改名与重建 ---------------------------------------------------------------


def test_renaming_does_not_touch_the_text(client) -> None:
    """**原文不给改**：改了它盘上的索引就对不上了。

    块的区间会指到别的字上，而检索仍然照跑——**引文静默地指错位置**。
    """
    corpus_id = import_corpus(client)
    wait_ready(client, corpus_id)

    res = client.patch(f"/corpora/{corpus_id}", json={"name": "改过的", "note": "从某处抄的"})
    assert (res.json()["name"], res.json()["note"]) == ("改过的", "从某处抄的")
    assert client.get(f"/corpora/{corpus_id}").json()["head"] == TEXT[:500]


def test_rebuild_recovers_a_failed_corpus(client, tmp_path: Path) -> None:
    """**重建是建失败之后唯一的出路**，也用于换了 embedding 模型之后。

    换模型不重建的话，盘上的向量维度和新模型对不上，那个错要等到下一次检索才炸。
    """
    corpus_id = import_corpus(client)
    wait_ready(client, corpus_id)

    repo = CorpusStore(tmp_path).repo(corpus_id)
    repo.set_build(BuildStatus(state=BuildState.FAILED, error="上次没跑完"))

    assert client.post(f"/corpora/{corpus_id}/rebuild").status_code == 200
    assert wait_ready(client, corpus_id)["build"]["state"] == BuildState.READY


def test_an_unknown_corpus_is_a_404(client) -> None:
    assert client.get("/corpora/cp_nope").status_code == 404
    assert client.delete("/corpora/cp_nope").status_code == 404
    assert client.patch("/corpora/cp_nope", json={"name": "x"}).status_code == 404
    assert client.post("/corpora/cp_nope/rebuild").status_code == 404


def test_an_empty_import_is_refused(client) -> None:
    """空原文当场拒绝——它建出来的是一份永远查不到东西的资料。"""
    assert client.post("/corpora", json={"name": "空的", "text": ""}).status_code == 422


# -- 接上切片 4：导进来的资料真的能被查到 --------------------------------------


def test_an_imported_corpus_becomes_searchable_by_the_director(client, tmp_path: Path) -> None:
    """**这条把两个切片接起来**：导入 → 建索引 → 工具查得到。

    切片 4 之前模型能查但用户导不进东西；这一条证明那道缝合上了。
    """
    from agenttavern.tools import build_tools
    from agenttavern.tools.corpus import corpus_tool

    corpus_id = import_corpus(client)
    wait_ready(client, corpus_id)
    sid = client.post("/sessions").json()["id"]
    client.put(f"/sessions/{sid}/corpora/{corpus_id}")

    repo = Store(tmp_path).session(sid)
    assert "corpus_search" in {t.name for t in build_tools(corpora=repo.corpora())}
    assert [s.corpus_id for s in repo.corpus_sources()] == [corpus_id]
    assert corpus_tool().name == "corpus_search"


def test_a_corpus_still_building_is_attached_but_not_yet_searchable(client, tmp_path: Path) -> None:
    """挂上了但还没建好：**列得出来，查不了**（S144）。

    两件事必须分开——列不出来会让用户以为没挂上；而 `corpus_sources` 放它进去，
    检索就会拿一份空索引查，查出来的"没找到"是假的。
    """
    corpus_id = import_corpus(client)
    sid = client.post("/sessions").json()["id"]
    client.put(f"/sessions/{sid}/corpora/{corpus_id}")

    repo = Store(tmp_path).session(sid)
    CorpusStore(tmp_path).repo(corpus_id).set_build(BuildStatus(state=BuildState.BUILDING))

    assert [c.corpus_id for c in repo.corpora()] == [corpus_id], "列得出来"
    assert repo.corpus_sources() == [], "但查不了"
