"""`corpus_search` 接进 director（S137 / S138 / S139 / S144 / S145 / S146）。

这一份钉的是**回执**——这条链路成败的地方。回执只有两种形状：

| | |
|---|---|
| **找到了** | 那几段原文 + 各自在哪份资料的第几字 |
| **这次没找到** | 一句「这次没找到」+ 用的 query |

**没有第三种。** 尤其不附「这可能不全，仅供参考」：那是免责声明不是机制（S139）。
"""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from conftest import using

from agenttavern.config import LIMITS, Settings
from agenttavern.corpus.bm25 import BM25Index
from agenttavern.llm import LLMGateway
from agenttavern.models import Session
from agenttavern.models.corpus import BuildState, BuildStatus, Chunk, Corpus
from agenttavern.prompting import load, rendering
from agenttavern.storage import Store
from agenttavern.storage.corpora import CorpusStore
from agenttavern.tools import RunContext, build_state_tools, build_tools, goal_tools
from agenttavern.tools.corpus import corpus_tool

# -- 素材 ---------------------------------------------------------------------

TANG = [
    "尚书省掌管六部政令，长官为尚书令。",
    "中书省负责起草诏令，长官为中书令。",
    "门下省负责审核封驳，长官为侍中。",
]
"""三块，词面互不重叠——**查得到和查不到都要能干净地做出来**。

「长官」三块都有（三条候选，rerank 会被调用），「六部」只有第一块，
「长安城墙」一块都没有。
"""

SHIP = ["福船水密隔舱以樟木为板。", "沙船平底适宜北洋，长官不管这个。"]


def _weave(pieces: list[str]) -> tuple[str, list[Chunk]]:
    """把几段拼成原文，**顺带算出各自的区间**。

    位置是自己算的而不是事后 `find()` 出来的：重复的段落会让 `find` 落在
    第一次出现的地方，而那种错位不会报错，只会让引用指错位置（S142 同理）。
    """
    text, chunks, at = "", [], 0
    for i, piece in enumerate(pieces):
        chunks.append(Chunk(index=i, start=at, end=at + len(piece), text=piece))
        text += piece
        at += len(piece)
    return text, chunks


def make_corpus(root: Path, name: str, pieces: list[str], *, ready: bool = True) -> Corpus:
    text, chunks = _weave(pieces)
    repo = CorpusStore(root).create(Corpus(name=name), text=text)
    repo.save_chunks(chunks)
    repo.save_bm25(BM25Index.build([c.text for c in chunks]))
    return repo.set_build(
        BuildStatus(
            state=BuildState.READY if ready else BuildState.BUILDING,
            chunks_total=len(chunks),
            chunks_done=len(chunks) if ready else 0,
        )
    )


def gateway(reply: str = "0,1,2") -> LLMGateway:
    """rerank 那一次调用的替身。默认原样保留次序。"""

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return LLMGateway(Settings(llm_model="m"), http_client=client)


class BrokenEmbedder:
    """端点挂了。**降级路径要被真的走一遍**，不能只在单测里假设它会发生。"""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("端点不可用")


def context(tmp_path: Path, *corpora: Corpus, reply: str = "0,1,2", embedder=None) -> RunContext:
    repo = Store(tmp_path).create_session(Session(corpus_ids=[c.corpus_id for c in corpora]))
    return RunContext(
        run_id="run_test",
        repo=repo,
        session=repo.load_session(),
        gateway=gateway(reply),
        embedder=embedder,
    )


async def search(ctx: RunContext, query: str):
    return await corpus_tool().invoke(ctx, {"query": query})


# -- S137 / S138 只有 director 拿得到 -----------------------------------------


def test_corpus_search_is_only_in_the_director_tools() -> None:
    """**只出现在 director 的工具面里**（S137）。

    检索到的东西到达 writer 只有一条路——**director 把它写进细纲**。
    """
    attached = [Corpus(name="占位")]
    assert "corpus_search" in {t.name for t in build_tools(corpora=attached)}
    assert "corpus_search" not in {t.name for t in build_state_tools()}
    assert "corpus_search" not in {t.name for t in goal_tools()}


