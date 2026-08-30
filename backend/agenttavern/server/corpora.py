"""外部资料的读写接口（S136 / S143 / S144）。

**单独一个模块**，同 `prompts.py`：它和 session / worldbook 不共享状态，
读写的是 `data/corpora/` 那一棵树。

## 导入立刻返回，索引在后台建

一本三十万字的小说切几千块、每块过一次 embedding，要**分钟级**的时间。
同步做的话导入看起来就是卡死了（S144）。

所以 `POST /corpora` 建完目录、写完原文就返回，索引交给一个后台任务；
状态与进度落在 `Corpus.build` 上，界面轮询 `GET /corpora` 就看得见。

**后台任务的引用必须留着。** `asyncio.create_task` 的返回值没人持有时，
任务随时可能被垃圾回收——症状是建索引"偶尔"不动，而且只在负载高的时候出现。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..corpus.build import build_index, sweep_stale
from ..corpus.search import EmbedderLike
from ..models.corpus import BuildState, Corpus
from ..storage import SessionRepository, Store
from ..storage.corpora import CorpusRepository


class NewCorpus(BaseModel):
    """导入一份资料。**原文整个传上来**——它是纯文本，没有别的形状。"""

    name: str = "未命名资料"
    note: str = ""
    text: str = Field(min_length=1)


class Rename(BaseModel):
    """能改的只有给人看的那两样。**原文不给改**——见 `patch_corpus`。"""

    name: str | None = None
    note: str | None = None


class Builder:
    """后台建索引的那几个任务。

    **持有引用是必须的**，不是整洁：`asyncio` 只保持对运行中任务的弱引用，
    没人持有时它可能在任意 await 点被回收，而症状是"建索引偶尔卡住不动"。
    """

    def __init__(self, store: Store, embedder: EmbedderLike | None) -> None:
        self._store = store
        self._embedder = embedder
        self._tasks: set[asyncio.Task[Any]] = set()

    def start(self, corpus_id: str) -> None:
        """**只能从事件循环里叫。**

        FastAPI 把 `def` 的 handler 丢到线程池跑，那里没有运行中的循环，
        `create_task` 会当场 `RuntimeError`——所以要起后台任务的那两个
        handler 必须是 `async def`。它们只做文件 IO，不阻塞循环。
        """
        task = asyncio.create_task(self._run(corpus_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, corpus_id: str) -> None:
        # `build_index` 自己把失败写进 `build.state`，所以这里不需要 try——
        # **它不抛**，那是它的契约（失败也要看得见，S144）。
        await build_index(self._store.corpora().repo(corpus_id), embedder=self._embedder)

    async def drain(self) -> None:
        """等在跑的建完。**只给测试和关服务用。**"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def router(store: Store, embedder: EmbedderLike | None = None) -> tuple[APIRouter, Builder]:
    """**返回 builder**，让调用方能在关服务时等它，也让测试等得到。"""
    api = APIRouter()
    builder = Builder(store, embedder)

    def repo_of(corpus_id: str) -> CorpusRepository:
        repo = store.corpora().repo(corpus_id)
        if not repo.exists():
            raise HTTPException(404, f"资料不存在：{corpus_id}")
        return repo

    @api.get("/corpora")
    def list_corpora() -> dict[str, Any]:
        """全部资料，**带上「谁在用」**——同设定库那条（S136）。

        **一份读不出来的资料不该让整张列表 500**（同 S72）：`survey()` 数得出
        好的、也点得出坏的。坏的要**报出来而不是跳过**——悄悄少一份比 500 更糟，
        用户根本不知道自己丢了什么。
        """
        good, broken = store.corpora().survey()
        return {
            "corpora": [{"corpus": c, "sessions": store.sessions_using(c.corpus_id)} for c in good],
            "broken": broken,
        }

    @api.post("/corpora")
    async def create_corpus(new: NewCorpus) -> Corpus:
        """导入。**立刻返回，索引在后台建**（S144）。

        返回的 `build.state` 是 `pending`——**不是骗人的 `ready`**：
        这一刻它确实还查不了，而界面要照着这个状态显示进度。
        """
        corpus = Corpus(name=new.name, note=new.note)
        store.corpora().create(corpus, text=new.text)
        builder.start(corpus.corpus_id)
        return store.corpora().repo(corpus.corpus_id).load()

    @api.get("/corpora/{corpus_id}")
    def get_corpus(corpus_id: str) -> dict[str, Any]:
        repo = repo_of(corpus_id)
        corpus = repo.load()
        return {
            "corpus": corpus,
            "sessions": store.sessions_using(corpus_id),
            # **只给开头一段**：整本几十万字，而这里的用处是"确认导对了没有"。
            "head": repo.text()[:500],
        }

    @api.patch("/corpora/{corpus_id}")
    def patch_corpus(corpus_id: str, patch: Rename) -> Corpus:
        """改名字和备注。**原文不给改**。

        改了原文，盘上那份索引就对不上了——块的区间会指到别的字上，
        而检索仍然照跑，**引文静默地指错位置**。要换内容就重新导一份。
        """
        repo = repo_of(corpus_id)
        fields = {k: v for k, v in patch.model_dump().items() if v is not None}
        corpus = repo.load().model_copy(update=fields)
        repo.save(corpus)
        return corpus

    @api.post("/corpora/{corpus_id}/rebuild")
    async def rebuild_corpus(corpus_id: str) -> Corpus:
        """重建索引。**建失败之后唯一的出路**，也用于换了 embedding 模型之后。

        换模型不重建的话，盘上的向量维度和新模型对不上——那个错要等到下一次
        检索才炸（`VectorIndex` 会拒绝维度不符的查询）。
        """
        repo = repo_of(corpus_id)
        builder.start(corpus_id)
        return repo.load()

    @api.delete("/corpora/{corpus_id}")
    def delete_corpus(corpus_id: str) -> dict[str, Any]:
        """**有故事挂着就不让删**，先说清是哪几局（S136）。"""
        repo_of(corpus_id)
        if users := store.delete_corpus(corpus_id):
            raise HTTPException(409, f"还有 {len(users)} 局故事挂着这份资料。")
        return {"deleted": corpus_id}

    return api, builder


