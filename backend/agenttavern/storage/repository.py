"""Session 仓储：节点树、路径重放、trace。

目录布局::

    data/sessions/<session_id>/
      session.json              worldbook_id、corpus_ids、标题、goal
      nodes/<node_id>.json      一个 run 一个节点
      trace/<run_id>.jsonl      append-only，每 run 一个文件

**设定不在这里**——它是一级资产，见 `worldbooks.py`（S63）。
**外部资料也不在这里**，见 `corpora.py`（S136）：session 上只存挂了哪几份的 id。

写入是**单线程**的（P5），因此不需要锁。规模上限是几百个 run × 每个几 KB，
全量读取毫秒级；真到瓶颈再加索引缓存，模型不变。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from ..corpus.lookup import Source
from ..models import (
    EntryKind,
    Node,
    RunDraft,
    Session,
    StateDelta,
    TraceEvent,
    Worldbook,
    WorldbookEntry,
    WorldState,
)
from ..models.corpus import Corpus
from .corpora import CorpusStore
from .jsonfile import append_jsonl, read_jsonl, read_model, read_model_or_none, write_model
from .worldbooks import WorldbookRepository


class SessionRepository:
    def __init__(self, root: Path, session_id: str) -> None:
        self.root = root
        self.dir = root / "sessions" / session_id
        self.session_id = session_id

    # -- 路径 ---------------------------------------------------------------

    @property
    def session_path(self) -> Path:
        return self.dir / "session.json"

    @property
    def nodes_dir(self) -> Path:
        return self.dir / "nodes"

    def node_path(self, node_id: str) -> Path:
        return self.nodes_dir / f"{node_id}.json"

    def trace_path(self, run_id: str) -> Path:
        return self.dir / "trace" / f"{run_id}.jsonl"

    # -- session ------------------------------------------------------------

    def load_session(self) -> Session:
        return read_model(self.session_path, Session)

    def save_session(self, session: Session) -> None:
        write_model(self.session_path, session)

    # -- 被打断的推演（S48） -------------------------------------------------

    @property
    def draft_path(self) -> Path:
        return self.dir / "draft.json"

    def load_draft(self) -> RunDraft | None:
        """**没有草稿 = 文件不存在。** 不能用某个字段的空值表示，因为
        `parent_id=None` 本身是合法的（根节点），所以它表达不了「没有指定」。
        """
        return read_model_or_none(self.draft_path, RunDraft)

    def save_draft(self, draft: RunDraft) -> None:
        write_model(self.draft_path, draft)

    def clear_draft(self) -> None:
        self.draft_path.unlink(missing_ok=True)

    # -- 节点 ---------------------------------------------------------------

    def load_node(self, node_id: str) -> Node:
        return read_model(self.node_path(node_id), Node)

    def save_node(self, node: Node) -> None:
        write_model(self.node_path(node.id), node)

    def all_nodes(self) -> dict[str, Node]:
        if not self.nodes_dir.exists():
            return {}
        nodes = (read_model(p, Node) for p in self.nodes_dir.glob("*.json"))
        return {n.id: n for n in nodes}

    def children_of(self, node_id: str | None) -> list[Node]:
        """某节点的子节点，按创建时间排序。用于 UI 的 ``< 2/3 >`` 兄弟切换（S44）。

        以 id 作为次级排序键：``all_nodes`` 来自 ``glob``，顺序由文件系统决定，
        时间戳相同（同一微秒内创建）时会渗进来，导致切换器的顺序在两次刷新之间
        跳动。真实场景每个 run 相隔数秒不会撞，但顺序必须是确定的。
        """
        siblings = [n for n in self.all_nodes().values() if n.parent_id == node_id]
        return sorted(siblings, key=lambda n: (n.created_at, n.id))

    # -- 路径与状态重放 ------------------------------------------------------

    def path_to(self, leaf_id: str) -> list[Node]:
        """根 → leaf 的一条链。

        沿 parent 指针上溯再反转。带环检测——文件可被用户直接编辑（R22），
        不能假设数据永远自洽。
        """
        nodes = self.all_nodes()
        chain: list[Node] = []
        seen: set[str] = set()
        current: str | None = leaf_id

        while current is not None:
            if current in seen:
                raise ValueError(f"节点树中存在环：{current}")
            node = nodes.get(current)
            if node is None:
                raise ValueError(f"节点不存在：{current}")
            seen.add(current)
            chain.append(node)
            current = node.parent_id

        chain.reverse()
        return chain

    def compose_initial_state(self) -> WorldState:
        """把全部 `STATE` 条目合成一个初始状态。**故事开始之前**用它。"""
        return self.book().compose_initial_state()

    def initial_state(self) -> WorldState:
        """一切分支共同的起点。

        **故事一开始就冻结**（S67）。开始之前它是现算的——那时改设定当然该
        立刻生效，故事还没有过去。第一轮落地时快照进 `Session.initial_state`，
        之后再改条目就只影响"以后新建的会话"，不再回头改写既有的每一轮。

        早先它一直是现算的，说法是"改了影响所有分支，同改角色卡，是对的"。
        **那个类比不成立**：改角色卡改的是设定，而这里改的是**已经发生过的
        剧情所依赖的事实**——第五轮写着"她掏出钥匙"，而钥匙是初始状态给的；
        把它删掉，那一轮就凭空多了一把不存在的钥匙，且没有任何地方会报错。
        """
        session = self.load_session()
        if session.initial_state is not None:
            return WorldState(values=session.initial_state)
        return self.compose_initial_state()

    def freeze_initial_state(self) -> None:
        """把当前的初始状态定下来。**第一轮开始时调，之后是空操作。**"""
        session = self.load_session()
        if session.initial_state is None:
            session.initial_state = self.compose_initial_state().values
            self.save_session(session)

    def state_at(self, leaf_id: str | None) -> WorldState:
        """沿路径重放增量，得到该点的状态。

        **状态是路径的函数，不是全局单例**（S42）。切换分支必须同时切换状态，
        否则会出现"文本对、状态悄悄错"——那类 bug 要很多 run 后才以角色行为
        不合理的形式暴露，届时根本查不出源头。

        起点是初始状态而非空，见 ``initial_state``。
        """
        state = self.initial_state()
        if leaf_id is None:
            return state
        for node in self.path_to(leaf_id):
            state = state.apply(node.state_delta)
        return state

    def deltas_along(self, leaf_id: str) -> list[StateDelta]:
        return [n.state_delta for n in self.path_to(leaf_id)]

    # -- worldinfo ----------------------------------------------------------

    def book(self) -> WorldbookRepository:
        """这个 session 挂在哪份设定上（S63）。

        **每次重新读 session**：`worldbook_id` 本身不会变，但元操作与生成是并行的
        （P10），缓存一份反而多一个可能对不上的东西，而这一步只是拼个路径。
        """
        return WorldbookRepository(self.root, self.load_session().worldbook_id)

    def worldbook(self) -> list[WorldbookEntry]:
        """这个 session 看得见的全部设定条目。**不做预筛选**（P13/S53）。"""
        return self.book().entries()

    def corpora(self) -> list[Corpus]:
        """这个 session 挂着的外部资料，**只有元信息**（S136）。

        索引不在这里读——注册工具时只需要知道"挂没挂、建好没有"（S145/S144），
        而把几十万字的块表读进来只为了数一下有没有，代价完全不对等。
        真要查时走 `corpus_sources()`。

        **挂了但目录已经不在的，跳过。** 一条悬空的 id 让整局故事开不起来，
        症状会是"点了没反应"，而它本该只是少一份资料（同 S72）。
        """
        store = CorpusStore(self.root)
        out: list[Corpus] = []
        for corpus_id in self.load_session().corpus_ids:
            repo = store.repo(corpus_id)
            if repo.exists():
                out.append(repo.load())
        return out

    def corpus_sources(self) -> list[Source]:
        """挂着的资料里**这一刻能查的那几份**。还在建索引的不在其中（S144）。"""
        store = CorpusStore(self.root)
        sources = (store.repo(c.corpus_id).source() for c in self.corpora())
        return [s for s in sources if s is not None]

    def attach_corpus(self, corpus_id: str) -> Session:
        """把一份资料挂到这局故事上。**重复挂是空操作**，不报错。

        **挂一份不存在的当场拒绝**：留下一条悬空 id 的话，`corpora()` 会宽容地
        跳过它（同 S72），于是用户看到"挂上了"而检索永远查不到它——
        **一个没生效的操作不该悄悄没生效**（P29）。
        """
        if not CorpusStore(self.root).repo(corpus_id).exists():
            raise KeyError(f"资料不存在：{corpus_id}")
        session = self.load_session()
        if corpus_id in session.corpus_ids:
            return session
        updated = session.model_copy(update={"corpus_ids": [*session.corpus_ids, corpus_id]})
        self.save_session(updated)
        return updated

    def detach_corpus(self, corpus_id: str) -> Session:
        """摘下来。**资料本身不动**——它是一级资产，别的故事可能还在用（S136）。

        没挂过也是空操作：摘一个本来就不在的东西，结果和期望一致。
        """
        session = self.load_session()
        remaining = [c for c in session.corpus_ids if c != corpus_id]
        if remaining == session.corpus_ids:
            return session
        updated = session.model_copy(update={"corpus_ids": remaining})
        self.save_session(updated)
        return updated

    # `world_meta()` **已删除**（S64）：worldbook 上没有字段了，题材与尺度都是
    # `WORLD` 条目，跟着 `worldbook()` 一起来。

    # -- trace --------------------------------------------------------------

    def append_trace(self, event: TraceEvent) -> None:
        append_jsonl(self.trace_path(event.run_id), event)

    def read_trace(self, run_id: str) -> list[TraceEvent]:
        return list(read_jsonl(self.trace_path(run_id), TraceEvent))


class Store:
    """数据根目录。**两类一级资产：worldbook 与 session**（S63）。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- session ------------------------------------------------------------

    def session(self, session_id: str) -> SessionRepository:
        return SessionRepository(self.root, session_id)

    def create_session(self, session: Session) -> SessionRepository:
        """**没指定设定就现建一份空的**（S63）。

        允许「先开故事、边写边补设定」——要求先建 worldbook 才能开会话，等于
        在最没有想法的那一刻逼人做决定。
        """
        if not session.worldbook_id:
            book = self.create_worldbook(Worldbook())
            session = session.model_copy(update={"worldbook_id": book.worldbook_id})
        repo = self.session(session.id)
        repo.save_session(session)
        return repo

    def list_sessions(self) -> list[str]:
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return []
        return sorted(p.name for p in sessions_dir.iterdir() if (p / "session.json").exists())

    # -- worldbook ----------------------------------------------------------

    def worldbook(self, worldbook_id: str) -> WorldbookRepository:
        return WorldbookRepository(self.root, worldbook_id)

    def create_worldbook(self, book: Worldbook) -> WorldbookRepository:
        repo = self.worldbook(book.id)
        repo.save(book)
        return repo

    def list_worldbooks(self) -> list[str]:
        books_dir = self.root / "worldbooks"
        if not books_dir.exists():
            return []
        return sorted(p.name for p in books_dir.iterdir() if (p / "worldbook.json").exists())

    def sessions_of(self, worldbook_id: str) -> list[str]:
        """哪些会话开在这份设定上。

        **删设定之前必须问这个**：一份被三局故事用着的设定被删掉，那三局的
        角色会当场失忆，而界面上什么都不会说。
        """
        return [
            sid
            for sid in self.list_sessions()
            if self.session(sid).load_session().worldbook_id == worldbook_id
        ]

    def delete_worldbook(self, worldbook_id: str) -> list[str]:
        """删掉一份设定。**返回挡住它的那几局故事**——空列表表示删掉了。

        判断和执行放在一起，不让调用方先查一遍再删：那就是两处判断，而两处判断
        迟早对不上（同 S65）。调用方拿返回值渲染理由，不必自己再扫一遍。
        """
        repo = self.worldbook(worldbook_id)
        users = self.sessions_of(worldbook_id)
        if users or not repo.exists():
            return users
        shutil.rmtree(repo.dir, ignore_errors=True)
        return []

    # -- 外部资料（S136） ----------------------------------------------------

    def corpora(self) -> CorpusStore:
        return CorpusStore(self.root)

    def sessions_using(self, corpus_id: str) -> list[str]:
        """哪些故事挂着这份资料。**删之前必须问这个**，同 `sessions_of`。

        和设定那条不同的是这里是**多对多**：一局故事挂零到多份资料，
        所以查的是"在不在那个列表里"，不是"等不等于那个 id"。
        """
        return [
            sid
            for sid in self.list_sessions()
            if corpus_id in self.session(sid).load_session().corpus_ids
        ]

    def delete_corpus(self, corpus_id: str) -> list[str]:
        """删掉一份资料。**返回挡住它的那几局故事**——空列表表示删掉了。

        判断和执行放在一起，同 `delete_worldbook`：让调用方先查一遍再删，
        就是两处判断，而两处判断迟早对不上。
        """
        store = self.corpora()
        users = self.sessions_using(corpus_id)
        if users or not store.repo(corpus_id).exists():
            return users
        store.delete(corpus_id)
        return []

    # -- 旧数据 -------------------------------------------------------------

    def migrate(self) -> int:
        """跑所有的旧数据搬迁。返回搬了几个 session（`migrate_kinds` 不计入）。

        **两件事，顺序要紧**：先把条目的 `kind` 改成现在的写法，再搬 worldbook——
        后者会 `create_worldbook`，而那之后任何一次列表都会去读条目。
        """
        self.migrate_kinds()
        return self._migrate_worldbooks()

    def migrate_kinds(self) -> int:
        """条目的 `kind` 换过两次名，把旧值改成现在的（S65/S64）。返回改了几条。

        | 旧值 | 现在 |
        |---|---|
        | `lore` | `world` |
        | `player` | `character` + `is_player: true` |

        `player` 那条尤其要紧：**用户扮演的角色不是第四种 kind**，它是一条角色
        条目加一个标记（S65）——那次改形状时**漏了迁移**，于是盘上留着一批
        读不出来的条目。

        **读原始 JSON，不经模型**（同 S63 的理由，只是这次更硬）：这些值现在
        根本不在 `EntryKind` 里，照模型读会直接抛 `ValidationError`——而那正是
        这个函数要修的东西，用它自己去读要修的数据是个死结。
        """
        renamed = 0
        for path in (self.root / "worldbooks").glob("*/entries/*.json"):
            raw = json.loads(path.read_text(encoding="utf-8"))
            kind = raw.get("kind")
            if kind == "lore":
                raw["kind"] = EntryKind.WORLD.value
            elif kind == "player":
                # 名字、正文、语言习惯全都留着——变的只是"他是不是用户扮演的那个"
                # 从 kind 挪到了一个标记上。
                raw["kind"] = EntryKind.CHARACTER.value
                raw["is_player"] = True
            else:
                continue
            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            renamed += 1
        return renamed

    def _migrate_worldbooks(self) -> int:
        """把 worldbook 从 session 目录里搬出来（S63）。返回搬了几个 session。

        **启动时跑一次，之后是空操作。** 旧布局是 `sessions/<id>/worldinfo/`，
        而世界那几个属性（题材、尺度、用户扮演谁）当时挂在 `session.json` 的
        `meta` 里——`SessionMeta` 上已经没有那些字段了，所以这里读**原始 JSON**，
        不经模型：模型会默默把它们丢掉，而那正是要抢救的东西。

        一个旧 session 得到一份属于它自己的设定，不合并——两局旧故事的设定重名
        也可能完全是两回事，合并会把它们搅在一起，而拆回去没有依据。
        """
        moved = 0
        for session_id in self.list_sessions():
            repo = self.session(session_id)
            raw = json.loads(repo.session_path.read_text(encoding="utf-8"))
            if raw.get("worldbook_id"):
                continue

            meta = raw.get("meta") or {}
            # 旧的 session 标题同时是世界的名字——那时两者没分开。
            book = Worldbook(name=meta.get("title") or "未命名")
            book_repo = self.create_worldbook(book)

            old_entries = repo.dir / "worldinfo"
            if old_entries.exists():
                book_repo.entries_dir.mkdir(parents=True, exist_ok=True)
                for path in old_entries.glob("*.json"):
                    shutil.move(str(path), str(book_repo.entries_dir / path.name))
                shutil.rmtree(old_entries, ignore_errors=True)

            _lift_voices(book_repo, meta)
            _lift_world_text(book_repo, meta)

            raw["worldbook_id"] = book.id
            repo.session_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            moved += 1
        return moved


