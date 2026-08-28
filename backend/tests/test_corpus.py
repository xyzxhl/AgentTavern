"""外部资料：切块、分词、BM25、落盘（S136 / S140 / S142 / S143）。

**这一层不打任何网络。** 切块与 BM25 都是纯计算，向量那一路在别处测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttavern.corpus.bm25 import BM25Index, tokenise
from agenttavern.corpus.chunking import chunk
from agenttavern.corpus.search import search_corpus
from agenttavern.models.corpus import BuildState, Corpus
from agenttavern.storage.corpora import CorpusRepository, CorpusStore

# -- S142 递归字符切分 -------------------------------------------------------


def test_it_splits_on_paragraph_before_sentence() -> None:
    """**先试最大的自然边界**（S142）：能按段落切开就不要拆句子。"""
    text = "第一段讲甲。第一段还讲乙。\n\n第二段讲丙。第二段还讲丁。"
    chunks = chunk(text, size=20, overlap=0)

    assert any(c.text.startswith("第一段讲甲") for c in chunks)
    assert any(c.text.lstrip().startswith("第二段讲丙") for c in chunks), (
        f"段落边界应当优先于句号：{[c.text for c in chunks]}"
    )


def test_a_sentence_keeps_its_full_stop() -> None:
    """分隔符跟着**前一片**走——句子要以句号结尾，不能把句号甩到下一块开头。"""
    chunks = chunk("甲乙丙丁。戊己庚辛。壬癸子丑。", size=10, overlap=0)
    assert all(not c.text.startswith(("。", "，", "；")) for c in chunks), (
        f"标点被甩到了块首：{[c.text for c in chunks]}"
    )


def test_it_falls_back_to_characters_when_nothing_else_fits() -> None:
    """一整段没有任何分隔符的长文本仍然要切开——**最后一档是按字符**（S142）。"""
    chunks = chunk("甲" * 250, size=100, overlap=0)
    assert len(chunks) >= 3
    assert all(len(c.text) <= 100 for c in chunks)


def test_no_chunk_exceeds_the_limit() -> None:
    """限长是硬的。超了的块会让「一次检索回几段」这笔账全部失真。"""
    text = ("这是一个比较长的句子，里面有逗号，也有句号。" * 30) + "另起一段" * 40
    assert all(len(c.text) <= 120 for c in chunk(text, size=120, overlap=20))


def test_a_chunk_remembers_where_it_came_from() -> None:
    """每块记住它在原文里的字符区间——**没有它，检索结果指不回原文**（S142）。

    用户判得出这条链路准不准，靠的正是"这段是从哪儿来的"（R42）。
    """
    text = "第一句。第二句。第三句。\n\n新的一段，里面还有逗号，以及句号。"
    for c in chunk(text, size=16, overlap=4):
        assert text[c.start : c.end] == c.text, "区间必须能还原出原文"


def test_chunks_overlap_so_nothing_falls_between_them() -> None:
    """**自然边界也会切断跨句的指代**——"他把它放回原处"里的"它"在上一句（S142）。"""
    text = "甲乙丙丁戊己庚辛。壬癸子丑寅卯辰巳。午未申酉戌亥子丑。"
    chunks = chunk(text, size=20, overlap=6)
    assert len(chunks) > 1
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.start < prev.end, "相邻两块必须有交叠"


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


# -- S148 置信度：相关度不够就判为没找到 ---------------------------------------


def test_coverage_not_raw_score_decides() -> None:
    """**判据是词覆盖率，不是 BM25 原始分**（S148）。

    原始分依赖这份资料的词频分布：同一个 3.7 分在一本小说里是强命中、
    在一份术语表里可能是噪声。拿它设阈值，**换一份资料整条链路的松紧就变了，
    而没有任何地方会报错**。覆盖率是比例，到哪儿都一样。
    """
    index = BM25Index.build(["尚书省左仆射掌管吏部铨选"])
    # 分词结果分别是 [尚书省, 吏部] / [尚书省, 铨选, 天文] / [完全, 无关]，
    # **断言按实际切分写**——照想当然写会得到一个测的是分词器而不是覆盖率的测试。
    assert index.coverage("尚书省 吏部", 0) == pytest.approx(1.0)
    assert index.coverage("尚书省 铨选 天文", 0) == pytest.approx(2 / 3)
    assert index.coverage("完全无关", 0) == 0.0


def test_a_weak_match_is_reported_as_a_miss() -> None:
    """**勉强沾边的比没有更糟**（S148）：模型会把不相关的资料当成相关的用，
    写进剧情的就是一段有出处、但出处对不上的东西。没找到，它至少知道自己不知道。
    """
    index = BM25Index.build(
        ["尚书省左仆射掌管吏部铨选，凡内外文官皆属焉。", "今日天气晴朗，适合出游踏青。"]
    )
    strong = search_corpus(index, "尚书省 吏部 铨选", limit=2, min_coverage=0.5)
    weak = search_corpus(index, "量子纠缠 与 贝尔不等式 的 实验验证", limit=2, min_coverage=0.5)

    assert strong, "该找到的要找到"
    assert weak == [], f"覆盖率不够就该判为没找到，而不是端上来：{weak}"


def test_the_threshold_comes_from_config() -> None:
    """阈值是旋钮，不是写死的字面量——它要能按资料调（同 S86）。"""
    from agenttavern.config import LIMITS

    assert 0.0 < LIMITS.corpus.min_coverage <= 1.0