def session_router(store: Store) -> APIRouter:
    """挂载与摘除。**挂在 session 那一侧**，因为改的是 session。"""
    api = APIRouter()

    def session_of(session_id: str) -> SessionRepository:
        repo = store.session(session_id)
        if not repo.session_path.exists():
            raise HTTPException(404, f"session 不存在：{session_id}")
        return repo

    @api.get("/sessions/{session_id}/corpora")
    def list_attached(session_id: str) -> list[Corpus]:
        """这局故事挂着哪几份。**还在建索引的也列出来**——
        用户要看得见"它还没好"，而不是以为自己没挂上（S144）。
        """
        return session_of(session_id).corpora()

    @api.put("/sessions/{session_id}/corpora/{corpus_id}")
    def attach(session_id: str, corpus_id: str) -> list[Corpus]:
        repo = session_of(session_id)
        try:
            repo.attach_corpus(corpus_id)
        except KeyError as exc:
            # **挂一份不存在的当场拒绝**：留下悬空 id 的话，用户看到"挂上了"
            # 而检索永远查不到它（P29）。
            raise HTTPException(404, str(exc)) from exc
        return repo.corpora()

    @api.delete("/sessions/{session_id}/corpora/{corpus_id}")
    def detach(session_id: str, corpus_id: str) -> list[Corpus]:
        """摘下来。**资料本身不动**——别的故事可能还在用（S136）。"""
        repo = session_of(session_id)
        repo.detach_corpus(corpus_id)
        return repo.corpora()

    return api


__all__ = ["BuildState", "Builder", "NewCorpus", "router", "session_router", "sweep_stale"]