def test_writer_never_sees_corpus_hits() -> None:
    """**writer 的 context 一条都不增加**（S137）。

    它仍然只有角色语言习惯、正文上文、本轮细纲。资料原文是一份现成的文风范本，
    摆在 writer 眼前它很可能跟着那个腔调走——**而那不是用户选的文风**（S15）。

    顺带钉住 writer **一个工具都没有**：这是它是一次干净调用的前提。
    """
    sources = {s.get("source") for s in load("writer")["sections"] if s.get("type") == "context"}
    assert sources == {"character_voices", "upstream", "outline"}
    assert not load("writer").get("tools"), "writer 一个工具都不该有"


def test_the_state_manager_has_no_corpus_tool() -> None:
    """state_manager 的活是**照着刚写出来的正文记账**，不是了解世界（S138）。

    术语在正文里怎么写，它就怎么记。这和它不读 worldinfo 是同一条理由。
    """
    assert {t.name for t in build_state_tools()} <= {"state_write", "state_query"}


def test_the_goal_manager_has_no_corpus_tool() -> None:
    """goal_manager 管的是「推到哪儿了」，**一条检索路都没有**（S138）。"""
    assert "corpus_search" not in {t.name for t in goal_tools()}


# -- S145 关掉 / 没挂就不注册 --------------------------------------------------


def test_the_corpus_tool_disappears_when_switched_off() -> None:
    """开关在 `director.yaml` 的 `tools.corpus_search.enabled`，同其它工具（S145）。"""
    attached = [Corpus(name="占位")]
    with using(tools={"director": {"corpus_search": False}}):
        assert "corpus_search" not in {t.name for t in build_tools(corpora=attached)}


def test_no_corpus_attached_means_no_tool() -> None:
    """**没挂资料时同样不注册**（S145）。

    注册了但没东西可查的工具白占提示词体量、还会被试着调一次、失败还要消耗
    一轮往返（同 S21）。
    """
    assert "corpus_search" not in {t.name for t in build_tools()}
    assert "corpus_search" not in {t.name for t in build_tools(corpora=[])}


def test_the_tool_stays_while_a_corpus_is_still_building() -> None:
    """挂了但还在建索引时**工具要在**（S144/S145）。

    不注册的话模型只会觉得这局故事根本没有资料可查；而工具在，它才说得出
    「这份资料还在建索引」——那是两件不同的事。
    """
    building = Corpus(name="还在建", build=BuildStatus(state=BuildState.BUILDING))
    assert "corpus_search" in {t.name for t in build_tools(corpora=[building])}


# -- S139 回执只有两种形状 -----------------------------------------------------


async def test_a_hit_carries_where_it_came_from(tmp_path: Path) -> None:
    """找到了 → **那几段原文 + 各自在资料里的位置**（S139/R42）。

    位置不是装饰：用户是唯一判得出这条链路准不准的人，而他要能翻回原文去对。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    result = await search(context(tmp_path, corpus), "尚书省 六部")

    assert "尚书省掌管六部政令" in result.content
    assert "唐代官制" in result.content, "得说清是哪一份资料"
    assert "0" in result.content and "17" in result.content, "得说清在第几字"

    [returned] = result.payload["corpus"]["returned"]
    assert (returned["start"], returned["end"]) == (0, 17)


async def test_a_hit_points_back_into_the_original_text(tmp_path: Path) -> None:
    """回执里报的区间**必须真的切得出那一段**。

    切块那一层已经保证 `text[start:end] == chunk.text`（S142），这里钉的是
    它一路传到回执上没有走样——中间任何一次重排都可能把位置配到别的段上。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    ctx = context(tmp_path, corpus)
    result = await search(ctx, "起草 诏令")

    text = CorpusStore(tmp_path).repo(corpus.corpus_id).text()
    for hit in result.payload["corpus"]["returned"]:
        assert text[hit["start"] : hit["end"]] in result.content


