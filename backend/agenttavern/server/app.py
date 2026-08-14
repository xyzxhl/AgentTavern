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
    Goal,
    GoalRun,
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
    GoalControl,
    Interject,
    Regenerate,
    ServerError,
    ServerMessage,
    SetGoal,
    SetGoalBudget,
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

    # -- 服务本身 ------------------------------------------------------------

    @app.get("/meta")
    def server_meta() -> dict[str, Any]:
        """服务级信息。**前端据此决定要不要显示开发者面板**（D156）。

        模式在启动服务时就定了，不是运行时开关——所以这里只读，没有对应的写接口。
        """
        return {"developer": resolved.developer, "model": resolved.llm_model}

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
            Tracer(
                repo,
                session.active_leaf_id,
                persist=resolved.developer,
                developer=resolved.developer,
            ),
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

    @app.delete("/sessions/{session_id}/worldinfo/{entry_id}")
    def delete_worldinfo(session_id: str, entry_id: str) -> dict[str, Any]:
        """删一条设定。**它的种子会一并从所有分支的起点消失**（见仓库层注释）。"""
        if not repo_of(session_id).delete_worldbook_entry(entry_id):
            raise HTTPException(404, f"设定条目不存在：{entry_id}")
        return {"deleted": entry_id}

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

        runner = SessionRunner(repo, active, broadcast, developer=resolved.developer)
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

    elif isinstance(message, SetGoal):
        # **同一时间只有一个 goal**（D148）。已有的必须先放弃——goal_manager 的
        # 对话是围着那个目标建起来的，换一个就得整份重来，而那正是 `abandon`。
        session = repo.load_session()
        if session.meta.goal is not None:
            await broadcast(ServerError(message="已经有一个目标在跑。先放弃它再设新的。"))
        else:
            session.meta.goal = Goal(
                description=message.description,
                **{
                    k: v
                    for k, v in (
                        ("max_runs", message.max_runs),
                        ("max_total_tokens", message.max_total_tokens),
                    )
                    if v is not None
                },
            )
            # **记下从哪儿开始**（D150）：它之前的剧情给细纲、进静态前缀，
            # 之后的每一轮给正文、逐条追加。
            session.goal_run = GoalRun(started_at_leaf_id=session.active_leaf_id)
            repo.save_session(session)

    elif isinstance(message, SetGoalBudget):
        # **这是"加预算接着跑"的入口。** 目标本身禁改，预算恰恰要能在跑到一半
        # 时调大——预算耗尽是暂停不是失败（D148）。调大之后顺手解除那次暂停。
        session = repo.load_session()
        if session.meta.goal is None:
            await broadcast(ServerError(message="当前没有目标。"))
        else:
            if message.max_runs is not None:
                session.meta.goal.max_runs = message.max_runs
            if message.max_total_tokens is not None:
                session.meta.goal.max_total_tokens = message.max_total_tokens
            run = session.goal_run
            if run is not None and run.paused and run.exhausted(session.meta.goal) is None:
                run.paused, run.paused_reason = False, ""
            repo.save_session(session)

    elif isinstance(message, GoalControl):
        session = repo.load_session()
        if session.meta.goal is None:
            await broadcast(ServerError(message="当前没有目标。"))
        elif message.action == "abandon":
            session.meta.goal = None
            session.goal_run = None
            repo.save_session(session)
        else:
            run = session.goal_run or GoalRun()
            run.paused = message.action == "pause"
            run.paused_reason = "用户暂停" if run.paused else ""
            session.goal_run = run
            repo.save_session(session)

    elif isinstance(message, Regenerate):
        # 重生成 = 在当前叶的父节点下新建兄弟（D46）。**只能重 roll 当前节点**
        # （D147）：分支只在当前节点上开，想在更早处分叉先 `SwitchBranch` 切过去。
        # 这里当场校验而不是静默改写，是因为"你以为的当前节点已经不是当前节点了"
        # 悄悄重 roll 掉另一轮，比报错难查得多。
        if message.node_id != repo.load_session().active_leaf_id:
            await broadcast(ServerError(message="只能重新生成当前节点。先切到那一支再重生成。"))
        else:
            node = repo.load_node(message.node_id)
            await runner.submit(node.user_input.text if node.user_input else "", regenerate=True)


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
