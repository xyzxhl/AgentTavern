"""外部资料：切块、分词、BM25、落盘（S136 / S140 / S142 / S143）。

**这一层不打任何网络。** 切块与 BM25 都是纯计算，向量那一路在别处测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttavern.corpus.bm25 import BM25Index, tokenise
from agenttavern.corpus.chunking import chunk
from agenttavern.models.corpus import BuildState, Corpus
from agenttavern.storage.corpora import CorpusRepository, CorpusStore

# -- S142 切块 ----------------------------------------------------------------


def test_a_chunk_remembers_where_it_came_from() -> None:
    """每块记住它在原文里的字符区间——**没有它，检索结果指不回原文**（S142）。

    用户判得出这条链路准不准，靠的正是"这段是从哪儿来的"（R42）。
    """
    text = "甲" * 250
    chunks = chunk(text, size=100, overlap=20)

    assert all(text[c.start : c.end] == c.text for c in chunks), "区间必须能还原出原文"
    assert chunks[0].start == 0
    assert chunks[-1].end == len(text)


def test_chunks_overlap_so_nothing_falls_between_them() -> None:
    """**重叠是为了不让一句话正好被切断**——被切断的那句谁也检索不到（S142）。"""
    text = "".join(str(i % 10) for i in range(500))
    chunks = chunk(text, size=100, overlap=30)

    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start < prev.end, "相邻两块必须有交叠，否则边界上的句子会掉进缝里"
        assert prev.end - nxt.start == 30


def test_overlap_must_be_smaller_than_the_chunk() -> None:
    """重叠 ≥ 块长会让切块永远推不动——**当场报错，不要静默死循环**。"""
    with pytest.raises(ValueError):
        chunk("随便什么", size=50, overlap=50)


def test_a_short_text_is_one_chunk_not_zero() -> None:
    """比一块还短的资料仍然要有一块，否则它整份查不到。"""
    chunks = chunk("短", size=100, overlap=20)
    assert len(chunks) == 1 and chunks[0].text == "短"


# -- S140 分词 ----------------------------------------------------------------


def test_chinese_terms_survive_tokenisation() -> None:
    """**专名与术语必须整个留下来**（S140）。

    切成单字之后 BM25 的强项就没了——而这条链路要解决的恰恰是同人黑话、
    朝代官职、专业术语这一类**低频词**。
    """
    tokens = tokenise("尚书省左仆射掌管吏部")
    assert "尚书省" in tokens, f"专名被切碎了：{tokens}"
    assert "吏部" in tokens


def test_tokenisation_keeps_latin_words_whole() -> None:
    """中英混排是常态（专业资料尤其），英文词不能被按字母拆开。"""
    tokens = tokenise("用 BM25 做召回")
    assert "bm25" in [t.lower() for t in tokens]


def test_punctuation_is_not_a_token() -> None:
    """标点在每一块里都出现，留着它等于给所有块加同一个高频词，稀释信号。"""
    assert not (set(tokenise("他说：「走吧。」")) & set("：「」。，"))


# -- S140 BM25 ----------------------------------------------------------------


def test_a_rare_term_beats_a_common_one() -> None:
    """BM25 的全部价值在这里：**低频词得分高**。

    这条一旦坏掉，检索会退化成"哪块最长"，而症状只是结果变差，不会报错。
    """
    index = BM25Index.build(
        [
            "他走进屋子，屋子里很暗，屋子中央有一张桌子。",
            "他走进屋子，看见桌上放着一枚青铜獬豸符。",
            "屋子外面下着雨，屋檐滴水。",
        ]
    )
    ranked = index.search("青铜獬豸符", limit=3)
    assert ranked[0][0] == 1, f"含罕见专名的那块应当排第一：{ranked}"


def test_a_miss_returns_nothing_rather_than_everything() -> None:
    """一个词都没命中就返回空——**由调用方去说"这次没命中"**（S139）。

    索引这一层不负责措辞，但也不能编造命中。
    """
    index = BM25Index.build(["甲乙丙", "丁戊己"])
    assert index.search("完全无关的词", limit=3) == []


def test_search_never_returns_more_than_asked() -> None:
    index = BM25Index.build(["屋子" for _ in range(20)])
    assert len(index.search("屋子", limit=5)) <= 5


def test_the_index_survives_a_round_trip_through_disk(tmp_path: Path) -> None:
    """索引要能存下来再读回去，否则每次起服务都得重建（S143）。"""
    index = BM25Index.build(["青铜獬豸符在案上", "屋外下着雨"])
    before = index.search("獬豸", limit=2)

    path = tmp_path / "bm25.json"
    index.dump(path)
    assert BM25Index.load(path).search("獬豸", limit=2) == before


# -- S136 / S143 存储 ---------------------------------------------------------


def test_a_corpus_lands_on_disk_and_reads_back(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path)
    repo = store.create(Corpus(name="唐六典"), text="尚书省左仆射掌管吏部。")

    again = CorpusRepository(tmp_path, repo.corpus_id).load()
    assert again.name == "唐六典"
    assert again.chars == len("尚书省左仆射掌管吏部。")


def test_files_are_human_readable(tmp_path: Path) -> None:
    """**用户可直接编辑**是这个项目的取向（S143）——原文以纯文本落盘，不是二进制。"""
    store = CorpusStore(tmp_path)
    repo = store.create(Corpus(name="手册"), text="第一条 甲乙丙")

    assert repo.text_path.read_text(encoding="utf-8") == "第一条 甲乙丙"
    assert "手册" in repo.meta_path.read_text(encoding="utf-8")


def test_a_fresh_corpus_is_not_ready_yet(tmp_path: Path) -> None:
    """**导入立刻返回，索引在后台建**（S144）。刚建好的那一刻它还不可用。"""
    store = CorpusStore(tmp_path)
    repo = store.create(Corpus(name="小说"), text="甲" * 500)
    assert repo.load().build.state is BuildState.PENDING


def test_listing_survives_one_broken_corpus(tmp_path: Path) -> None:
    """一份坏的资料不能让整个列表列不出来——同设定库那次（S72）。"""
    store = CorpusStore(tmp_path)
    store.create(Corpus(name="好的"), text="甲")
    bad = tmp_path / "corpora" / "cp_broken"
    bad.mkdir(parents=True)
    (bad / "corpus.json").write_text("{ 这不是 json", encoding="utf-8")

    ok, broken = store.survey()
    assert [c.name for c in ok] == ["好的"]
    assert broken == ["cp_broken"], "坏的要被点出来，不是静默跳过"