async def test_the_query_is_echoed_back(tmp_path: Path) -> None:
    """没找到时**要说清查的是什么**（S139）。

    否则模型只知道"失败了"，不知道是不是自己问法不对——而换个说法再查一次
    正是它此时唯一能做的事。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    result = await search(context(tmp_path, corpus), "长安城墙")
    assert "长安城墙" in result.content


async def test_a_miss_says_the_search_failed_not_that_the_corpus_lacks_it(
    tmp_path: Path,
) -> None:
    """**「这次检索没找到」，不是「资料里没有」**（S139）。

    前者是系统对自己这次动作的陈述，后者是对资料的断言——而查得到查不到取决于
    query 问得对不对，这条链路**没有资格下那个断言**。

    钉法是：没找到时的回执**整个来自 `rendering`**，代码一个字都不拼。措辞归
    用户（R31），而代码这一侧没有地方能偷偷加一句关于资料的断言。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    result = await search(context(tmp_path, corpus), "长安城墙")

    assert result.content == rendering("director", "corpus_miss", query="长安城墙")
    assert not result.is_error, "没找到不是错误——它是一个正常的结论"
    assert result.payload["corpus"]["returned"] == []


async def test_no_disclaimer_is_appended_to_a_hit(tmp_path: Path) -> None:
    """回执里**只有找到的那几段**，没有第三种东西（S139/S140）。

    这一条特意在**降级**的情况下测：向量那一路挂了，回执照旧只有那几段——
    「这次只用了关键词匹配」改变不了模型下一步做什么，只会多占上下文。
    它进 trace 和界面，**用户看到它才知道该去配 embeddings**。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    ctx = context(tmp_path, corpus, embedder=BrokenEmbedder())
    result = await search(ctx, "尚书省 六部")

    expected = "\n\n".join(
        [
            rendering("director", "corpus_hit", count=1),
            rendering("director", "corpus_passage", name="唐代官制", start=0, end=17, text=TANG[0]),
        ]
    )
    assert result.content == expected, "多出来的任何一句都是免责声明"
    assert result.payload["corpus"]["degraded"], "降级本身必须被记下来，只是不给模型"


# -- S144 还在建索引 -----------------------------------------------------------


async def test_searching_an_unbuilt_corpus_says_so_instead_of_returning_nothing(
    tmp_path: Path,
) -> None:
    """还在建索引的那份**要说出来**（S144）。

    静默略过会被读成「查过了，没有」——**和 S139 是同一条罪**：它压根没参加
    这次检索，而模型无从知道。
    """
    ready = make_corpus(tmp_path, "唐代官制", TANG)
    building = make_corpus(tmp_path, "海船考", SHIP, ready=False)

    result = await search(context(tmp_path, ready, building), "尚书省 六部")

    assert "海船考" in result.content
    assert result.payload["corpus"]["not_ready"] == ["海船考"]
    # 建好的那份照常查——**一份没好不该拖垮其余几份**。
    assert "尚书省掌管六部政令" in result.content


async def test_an_unbuilt_corpus_alone_is_not_reported_as_a_miss(tmp_path: Path) -> None:
    """只挂了一份、而它还在建时，回执**既说没找到、也说它还在建**。

    「这次没找到」是对已经查过的那些说的；那一份根本没参加，两句话都得在。
    """
    building = make_corpus(tmp_path, "海船考", SHIP, ready=False)
    result = await search(context(tmp_path, building), "水密隔舱")

    assert rendering("director", "corpus_miss", query="水密隔舱") in result.content
    assert "海船考" in result.content


# -- S146 两个阶段都进 trace ---------------------------------------------------


async def test_a_search_lands_in_the_trace_with_both_stages(tmp_path: Path) -> None:
    """**rerank 前后的次序都要记**（S146）。

    它到底有没有起作用，是这条链路第一个要量的东西——两边都记下来才比得出。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    # 模型把第三块提到最前面：**次序确实被改动过**，两份记录因此不同。
    ctx = context(tmp_path, corpus, reply="2,0,1")
    result = await search(ctx, "长官")

    trace = result.payload["corpus"]
    assert trace["query"] == "长官"
    [path] = trace["paths"]
    assert path["name"] == "唐代官制"
    assert path["lexical"] == [0, 1, 2], "BM25 那一路的候选要单独记"
    assert path["semantic"] == [], "没配 embeddings 时向量那一路是空的"

    assert [i for _, i in trace["before_rerank"]] == [0, 1, 2]
    assert [i for _, i in trace["after_rerank"]] == [2, 0, 1]
    assert trace["before_rerank"] != trace["after_rerank"], "两份记录必须能分辨"


