"""FastAPI 应用：REST 管 session 与节点，WebSocket 管一次会话的实时交互。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ValidationError

from ..config import Settings, load_settings
from ..llm import LLMGateway
from ..models import (
    Actor,
    Capabilities,
    ControlDimensions,
    Goal,
    GoalRun,
    Node,
    Session,
    SessionMeta,
    SessionModes,
    TraceEvent,
    TraceEventType,
    Worldbook,
    WorldbookBundle,
    WorldbookEntry,
    new_id,
)
from ..orchestration import close_segment
from ..prompting import choices_of
from ..storage import SessionRepository, Store, WorldbookRepository
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

    # **旧数据在这里搬家**（D170）：worldbook 以前躺在 session 目录里。
    # 跑一次就是空操作，所以放在这里比放一条命令可靠——没人会记得去跑那条命令。
    store.migrate()

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
        return {
            "developer": resolved.developer,
            "model": resolved.llm_model,
            # **档位从 YAML 现读**，不在前端写死一份（同 D149 的理由）：用户加一档
            # 文风，设置面板里就该多一个选项，而不是等谁想起来同步。
            "options": {
                "prose_style": choices_of("writer", "prose_style"),
                "pov": choices_of("writer", "pov"),
            },
        }

    # -- session ------------------------------------------------------------

    @app.post("/sessions")
    def create_session(session: Session | None = None) -> Session:
        """**返回落盘的那一份，不是传进来的那一份。**

        没指定设定时 `create_session` 会现建一份并写进 `worldbook_id`（D170），
        而它是在一个 `model_copy` 上写的——把入参原样回给客户端，那个字段就是空的。
        """
        return store.create_session(session or Session()).load_session()

    @app.get("/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        """**带标题，不只是 id。** 列表上一串 `sess_a1b2c3…` 认不出哪局是哪局。

        顺带给出它开在哪份设定上——那是会话列表里唯一需要的第二行信息（D172）。
        """
        out: list[dict[str, Any]] = []
        for sid in store.list_sessions():
            session = store.session(sid).load_session()
            out.append(
                {
                    "id": sid,
                    "title": session.meta.title,
                    "worldbook_id": session.worldbook_id,
                    "worldbook_name": store.worldbook(session.worldbook_id).load().name,
                }
            )
        return out

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
        return _apply_node_patch(repo, node_id, patch, developer=resolved.developer)

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

    @app.get("/sessions/{session_id}/trace/{run_id}")
    def read_trace(session_id: str, run_id: str) -> list[TraceEvent]:
        """回看某一轮落盘的 trace（D162）。

        **开发者面板离了它只能看当前连接期间的轮次**：事件是推过来的，刷新一次
        页面之前那些就没了。而开发者模式本来就强制落盘（D156）——查问题总是事后，
        记了不给读回来的路等于没记。

        没开发者模式时文件多半不存在，返回空列表而不是 404：那不是错误，
        只是这一轮当时没留痕。
        """
        return repo_of(session_id).read_trace(run_id)

    # -- worldbook（一级资产，D170） -----------------------------------------

    def book_of(worldbook_id: str) -> WorldbookRepository:
        repo = store.worldbook(worldbook_id)
        if not repo.exists():
            raise HTTPException(404, f"设定不存在：{worldbook_id}")
        return repo

    @app.get("/worldbooks")
    def list_worldbooks() -> list[dict[str, Any]]:
        """**带上「谁在用」**：一份设定可以开出好几个故事（D170），而删之前
        必须看得见这件事——被三局故事用着的设定被删掉，那三局会当场失忆。
        """
        return [
            {
                "worldbook": store.worldbook(wid).load(),
                "entries": len(store.worldbook(wid).entries()),
                "sessions": store.sessions_of(wid),
            }
            for wid in store.list_worldbooks()
        ]

    @app.post("/worldbooks")
    def create_worldbook(book: Worldbook | None = None) -> Worldbook:
        created = book or Worldbook()
        store.create_worldbook(created)
        return created

    @app.post("/worldbooks/import")
    def import_as_new(bundle: WorldbookBundle) -> Worldbook:
        """导入一份文件，**得到一份新设定**（D172）。

        和 `/worldbooks/{id}/import` 分开是因为它们回答不同的问题：那条是「往这
        一份里追加或替换」，这条是「把别人给我的这份变成我的一份」。**后者才是
        拿到分享文件时的动作**，而第一版只有前者——于是要先建个空的再往里导。
        """
        if (bad := bundle.check()) is not None:
            raise HTTPException(400, bad)

        book = Worldbook(name=bundle.name or "导入的设定")
        repo = store.create_worldbook(book)
        for entry in bundle.entries:
            # **id 一律重新分配**：文件里的 id 是别人存储里的地址，不是条目的身份。
            repo.save_entry(entry.model_copy(update={"id": new_id("wi")}))
        return book

    @app.get("/worldbooks/{worldbook_id}")
    def get_worldbook(worldbook_id: str) -> dict[str, Any]:
        repo = book_of(worldbook_id)
        return {
            "worldbook": repo.load(),
            "entries": repo.entries(),
            "sessions": store.sessions_of(worldbook_id),
            "initial_state": repo.compose_initial_state().values,
        }

    class Rename(BaseModel):
        name: str

    @app.patch("/worldbooks/{worldbook_id}")
    def patch_worldbook(worldbook_id: str, patch: Rename) -> Worldbook:
        """改名。**只有名字可改**——worldbook 上没有别的字段了（D175），
        题材与尺度都是 `WORLD` 条目，走 `/entries`。
        """
        repo = book_of(worldbook_id)
        book = repo.load().model_copy(update={"name": patch.name})
        repo.save(book)
        return book

    @app.delete("/worldbooks/{worldbook_id}")
    def delete_worldbook(worldbook_id: str) -> dict[str, Any]:
        """**有故事在用就不让删**，先说清是哪几局。"""
        book_of(worldbook_id)
        if users := store.delete_worldbook(worldbook_id):
            raise HTTPException(409, f"还有 {len(users)} 局故事开在这份设定上。")
        return {"deleted": worldbook_id}

    @app.get("/worldbooks/{worldbook_id}/entries")
    def list_entries(worldbook_id: str) -> list[WorldbookEntry]:
        return book_of(worldbook_id).entries()

    @app.put("/worldbooks/{worldbook_id}/entries")
    def put_entry(worldbook_id: str, entry: WorldbookEntry) -> WorldbookEntry:
        book_of(worldbook_id).save_entry(entry)
        return entry

    @app.delete("/worldbooks/{worldbook_id}/entries/{entry_id}")
    def delete_entry(worldbook_id: str, entry_id: str) -> dict[str, Any]:
        """删一条设定。故事开始之后删初始状态条目不影响既有的每一轮（D158）。"""
        if not book_of(worldbook_id).delete_entry(entry_id):
            raise HTTPException(404, f"设定条目不存在：{entry_id}")
        return {"deleted": entry_id}

    @app.get("/worldbooks/{worldbook_id}/export")
    def export_worldbook(worldbook_id: str) -> WorldbookBundle:
        """导出成一份可以拷走的文件。格式见 docs/design/WorldbookFormat.md。"""
        repo = book_of(worldbook_id)
        return WorldbookBundle.of(repo.entries(), name=repo.load().name)

    @app.post("/worldbooks/{worldbook_id}/import")
    def import_worldbook(
        worldbook_id: str, bundle: WorldbookBundle, replace: bool = False
    ) -> dict[str, Any]:
        """导入。``replace=True`` 先清空，否则追加。

        **id 一律重新分配**——文件里的 id 是别人存储里的地址，不是条目的身份。
        带着它导入要么撞上已有条目静默覆盖，要么两份文件里的同名 id 指向完全
        不同的东西。

        **不改已经冻结的初始状态**（D158）：故事一旦开始，起点就定死了，导入
        只影响"以后新建的会话"。还没开始时导入会立刻改变当前状态——那时没有
        任何一轮依赖它，改了是对的。**这里报的是「有几局故事已经开始了」**——
        一份设定可以开出好几个（D170），而它们各自冻结在各自的第一轮。
        """
        if (bad := bundle.check()) is not None:
            raise HTTPException(400, bad)

        repo = book_of(worldbook_id)
        removed = repo.clear() if replace else 0
        if replace and bundle.name:
            # 整份换掉时名字也跟着换。**只剩名字要换**——题材与尺度是条目
            # （D175），它们随 `entries` 一起进来。
            repo.save(repo.load().model_copy(update={"name": bundle.name}))
        for entry in bundle.entries:
            repo.save_entry(entry.model_copy(update={"id": new_id("wi")}))
        return {
            "imported": len(bundle.entries),
            "removed": removed,
            "frozen": [
                sid
                for sid in store.sessions_of(worldbook_id)
                if store.session(sid).load_session().initial_state is not None
            ],
        }

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
                await _handle(raw, runner, repo, broadcast, developer=resolved.developer)
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
    *,
    developer: bool = False,
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
            developer=developer,
        )
        await broadcast(StateSnapshot(values=repo.state_at(node.id).values, leaf_id=node.id))

    elif isinstance(message, SetGoal):
        # **同一时间只有一个 goal**（D148）。已有的必须先放弃——goal_manager 的
        # 对话是围着那个目标建起来的，换一个就得整份重来，而那正是 `abandon`。
        session = repo.load_session()
        if session.meta.goal is not None:
            await broadcast(ServerError(message="已经有一个目标在跑。先放弃它再设新的。"))
        else:
            # **留空就是不设上限**（D164）：两条预算原样透传，`None` 一路带到
            # `Goal`，不在这里替用户填一个默认值。
            session.meta.goal = Goal(
                description=message.description,
                max_runs=message.max_runs,
                max_total_tokens=message.max_total_tokens,
            )
            # **记下从哪儿开始**（D150）：它之前的剧情给细纲、进静态前缀，
            # 之后的每一轮给正文、逐条追加。
            session.goal_run = GoalRun(started_at_leaf_id=session.active_leaf_id)
            repo.save_session(session)
            # **设目标就是"去做"**（D165）。不叫醒的话，一个还没写过任何一轮的
            # 会话会举着「目标推进中」一动不动，直到用户随便打句话把它推起来。
            runner.nudge()

    elif isinstance(message, SetGoalBudget):
        # **这是"加预算接着跑"的入口。** 目标本身禁改，预算恰恰要能在跑到一半
        # 时调大——预算耗尽是暂停不是失败（D148）。调大之后顺手解除那次暂停。
        session = repo.load_session()
        if session.meta.goal is None:
            await broadcast(ServerError(message="当前没有目标。"))
        else:
            if message.unlimited:
                # 两条一起去掉。**只去掉一条没有意义**：另一条照样会把它拦下来，
                # 而用户按的那个按钮写的是"不设上限"。
                session.meta.goal.max_runs = None
                session.meta.goal.max_total_tokens = None
            if message.max_runs is not None:
                session.meta.goal.max_runs = message.max_runs
            if message.max_total_tokens is not None:
                session.meta.goal.max_total_tokens = message.max_total_tokens
            run = session.goal_run
            resumed = run is not None and run.paused and run.exhausted(session.meta.goal) is None
            if resumed and run is not None:
                run.paused, run.paused_reason = False, ""
            repo.save_session(session)
            # 加完预算它得自己接着跑——否则「加预算」只是把数字改大，还得再手动
            # 推一把才动。
            if resumed:
                runner.nudge()

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
            if not run.paused:
                runner.nudge()

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


def _apply_node_patch(
    repo: SessionRepository, node_id: str, patch: NodePatch, *, developer: bool = False
) -> Node:
    """用户编辑直接改写节点数据（D65），**不做依赖检查**。

    此前曾主张"插入一个只含 delta 的新节点"以保护已提交节点的不可变性，
    该方案已否决——它为一个乐观假设下不存在的问题增加复杂度，
    也不符合常见 AI chat 的编辑语义。

    **改了什么要入 trace**（D50）。`USER_EDIT` 这个事件类型一直定义着却从没被
    发出过——于是一段手改过的正文在 trace 里和 writer 写的一模一样，回看时会被
    当成模型的产出去归因，而 trace 存在的头一个理由就是 eval（D8）。
    记的是**改了哪几样**，不是新旧内容：正文动辄几千字，而"这个节点被人动过"
    才是回看时要的那一位信息。
    """
    node = repo.load_node(node_id)
    touched: list[str] = []
    if patch.outline is not None:
        node.outline = patch.outline
        node.outline_summary = None  # 细纲变了，缓存的摘要作废
        touched.append("outline")
    if patch.prose is not None:
        node.prose = patch.prose
        touched.append("prose")
    if patch.state_set is not None:
        node.state_delta.set = patch.state_set
        touched.append("state")
    repo.save_node(node)

    if touched:
        session = repo.load_session()
        Tracer(
            repo,
            node_id,
            persist=session.capabilities.trace or developer,
            developer=developer,
        ).emit(
            TraceEventType.USER_EDIT,
            Actor.USER,
            f"用户改了{'、'.join(_EDIT_LABEL[t] for t in touched)}",
            {"fields": touched},
        )
    return node


_EDIT_LABEL = {"outline": "细纲", "prose": "正文", "state": "状态增量"}
