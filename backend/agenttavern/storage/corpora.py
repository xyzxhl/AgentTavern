"""资料的存储（S136 / S143）。

`data/corpora/<id>/`，**不在任何 session 目录里**——一份资料要能被很多局故事挂，
放进 session 就等于每开一局复制一遍，而"改一次所有故事都跟着"正是把它独立出来
要买的东西。

**原文以纯文本落盘。** 用户可直接编辑是这个项目的取向，二进制会把这层能力藏起来。
"""

from __future__ import annotations

from pathlib import Path

from ..corpus.bm25 import BM25Index
from ..corpus.lookup import Source
from ..corpus.vectors import VectorIndex
from ..models.corpus import BuildState, BuildStatus, Chunk, Corpus
from .jsonfile import read_model, write_model


class CorpusRepository:
    """一份资料。"""

    def __init__(self, root: Path, corpus_id: str) -> None:
        self.root = root
        self.corpus_id = corpus_id

    @property
    def dir(self) -> Path:
        return self.root / "corpora" / self.corpus_id

    @property
    def meta_path(self) -> Path:
        return self.dir / "corpus.json"

    @property
    def text_path(self) -> Path:
        """原文。**纯文本，不是 JSON 里的一个字段**——几十万字塞进 JSON
        会让这个文件既打不开也改不动。
        """
        return self.dir / "text.txt"

    @property
    def chunks_path(self) -> Path:
        return self.dir / "chunks.json"

    @property
    def bm25_path(self) -> Path:
        return self.dir / "bm25.json"

    @property
    def vectors_path(self) -> Path:
        return self.dir / "vectors.npy"

    def exists(self) -> bool:
        return self.meta_path.exists()

    def load(self) -> Corpus:
        return read_model(self.meta_path, Corpus)

    def save(self, corpus: Corpus) -> None:
        write_model(self.meta_path, corpus)

    def text(self) -> str:
        return self.text_path.read_text(encoding="utf-8")

    def set_build(self, status: BuildStatus) -> Corpus:
        """更新建索引的进度。**失败也要落盘**——静默停在 building 上的话，
        用户会一直等一个永远不会好的东西（S144）。
        """
        corpus = self.load().model_copy(update={"build": status})
        self.save(corpus)
        return corpus

    # -- 索引 ---------------------------------------------------------------

    def save_chunks(self, chunks: list[Chunk]) -> None:
        self.chunks_path.write_text(
            "[\n" + ",\n".join(c.model_dump_json() for c in chunks) + "\n]",
            encoding="utf-8",
        )

    def chunks(self) -> list[Chunk]:
        import json

        if not self.chunks_path.exists():
            return []
        raw = json.loads(self.chunks_path.read_text(encoding="utf-8"))
        return [Chunk.model_validate(c) for c in raw]

    def save_bm25(self, index: BM25Index) -> None:
        index.dump(self.bm25_path)

    def bm25(self) -> BM25Index | None:
        return BM25Index.load(self.bm25_path) if self.bm25_path.exists() else None

    def vectors(self) -> VectorIndex | None:
        """向量索引。**没有是常态**——embeddings 没配时整条链路走纯 BM25（S140）。"""
        return VectorIndex.load(self.vectors_path) if self.vectors_path.exists() else None

    def source(self) -> Source | None:
        """把这份资料读成可检索的样子。**还不能查就返回 `None`。**

        「还在建 / 建失败 / 索引文件不见了」在这里合成同一个答案，因为对检索
        那一侧来说它们是同一件事：**这次查不了它**。区别（还要等多久、为什么
        失败）是给用户看的，在 `Corpus.build` 上，由界面呈现（S144）。

        **不返回一个空索引**：空索引查出来是"没找到"，而没找到和查不了是
        两回事——前者是关于这次检索的结论，后者是这份资料压根没参加（同 S139）。
        """
        corpus = self.load()
        if not corpus.build.searchable:
            return None
        index = self.bm25()
        if index is None:
            return None
        chunks = self.chunks()
        # **状态说没建向量就不读那个文件。** 换过 embedding 模型的话盘上
        # 可能还躺着一份维度对不上的旧向量，读进来只会在检索时报维度错。
        vectors = self.vectors() if corpus.build.embedded else None

        # **数量对账**（S152）：处理后的文件假定没人动，对不上就报错、不将就。
        # 判据用数量不用哈希，因为这三个数**已经在手上了**——检索路径上多算
        # 一次 600KB 的哈希是每次都要付的钱。
        #
        # 而它恰好抓得住最危险的那种不一致：块表和向量对不上时，向量那一路
        # 给出的 `chunk_index` 会索引到**不相干的段落，而且不会报错**。
        counts = {"块表": len(chunks), "BM25": index.total}
        if vectors is not None:
            counts["向量"] = vectors.count
        if len(set(counts.values())) > 1:
            raise ValueError(
                f"「{corpus.name}」的索引对不上（"
                + "、".join(f"{k} {v}" for k, v in counts.items())
                + "）。这几份文件是派生的，**重新处理一次**即可。"
            )

        return Source(
            corpus_id=corpus.corpus_id,
            name=corpus.name,
            chunks=chunks,
            bm25=index,
            vectors=vectors,
        )


class CorpusStore:
    """`data/` 底下的资料这一棵树。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def files_dir(self) -> Path:
        """**用户那一侧的文件夹**（S149）：`data/corpus_files/`。

        位置固定，不给配——多一个可配的路径就多一类「我明明放了它怎么没看见」，
        而这个功能的全部卖点就是"丢进去就行"。
        """
        return self.root / "corpus_files"

    def ensure_files_dir(self) -> Path:
        """建出来。**启动时叫一次**——一个不存在的文件夹没法往里丢东西。"""
        self.files_dir.mkdir(parents=True, exist_ok=True)
        return self.files_dir

    def repo(self, corpus_id: str) -> CorpusRepository:
        return CorpusRepository(self.root, corpus_id)

    def create(self, corpus: Corpus, *, text: str) -> CorpusRepository:
        """建一份资料。**立刻返回，索引留给后台**（S144）。

        一本三十万字的小说切几千块、每块过一次 embedding 要分钟级的时间，
        同步做会让导入看起来卡死。
        """
        repo = self.repo(corpus.corpus_id)
        repo.dir.mkdir(parents=True, exist_ok=True)
        repo.text_path.write_text(text, encoding="utf-8")
        repo.save(corpus.model_copy(update={"chars": len(text), "build": BuildStatus()}))
        return repo

    def survey(self) -> tuple[list[Corpus], list[str]]:
        """列出全部资料，**坏的点出来而不是让整个列表挂掉**（同 S72）。

        一条坏数据让整个列表列不出来，症状是「按钮点了没反应」——离原因隔着三层。
        """
        base = self.root / "corpora"
        if not base.exists():
            return [], []
        good: list[Corpus] = []
        broken: list[str] = []
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            try:
                good.append(CorpusRepository(self.root, d.name).load())
            except Exception:
                broken.append(d.name)
        return good, broken

    def delete(self, corpus_id: str) -> None:
        import shutil

        shutil.rmtree(self.repo(corpus_id).dir, ignore_errors=True)


__all__ = ["BuildState", "CorpusRepository", "CorpusStore", "Source"]