def _lift_voices(book: WorldbookRepository, meta: dict[str, Any]) -> None:
    """把旧的 `character_voices` / `user_character` 转成角色条目（S63/S65）。

    **这是迁移，不是兼容。** 那两个字段已经从 `WorldMeta` 上删掉了，代码里
    没有第二条读法——所以数据必须在这里被抬到新形状上，否则「柜台执事怎么说话」
    就静默消失了，而消失的症状是那个角色忽然不那样说话，没人会想到去查迁移。

    同名的条目**就地补字段**，不新建：那个角色多半已经有一条设定了，再造一条
    会让同一个人在 worldinfo 里出现两次。找不到才新建一条只有名字和语言习惯的。
    """
    voices: dict[str, str] = meta.get("character_voices") or {}
    player: str = meta.get("user_character") or ""
    if not voices and not player:
        return

    by_name = {e.name: e for e in book.entries()}
    for name in {*voices, *([player] if player else [])}:
        entry = by_name.get(name) or WorldbookEntry(name=name)
        book.save_entry(
            entry.model_copy(
                update={
                    # 有语言习惯或者是用户扮演的，它就**是个角色**，不再是 `world`。
                    "kind": EntryKind.CHARACTER,
                    "voice": voices.get(name, entry.voice),
                    "is_player": entry.is_player or name == player,
                }
            )
        )


_WORLD_TEXT = {"genre_tone": "题材与基调", "content_scale": "剧情尺度"}


def _lift_world_text(book: WorldbookRepository, meta: dict[str, Any]) -> None:
    """把旧的 `genre_tone` / `content_scale` 转成 `WORLD` 条目（S64）。

    **这是迁移，不是兼容。** 那两个字段已经从 `Worldbook` 上删掉了——它们本来
    就和「旧货店在老城区一条窄巷尽头」是同一种东西：一段关于这个世界的字。
    不搬的话它们静默消失，而症状是模型忽然不知道这是个悬疑故事。

    条目标记为 `ALWAYS`：题材与尺度每轮都该在眼前，按需展开对它们没有意义。
    """
    for field, name in _WORLD_TEXT.items():
        body = (meta.get(field) or "").strip()
        if body:
            book.save_entry(WorldbookEntry(name=name, kind=EntryKind.WORLD, content=body))
