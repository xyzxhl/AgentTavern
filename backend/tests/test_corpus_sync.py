"""源文件夹与知识库的对账（S150 / S151 / S152）。

**系统自己不动手。** 这一层只回答"哪儿对不上"，动手由用户按下才发生。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttavern.corpus.sync import ChangeKind, reconcile
from agenttavern.models.corpus import BuildState, BuildStatus, Corpus
from agenttavern.storage.corpora import CorpusStore

TANG = "尚书省掌管六部政令，长官为尚书令。中书省负责起草诏令。"


def put(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def kinds(pendings) -> dict[str, ChangeKind]:
    return {p.source_name: p.kind for p in pendings}


# -- S151 四种状态 -------------------------------------------------------------


def test_a_new_file_shows_up_as_pending_not_as_a_corpus(tmp_path: Path) -> None:
    """**新文件只是「待处理」，不是一份资料**（S151）。

    直接建出来就等于自动同步——而处理一份大资料要花时间也要花钱。
    """
    folder = tmp_path / "corpus_files"
    put(folder, "唐代官制.txt", TANG)

    pending = reconcile(folder, [])
    assert kinds(pending) == {"唐代官制.txt": ChangeKind.NEW}
    assert pending[0].corpus_id == "", "还没有对应的资料"


def test_a_file_matching_its_record_is_silent(tmp_path: Path) -> None:
    """对得上就不该出现在待办里——**待办里全是要用户看一眼的东西**。"""
    folder = tmp_path / "corpus_files"
    path = put(folder, "唐代官制.txt", TANG)

    from agenttavern.corpus.sources import source_hash

    corpus = Corpus(name="唐代官制", source_name="唐代官制.txt", source_hash=source_hash(path))
    assert reconcile(folder, [corpus]) == []


def test_an_edited_file_is_flagged(tmp_path: Path) -> None:
    folder = tmp_path / "corpus_files"
    path = put(folder, "唐代官制.txt", TANG)

    from agenttavern.corpus.sources import source_hash

    corpus = Corpus(name="唐代官制", source_name="唐代官制.txt", source_hash=source_hash(path))
    path.write_text(TANG + "门下省负责审核封驳。", encoding="utf-8")

    [change] = reconcile(folder, [corpus])
    assert change.kind is ChangeKind.MODIFIED
    assert change.corpus_id == corpus.corpus_id


def test_a_missing_file_is_flagged_as_deleted(tmp_path: Path) -> None:
    """源文件没了 → **「源已删除」**，等用户确认（S151/P36）。"""
    folder = tmp_path / "corpus_files"
    folder.mkdir(parents=True)
    corpus = Corpus(name="没了", source_name="没了.txt", source_hash="abc")

    [change] = reconcile(folder, [corpus])
    assert change.kind is ChangeKind.DELETED
    assert change.corpus_id == corpus.corpus_id


def test_an_unsupported_file_is_reported_not_ignored(tmp_path: Path) -> None:
    """认不得的后缀**标出来**，不当没看见（S154）。

    悄悄跳过的话，用户放了一个 `.doc` 会一直等一个永远不出现的条目。
    """
    folder = tmp_path / "corpus_files"
    put(folder, "封面.png", "x")

    [change] = reconcile(folder, [])
    assert change.kind is ChangeKind.UNSUPPORTED
    assert change.reason, "要说清为什么不支持"


# -- S150 指纹 -----------------------------------------------------------------


def test_a_change_made_while_the_server_was_down_is_still_noticed(tmp_path: Path) -> None:
    """**这条是不装监视器的全部理由**（S150）。

    进程没跑的时候用户改了文件，事件就永远没有了——而丢了事件的后果不是
    "晚一点更新"，是索引**永久地**对应着一份已经不存在的文本，且没有任何
    地方会报错。指纹不依赖任何事件流，重启之后照样对得出来。
    """
    folder = tmp_path / "corpus_files"
    path = put(folder, "唐代官制.txt", TANG)

    from agenttavern.corpus.sources import source_hash

    corpus = Corpus(name="唐代官制", source_name="唐代官制.txt", source_hash=source_hash(path))

    # 「服务没跑」的那段时间：这里没有任何代码在观察这个文件。
    path.write_text("完全换了一份内容。", encoding="utf-8")

    assert reconcile(folder, [corpus])[0].kind is ChangeKind.MODIFIED


def test_the_verdict_comes_from_the_hash_not_from_mtime(tmp_path: Path) -> None:
    """**结论一律由 hash 下**（S150）。

    某些同步盘与容器里 mtime 不可靠，而同一秒内的两次修改可能 mtime 与 size
    都不变。这里把 mtime 和 size 都还原成改动前的样子，**内容仍然被认出来**。
    """
    import os

    folder = tmp_path / "corpus_files"
    path = put(folder, "唐代官制.txt", TANG)
    before = path.stat()

    from agenttavern.corpus.sources import source_hash

    corpus = Corpus(name="唐代官制", source_name="唐代官制.txt", source_hash=source_hash(path))

    # 同样长度、不同内容，然后把 mtime 拨回去——mtime 与 size 都骗人。
    path.write_text("尚书省掌管六部政令，长官为尚书令。中书省负责起草诏書。", encoding="utf-8")
    assert path.stat().st_size == before.st_size, "样本要同长度，否则证明不了"
    os.utime(path, (before.st_atime, before.st_mtime))

    assert reconcile(folder, [corpus])[0].kind is ChangeKind.MODIFIED


# -- 改名 ----------------------------------------------------------------------


def test_a_renamed_file_is_recognised_by_its_content(tmp_path: Path) -> None:
    """改名不算改内容：**先按路径认，认不到再按哈希认**（S149）。

    按文件名当身份的话，重命名会静默地把这份资料从每一局故事上摘下来。
    """
    folder = tmp_path / "corpus_files"
    path = put(folder, "旧名.txt", TANG)

    from agenttavern.corpus.sources import source_hash

    corpus = Corpus(name="旧名", source_name="旧名.txt", source_hash=source_hash(path))
    path.rename(folder / "新名.txt")

    [change] = reconcile(folder, [corpus])
    assert change.kind is ChangeKind.RENAMED
    assert change.corpus_id == corpus.corpus_id
    assert change.source_name == "新名.txt"


def test_renamed_and_edited_at_once_reads_as_delete_plus_add(tmp_path: Path) -> None:
    """**同时改名又改内容认不出来**——这是这套认领规则唯一的失真，写在这里。

    路径认不到、哈希也认不到，于是当成"删了一个、加了一个"。
    """
    folder = tmp_path / "corpus_files"
    path = put(folder, "旧名.txt", TANG)

    from agenttavern.corpus.sources import source_hash

    corpus = Corpus(name="旧名", source_name="旧名.txt", source_hash=source_hash(path))
    path.rename(folder / "新名.txt")
    (folder / "新名.txt").write_text("换了内容。", encoding="utf-8")

    assert kinds(reconcile(folder, [corpus])) == {
        "新名.txt": ChangeKind.NEW,
        "旧名.txt": ChangeKind.DELETED,
    }


# -- S152 处理后的文件被改了 ---------------------------------------------------


def test_a_tampered_index_is_refused_not_repaired(tmp_path: Path) -> None:
    """块表和索引对不上就**拒绝提供检索**，不试图修（S152）。

    对不上时向量那一路给出的 `chunk_index` 会索引到不相干的段落，
    **而且不会报错**——检索结果只是"变得莫名其妙"。
    """
    from agenttavern.corpus.bm25 import BM25Index
    from agenttavern.corpus.chunking import chunk

    store = CorpusStore(tmp_path)
    repo = store.create(Corpus(name="唐代官制"), text=TANG)
    chunks = chunk(TANG, size=20, overlap=4)
    repo.save_chunks(chunks)
    repo.save_bm25(BM25Index.build([c.text for c in chunks]))
    repo.set_build(BuildStatus(state=BuildState.READY, chunks_total=len(chunks)))
    assert repo.source() is not None, "先确认它本来是好的"

    # 有人动了块表：少了一条。
    repo.save_chunks(chunks[:-1])
    with pytest.raises(ValueError, match="重新处理"):
        repo.source()


def test_the_counts_must_agree_across_chunks_bm25_and_vectors(tmp_path: Path) -> None:
    """三处数量必须一致：块表、BM25、向量（S152）。"""
    from agenttavern.corpus.bm25 import BM25Index
    from agenttavern.corpus.chunking import chunk
    from agenttavern.corpus.vectors import VectorIndex

    store = CorpusStore(tmp_path)
    repo = store.create(Corpus(name="唐代官制"), text=TANG)
    chunks = chunk(TANG, size=20, overlap=4)
    repo.save_chunks(chunks)
    repo.save_bm25(BM25Index.build([c.text for c in chunks]))
    # 向量少一条——这正是"换了 embedding 模型没重建"会留下的样子。
    VectorIndex.build([[1.0, 0.0]] * (len(chunks) - 1)).dump(repo.vectors_path)
    repo.set_build(BuildStatus(state=BuildState.READY, embedded=True))

    with pytest.raises(ValueError, match="重新处理"):
        repo.source()
