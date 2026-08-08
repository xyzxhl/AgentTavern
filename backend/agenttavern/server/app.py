"""FastAPI 应用：REST 管 session 与节点，WebSocket 管一次会话的实时交互。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from ..config import Settings, load_settings
from ..llm import LLMGateway
from ..models import (
    Capabilities,
    ControlDimensions,
    Node,
    Session,
    SessionMeta,
    SessionModes,
    WorldbookEntry,
)
from ..orchestration import close_segment
from ..storage import SessionRepository, Store
from ..tracing import Tracer
from .protocol import (
    Answer,
    Branches,
    ClientEnvelope,
    EditNode,
    Interject,
    Regenerate,
    ServerError,
    ServerMessage,
    StateSnapshot,
    Stop,
    Submit,
    SwitchBranch,
)
from .runner import SessionRunner


class SessionPatch(BaseModel):
    meta: SessionMeta | None = None
    control: ControlDimensions | None = None
    modes: SessionModes | None = None
    capabilities: Capabilities | None = None


class NodePatch(BaseModel):
    """用户直接编辑节点数据（D65）。

    **直接改写，不做依赖检查**——乐观假设用户只改最新输出，此时本无下游。
    """

    outline: str | None = None
    prose: str | None = None
    state_set: dict[str, Any] | None = None


def create_app(settings: Settings | None = None, gateway: LLMGateway | None = None) -> FastAPI:
    resolved = settings or load_settings()
    store = Store(resolved.data_dir)

    # 注入的 gateway 直接持有；只有自建的那个才归应用管生命周期。
    # 不放 app.state：TestClient 不作为上下文管理器使用时不会跑 lifespan。
    owned = gateway is None
    active = gateway or LLMGateway(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI):  # type: ignore[no-untyped-def]
        yield
        if owned:
            await active.aclose()

    app = FastAPI(title="AgentTavern", lifespan=lifespan)

    def repo_of(session_id: str) -> SessionRepository:
        repo = store.session(session_id)
        if not repo.session_path.exists():
            raise HTTPException(404, f"session 不存在：{session_id}")
        return repo

    # -- session ------------------------------------------------------------

    @app.post("/sessions")
    def create_session(session: Session | None = None) -> Session:
        created = session or Session()
        store.create_session(created)
        return created

    @app.get("/sessions")
    def list_sessions() -> list[str]:
        return store.list_sessions()

    @app.get("/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        repo = repo_of(session_id)
        session = repo.load_session()
        leaf = session.active_leaf_id
        return {
            "session": session,
            "path": repo.path_to(leaf) if leaf else [],
            "state": repo.state_at(leaf).values,
        }

    @app.patch("/sessions/{session_id}")
    def patch_session(session_id: str, patch: SessionPatch) -> Session:
        repo = repo_of(session_id)
        session = repo.load_session()
        # 用模型对象而非 model_dump 更新——后者会绕过校验，且让嵌套字段退化成 dict。
        update = {k: v for k, v in patch if v is not None}
        session = session.model_copy(update=update)
        repo.save_session(session)
        return session

    # -- 节点与分支 ----------------------------------------------------------

    @app.get("/sessions/{session_id}/nodes/{node_id}/siblings")
    def siblings(session_id: str, node_id: str) -> Branches:
        repo = repo_of(session_id)
        node = repo.load_node(node_id)
        group = repo.children_of(node.parent_id)
        ids = [n.id for n in group]
        return Branches(node_id=node_id, siblings=ids, active_index=ids.index(node_id))

    @app.patch("/sessions/{session_id}/nodes/{node_id}")
    def patch_node(session_id: str, node_id: str, patch: NodePatch) -> Node:
        repo = repo_of(session_id)
        return _apply_node_patch(repo, node_id, patch)

    @app.post("/sessions/{session_id}/close-segment")
    async def close_current_segment(session_id: str) -> dict[str, Any]:
        """用户手动归段（D112 的第三条支路）。

        **用户说这一段收了，就是收了**——不再追问模型。这条路在
        `SummarizeMode.MANUAL` 下是唯一的归段方式，在 AUTO 下也保留：
        自动追问判早了或判晚了，用户随时可以自己断。
        """
        repo = repo_of(session_id)
        session = repo.load_session()
        if session.active_leaf_id is None:
            raise HTTPException(400, "还没有可归段的内容")

        end = await close_segment(
            repo,
            active,
            session.active_leaf_id,
            Tracer(repo, session.active_leaf_id),
            forced=True,
        )
        if end is None:
            return {"closed": False, "reason": "没有待归段的轮次"}
        return {"closed": True, "end": end.id, "span": end.segment_span}

    # -- worldinfo ----------------------------------------------------------

    @app.get("/sessions/{session_id}/worldinfo")
    def list_worldinfo(session_id: str) -> list[WorldbookEntry]:
        return repo_of(session_id).worldbook()

    @app.put("/sessions/{session_id}/worldinfo")
    def put_worldinfo(session_id: str, entry: WorldbookEntry) -> WorldbookEntry:
        repo_of(session_id).save_worldbook_entry(entry)
        return entry

    # -- WebSocket ----------------------------------------------------------

    @app.websocket("/sessions/{session_id}/ws")
    async def session_socket(websocket: WebSocket, session_id: str) -> None:
        repo = store.session(session_id)
        if not repo.session_path.exists():
            await websocket.close(code=4404)
            return

        await websocket.accept()

        async def broadcast(message: ServerMessage) -> None:
            await websocket.send_text(message.model_dump_json())

        runner = SessionRunner(repo, active, broadcast)
        runner.start()

        session = repo.load_session()
        await broadcast(
            StateSnapshot(
                values=repo.state_at(session.active_leaf_id).values,
                leaf_id=session.active_leaf_id,
            )
        )

        try:
            while True:
                raw = await websocket.receive_text()
                await _handle(raw, runner, repo, broadcast)
        except WebSocketDisconnect:
            pass
        finally:
            await runner.aclose()

    return app


async def _handle(
    raw: str,
    runner: SessionRunner,
    repo: SessionRepository,
    broadcast: Any,
) -> None:
    try:
        message = ClientEnvelope.model_validate_json(f'{{"message":{raw}}}').message
    except ValidationError as exc:
        await broadcast(ServerError(message=f"消息格式错误：{exc.error_count()} 处"))
        return

    if isinstance(message, Submit):
        await runner.submit(message.text, message.control_override)

    elif isinstance(message, Interject):
        # 没有 run 在跑（或已经进了 writer，那里没有操作边界）就回落到开新的一轮——
        # 用户的话在任何情况下都不该被吞掉。
        if not runner.interject(message.text, interrupt=message.interrupt):
            await runner.submit(message.text)

    elif isinstance(message, Answer):
        if not runner.answer(message.question_id, message.selected):
            await broadcast(ServerError(message="这个问题已经不在等待中了"))

    elif isinstance(message, Stop):
        if not runner.stop():
            await broadcast(ServerError(message="当前没有正在进行的生成"))

    elif isinstance(message, SwitchBranch):
        session = repo.load_session()
        session.active_leaf_id = message.node_id
        repo.save_session(session)
        # 切换分支必须同时切换状态（D48），否则"文本对、状态悄悄错"。
        await broadcast(
            StateSnapshot(values=repo.state_at(message.node_id).values, leaf_id=message.node_id)
        )

    elif isinstance(message, EditNode):
        node = _apply_node_patch(
            repo,
            message.node_id,
            NodePatch(
                outline=message.outline,
                prose=message.prose,
                state_set=message.state_set,
            ),
        )
        await broadcast(StateSnapshot(values=repo.state_at(node.id).values, leaf_id=node.id))

    elif isinstance(message, Regenerate):
        # 重生成 = 在父节点下新建兄弟（D46）。**父节点随这一条输入一起入队**，
        # 不通过改 `session.active_leaf_id` 传递——那是共享状态，若此刻有 run 在飞，
        # 它结束时会把活跃叶覆盖成自己，重生成就挂到了错误的父节点上。
        node = repo.load_node(message.node_id)
        await runner.submit(
            node.user_input.text if node.user_input else "",
            parent_id=node.parent_id,
            explicit_parent=True,
        )


def _apply_node_patch(repo: SessionRepository, node_id: str, patch: NodePatch) -> Node:
    """用户编辑直接改写节点数据（D65），**不做依赖检查**。

    此前曾主张"插入一个只含 delta 的新节点"以保护已提交节点的不可变性，
    该方案已否决——它为一个乐观假设下不存在的问题增加复杂度，
    也不符合常见 AI chat 的编辑语义。
    """
    node = repo.load_node(node_id)
    if patch.outline is not None:
        node.outline = patch.outline
        node.outline_summary = None  # 细纲变了，缓存的摘要作废
    if patch.prose is not None:
        node.prose = patch.prose
    if patch.state_set is not None:
        node.state_delta.set = patch.state_set
    repo.save_node(node)
    return node
