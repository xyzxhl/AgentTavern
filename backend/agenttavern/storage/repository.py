"""Session 仓储：节点树、路径重放、trace。

目录布局::

    data/sessions/<session_id>/
      session.json              元指令、控制维度默认值、goal
      nodes/<node_id>.json      一个 run 一个节点
      trace/<run_id>.jsonl      append-only，每 run 一个文件

写入是**单线程**的（D6），因此不需要锁。规模上限是几百个 run × 每个几 KB，
全量读取毫秒级；真到瓶颈再加索引缓存，模型不变。
"""

from __future__ import annotations

from pathlib import Path

from ..models import Node, RunDraft, Session, StateDelta, TraceEvent, WorldState
from ..models.worldbook import EntryKind, WorldbookEntry
from .jsonfile import append_jsonl, read_jsonl, read_model, read_model_or_none, write_model


class SessionRepository:
    def __init__(self, root: Path, session_id: str) -> None:
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

    # -- 被打断的推演（D118） -------------------------------------------------

    @property
    def draft_path(self) -> Path:
        return self.dir / "draft.json"

    def load_draft(self) -> RunDraft | None:
        """**没有草稿 = 文件不存在。** 不能用某个字段的空值表示，因为
        `parent_id=None` 本身是合法的（根节点）——同 D103 那个坑。
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
        """某节点的子节点，按创建时间排序。用于 UI 的 ``< 2/3 >`` 兄弟切换（D46）。

        以 id 作为次级排序键：``all_nodes`` 来自 ``glob``，顺序由文件系统决定，
        时间戳相同（同一微秒内创建）时会渗进来，导致切换器的顺序在两次刷新之间
        跳动。真实场景每个 run 相隔数秒不会撞，但顺序必须是确定的。
        """
        siblings = [n for n in self.all_nodes().values() if n.parent_id == node_id]
        return sorted(siblings, key=lambda n: (n.created_at, n.id))

    # -- 路径与状态重放 ------------------------------------------------------

    def path_to(self, leaf_id: str) -> list[Node]:
        """根 → leaf 的一条链。

        沿 parent 指针上溯再反转。带环检测——文件可被用户直接编辑（D65），
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
        state = WorldState()
        for entry in self.worldbook():
            if entry.kind is EntryKind.STATE and entry.values:
                state = state.apply(StateDelta(set=entry.values))
        return state

    def initial_state(self) -> WorldState:
        """一切分支共同的起点。

        **故事一开始就冻结**（D158）。开始之前它是现算的——那时改设定当然该
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

        **状态是路径的函数，不是全局单例**（D48）。切换分支必须同时切换状态，
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

    @property
    def worldbook_dir(self) -> Path:
        return self.dir / "worldinfo"

    def worldbook_path(self, entry_id: str) -> Path:
        return self.worldbook_dir / f"{entry_id}.json"

    def save_worldbook_entry(self, entry: WorldbookEntry) -> None:
        write_model(self.worldbook_path(entry.id), entry)

    def load_worldbook_entry(self, entry_id: str) -> WorldbookEntry | None:
        return read_model_or_none(self.worldbook_path(entry_id), WorldbookEntry)

    def delete_worldbook_entry(self, entry_id: str) -> bool:
        """删掉一条设定。不存在返回 ``False``。

        **故事开始之后删初始状态条目不影响既有的每一轮**（D158）：起点那时
        已经冻结进 `Session.initial_state` 了，这里删的只是"以后新建会话时的模板"。
        开始之前删则立刻生效——那时故事还没有过去，没有东西依赖它。
        """
        path = self.worldbook_path(entry_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def clear_worldbook(self) -> int:
        """删掉全部条目，返回删了几条。**导入时的「替换」用它。**"""
        entries = self.worldbook()
        for entry in entries:
            self.worldbook_path(entry.id).unlink(missing_ok=True)
        return len(entries)

    def worldbook(self) -> list[WorldbookEntry]:
        """**全部**条目，不做预筛选（D62/D63）。索引覆盖全集是准入标准。"""
        if not self.worldbook_dir.exists():
            return []
        entries = [read_model(p, WorldbookEntry) for p in self.worldbook_dir.glob("*.json")]
        return sorted(entries, key=lambda e: (e.name, e.id))

    # -- trace --------------------------------------------------------------

    def append_trace(self, event: TraceEvent) -> None:
        append_jsonl(self.trace_path(event.run_id), event)

    def read_trace(self, run_id: str) -> list[TraceEvent]:
        return list(read_jsonl(self.trace_path(run_id), TraceEvent))


class Store:
    """数据根目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def session(self, session_id: str) -> SessionRepository:
        return SessionRepository(self.root, session_id)

    def create_session(self, session: Session) -> SessionRepository:
        repo = self.session(session.id)
        repo.save_session(session)
        return repo

    def list_sessions(self) -> list[str]:
        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return []
        return sorted(p.name for p in sessions_dir.iterdir() if (p / "session.json").exists())