async def test_candidates_from_several_corpora_are_reranked_together(tmp_path: Path) -> None:
    """几份资料的候选**合起来排一次**，不是各排各的再拼。

    分别 rerank 再拼的话，每一份都会贡献它自己的前几名，于是「三份资料」
    自动变成「三倍的结果」，而其中两份可能根本不相关。
    """
    tang = make_corpus(tmp_path, "唐代官制", TANG)
    ship = make_corpus(tmp_path, "海船考", SHIP)

    ctx = context(tmp_path, tang, ship, reply="0,1,2,3")
    result = await search(ctx, "长官")

    returned = result.payload["corpus"]["returned"]
    assert {h["name"] for h in returned} == {"唐代官制", "海船考"}, "候选要在同一次排序里"
    # 块序号只在一份资料之内唯一，**所以每条候选都得带上是哪一份**。
    assert len({(h["corpus_id"], h["chunk_index"]) for h in returned}) == len(returned)


async def test_rerank_sees_more_candidates_than_it_returns(tmp_path: Path) -> None:
    """**截断留给 rerank**：两路各取 `candidates` 条，它才在里面挑 `top_k` 条。

    在它之前就砍到 `top_k` 的话，rerank 只能在已经排好的那几条里换顺序——
    它本来要做的事（把第 12 条提到第 1 位）就**永远发生不了**，而这条链路
    看起来一切正常，只是它那次调用完全白花。
    """
    pieces = [f"第{i}处衙署的长官各有职掌。" for i in range(8)]
    corpus = make_corpus(tmp_path, "官署录", pieces)
    ctx = context(tmp_path, corpus, reply=",".join(str(i) for i in range(8)))

    trace = (await search(ctx, "长官")).payload["corpus"]
    assert len(pieces) > LIMITS.corpus.top_k, "素材要比 top_k 多，否则这条测不出东西"
    assert len(trace["before_rerank"]) == len(pieces), "rerank 该看到全部候选"
    assert len(trace["after_rerank"]) == LIMITS.corpus.top_k


async def test_a_dangling_corpus_id_does_not_break_the_run(tmp_path: Path) -> None:
    """挂着的资料被删掉之后，**这局故事还得能跑**（同 S72）。

    一条悬空 id 让整局开不起来的话，症状是"点了没反应"，离原因隔着三层。
    """
    corpus = make_corpus(tmp_path, "唐代官制", TANG)
    ctx = context(tmp_path, corpus)
    CorpusStore(tmp_path).delete(corpus.corpus_id)

    result = await search(ctx, "尚书省 六部")
    assert result.content == rendering("director", "corpus_miss", query="尚书省 六部")


@pytest.mark.parametrize("key", ["corpus_hit", "corpus_passage", "corpus_miss", "corpus_building"])
def test_every_line_of_the_receipt_is_editable(key: str) -> None:
    """回执里的每一句都住在 `director.yaml` 的 `rendering` 里（R31）。

    **它们是模型会读到的字**，所以必须可审查可修改——代码只决定说哪一句、
    填什么值。少一条就是运行时 KeyError，而它藏在工具深处。
    """
    assert key in (load("director").get("rendering") or {})
