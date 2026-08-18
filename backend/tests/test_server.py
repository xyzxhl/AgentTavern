"""服务端：REST、WebSocket、队列与挂起语义。"""

import json
from pathlib import Path

import pytest
from conftest import GOAL_MARK, ScriptedLLM, text, tool, tools
from fastapi.testclient import TestClient

from agenttavern.config import Settings
from agenttavern.models import Goal, Session, SessionMeta, Worldbook
from agenttavern.server import create_app
from agenttavern.storage import SessionRepository


def one_run(
    place: str = "客厅", outline: str = "她推开门，屋里坐着陈。", prose: str = "门轴响了。"
):
    """一个完整 run 需要的两次模型调用：submit_outline → 正文。

    状态那一次不在这里——它跑在 writer 之后，由 `ScriptedLLM(state=...)` 应答
    （D143）。``place`` 因此返回的是那份增量，交给调用方传进去。
    """
    return [
        tool("submit_outline", outline=outline),
        text(prose),
    ]


def one_state(place: str = "客厅") -> dict:
    return {"set": {"地点": place}}


def happy_llm(prose: str = "门轴发出一声长长的呻吟。") -> ScriptedLLM:
    return ScriptedLLM(*one_run(prose=prose), state=one_state())


@pytest.fixture
def client_for(tmp_path: Path):
    def build(llm: ScriptedLLM, *, developer: bool = False, stream: bool = True):
        # `stream` / `trace` 是**服务级**的（D174）：一条关于交付，一条关于
        # 运维，两条都跟模型收到什么无关，所以它们没跟着配置一起上调到提示词层。
        app = create_app(
            Settings(data_dir=tmp_path, llm_model="m", developer=developer, stream=stream),
            gateway=llm.gateway(),
        )
        return TestClient(app)

    return build


def book_of(client, session_id: str) -> str:
    """这个 session 开在哪份设定上（D170）。**建会话时会自动配一份空的。**"""
    return client.get(f"/sessions/{session_id}").json()["session"]["worldbook_id"]


def collect(ws, until: str, limit: int = 80) -> list[dict]:
    """读到某个类型的消息为止。

    ``run_failed`` 视同终止——否则等不到 ``run_finished`` 时会一直阻塞，
    把测试失败变成挂起。
    """
    seen: list[dict] = []
    for _ in range(limit):
        message = json.loads(ws.receive_text())
        seen.append(message)
        if message["type"] == until:
            return seen
        if message["type"] == "run_failed" and until != "run_failed":
            raise AssertionError(f"run 失败了：{message['reason']}")
    raise AssertionError(f"没等到 {until}，收到：{[m['type'] for m in seen]}")


# -- REST --------------------------------------------------------------------


def test_session_crud(client_for) -> None:
    client = client_for(happy_llm())
    session = client.post("/sessions").json()

    patched = client.patch(
        f"/sessions/{session['id']}",
        json={"meta": {"title": "雨夜", "prose_style": "短句，冷调"}},
    ).json()
    assert patched["meta"]["title"] == "雨夜"

    # **列表带标题，不只是 id**（D172）：一串 `sess_a1b2c3…` 认不出哪局是哪局。
    # 顺带给出它开在哪份设定上——那是列表里唯一需要的第二行信息。
    listed = client.get("/sessions").json()
    assert listed == [
        {
            "id": session["id"],
            "title": "雨夜",
            "worldbook_id": session["worldbook_id"],
            "worldbook_name": "未命名",
        }
    ]

    detail = client.get(f"/sessions/{session['id']}").json()
    assert detail["path"] == []
    assert detail["state"] == {}


def test_missing_session_is_404(client_for) -> None:
    client = client_for(happy_llm())
    assert client.get("/sessions/nope").status_code == 404


def test_worldinfo_roundtrip(client_for) -> None:
    client = client_for(happy_llm())
    book = client.post("/worldbooks").json()["id"]

    client.put(
        f"/worldbooks/{book}/entries",
        json={"id": "wi_1", "name": "教团", "content": "很长的设定"},
    )
    entries = client.get(f"/worldbooks/{book}/entries").json()
    assert entries[0]["name"] == "教团"


def test_a_worldbook_can_open_several_sessions(client_for) -> None:
    """**一份设定开出好几个故事**（D170 推翻了 D92 的一对一）。

    它们共用同一批条目——改一次，所有故事都跟着，而那正是把 worldbook 独立
    出来要买的东西。已经发生过的剧情所依赖的事实是另一回事：那部分在各自的
    第一轮就冻结了（D158），互不影响。
    """
    client = client_for(happy_llm())
    book = client.post("/worldbooks", json={"name": "旧城区"}).json()["id"]
    client.put(f"/worldbooks/{book}/entries", json={"name": "教团", "content": "总部在旧城区。"})

    ids = [client.post("/sessions", json={"worldbook_id": book}).json()["id"] for _ in range(2)]

    listed = client.get("/worldbooks").json()[0]
    assert listed["worldbook"]["name"] == "旧城区"
    assert sorted(listed["sessions"]) == sorted(ids), "得看得见谁在用"
    assert listed["entries"] == 1

    # **有故事在用就不让删**：删掉的话那几局会当场失忆，而界面上什么都不会说。
    assert client.delete(f"/worldbooks/{book}").status_code == 409
    for sid in ids:
        assert book_of(client, sid) == book


def test_a_new_session_gets_its_own_worldbook_when_none_is_named(client_for) -> None:
    """**先开故事、边写边补设定**也要走得通（D170）。

    要求先建 worldbook 才能开会话，等于在最没有想法的那一刻逼人做决定。
    """
    client = client_for(happy_llm())
    a, b = (client.post("/sessions").json()["id"] for _ in range(2))

    assert book_of(client, a) and book_of(client, b)
    assert book_of(client, a) != book_of(client, b), "各自一份，不共用"


def test_deleting_a_state_entry_before_the_story_starts_takes_effect(client_for) -> None:
    """初始状态是所有分支的共同起点（D90/D158）。

    **故事开始之前**改它立刻生效——那时还没有任何一轮依赖它。开始之后就冻结了，
    见 `test_the_initial_state_freezes_once_the_story_starts`。
    """
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]
    book = book_of(client, session_id)

    client.put(
        f"/worldbooks/{book}/entries",
        json={
            "id": "wi_1",
            "name": "陈屿·初始状态",
            "kind": "state",
            "values": {"陈屿": {"在场": True}},
        },
    )
    assert client.get(f"/sessions/{session_id}").json()["state"] == {"陈屿": {"在场": True}}

    assert client.delete(f"/worldbooks/{book}/entries/wi_1").json() == {"deleted": "wi_1"}
    assert client.get(f"/worldbooks/{book}/entries").json() == []
    assert client.get(f"/sessions/{session_id}").json()["state"] == {}

    # 删不存在的要报错，不能静默成功——否则前端会以为删掉了。
    assert client.delete(f"/worldbooks/{book}/entries/wi_1").status_code == 404


# -- worldbook 导入导出（格式见 docs/design/WorldbookFormat.md） -----------------


def _bundle(client, book: str) -> dict:
    return client.get(f"/worldbooks/{book}/export").json()


def test_export_drops_the_id_and_the_cached_summary(client_for) -> None:
    """**导出的是「有哪些设定」，不是「它们在谁的硬盘上排第几」。**

    - `id` 是这份存储里的地址，不是条目的身份。带着它导入要么撞上已有条目静默
      覆盖，要么两份文件里的同名 id 指向完全不同的东西。
    - `summary` 是懒惰生成的缓存（D69），跟着 `content` 走。`content` 一旦被人
      改过旧摘要就是错的，而导入方无从判断改没改——**缓存不该跨机器传播**。
    """
    client = client_for(happy_llm())
    book = client.post("/worldbooks").json()["id"]
    client.put(
        f"/worldbooks/{book}/entries",
        json={"id": "wi_1", "name": "陈屿", "content": "旧货店老板。", "summary": "老板"},
    )

    bundle = _bundle(client, book)
    assert bundle["format"] == "agenttavern.worldbook"
    assert bundle["version"] == 1
    assert bundle["entries"][0]["name"] == "陈屿"
    assert bundle["entries"][0]["id"] == ""
    assert bundle["entries"][0]["summary"] is None


def test_import_reassigns_ids_so_nothing_is_silently_overwritten(client_for) -> None:
    """同一份文件导两次，应当得到两批条目，而不是第二次把第一次盖掉。"""
    client = client_for(happy_llm())
    book = client.post("/worldbooks").json()["id"]
    bundle = {
        "format": "agenttavern.worldbook",
        "version": 1,
        "entries": [{"id": "wi_x", "name": "陈屿", "content": "旧货店老板。"}],
    }

    for _ in range(2):
        client.post(f"/worldbooks/{book}/import", json=bundle)

    entries = client.get(f"/worldbooks/{book}/entries").json()
    assert len(entries) == 2
    assert len({e["id"] for e in entries}) == 2, "id 必须重新分配"
    assert all(e["id"] != "wi_x" for e in entries)


def test_import_can_replace_instead_of_append(client_for) -> None:
    client = client_for(happy_llm())
    book = client.post("/worldbooks").json()["id"]
    client.put(f"/worldbooks/{book}/entries", json={"name": "旧的"})

    body = client.post(
        f"/worldbooks/{book}/import?replace=true",
        json={
            "format": "agenttavern.worldbook",
            "version": 1,
            "entries": [{"name": "新的"}],
        },
    ).json()

    # `frozen` 报的是**哪几局故事已经开始了**：一份设定可以开出好几个（D170），
    # 而它们各自冻结在各自的第一轮，导入只影响还没开始的那些。
    assert body == {"imported": 1, "removed": 1, "frozen": []}
    assert [e["name"] for e in client.get(f"/worldbooks/{book}/entries").json()] == ["新的"]


def test_an_unknown_format_is_refused_instead_of_read_best_effort(client_for) -> None:
    """**不做尽力而为的解析。**

    读一份半懂的格式会把坏数据导进来，用户以为成了，几轮之后才发现不对，
    而那时已经分不清是导入丢的还是模型写坏的。
    """
    client = client_for(happy_llm())
    book = client.post("/worldbooks").json()["id"]

    for bad in (
        {"format": "sillytavern.lorebook", "version": 1, "entries": [{"name": "x"}]},
        {"format": "agenttavern.worldbook", "version": 99, "entries": [{"name": "x"}]},
        {"format": "agenttavern.worldbook", "version": 1, "entries": []},
    ):
        assert client.post(f"/worldbooks/{book}/import", json=bad).status_code == 400
    assert client.get(f"/worldbooks/{book}/entries").json() == []


def test_a_round_trip_keeps_the_kinds_and_the_expansion_settings(client_for) -> None:
    """三类条目、可展开开关、展开层级都要原样过一遍（D158）。"""
    client = client_for(happy_llm())
    a = client.post("/worldbooks", json={"name": "旧城区"}).json()["id"]
    for entry in (
        # **题材也是一条条目**（D175）：worldbook 上没有字段了。
        {"name": "题材与基调", "kind": "world", "content": "悬疑"},
        {
            "name": "旧货店",
            "kind": "world",
            "mode": "on_demand",
            "content": "老城区一条窄巷尽头。",
        },
        {
            "name": "林昭",
            "kind": "character",
            "is_player": True,
            "content": "记者。",
            "voice": "话少，先观察再开口",
        },
        {
            "name": "起点",
            "kind": "state",
            "mode": "on_demand",
            "depth": 2,
            "values": {"陈屿": {"在场": True}},
        },
    ):
        client.put(f"/worldbooks/{a}/entries", json=entry)

    b = client.post("/worldbooks").json()["id"]
    client.post(f"/worldbooks/{b}/import?replace=true", json=_bundle(client, a))

    landed = {e["name"]: e for e in client.get(f"/worldbooks/{b}/entries").json()}
    assert landed["旧货店"]["kind"] == "world" and landed["旧货店"]["mode"] == "on_demand"
    # **用户扮演的角色是一般角色的子类**（D159）：它是 `character` + 一个标记，
    # 不是第四种条目。语言习惯也要跟着过来。
    assert landed["林昭"]["kind"] == "character" and landed["林昭"]["is_player"] is True
    assert landed["林昭"]["voice"] == "话少，先观察再开口"
    assert landed["起点"]["kind"] == "state" and landed["起点"]["depth"] == 2
    # 初始状态跟着过来；开在这份设定上的会话还没开始，所以立刻生效。
    landed_session = client.post("/sessions", json={"worldbook_id": b}).json()["id"]
    assert client.get(f"/sessions/{landed_session}").json()["state"] == {"陈屿": {"在场": True}}
    # **题材跟着条目走**（D175）：worldbook 上除了名字没有别的字段，所以它必然
    # 和别的设定同路——这正是"不特殊处理"买到的东西。
    assert landed["题材与基调"]["content"] == "悬疑"
    assert client.get(f"/worldbooks/{b}").json()["worldbook"]["name"] == "旧城区"


# -- WebSocket：一个完整 run --------------------------------------------------


def test_submit_drives_a_run(client_for) -> None:
    llm = happy_llm()
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        assert json.loads(ws.receive_text())["type"] == "state"

        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

        kinds = [m["type"] for m in messages]
        assert "queue" in kinds
        assert "trace" in kinds

        finished = next(m for m in messages if m["type"] == "run_finished")
        assert finished["node"]["prose"] == "门轴发出一声长长的呻吟。"

        # 成本可见性：实时给出消耗，由用户决定要不要打断。
        usage = collect(ws, "usage")[-1]
        assert usage["tokens_in"] > 0

        state = collect(ws, "state")[-1]
        assert state["values"]["地点"] == "客厅"


def test_trace_events_carry_readable_labels(client_for) -> None:
    """UI 是 trace 的投影（D25）：服务端递出完整事件，客户端决定显示什么。"""
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    labels = [m["event"]["label"] for m in messages if m["type"] == "trace"]
    assert "开始新的一轮" in labels
    assert "细纲完成" in labels
    assert all(labels)


# -- ask_question：挂起 / 恢复，无超时 ----------------------------------------


def test_ask_question_suspends_until_answered(client_for) -> None:
    """不设超时（D66）——等待期间逻辑上什么都没发生，恢复即从中断点续。"""
    llm = ScriptedLLM(
        tool("ask_question", question="她怎么反应？", options=["否认", "沉默", "反问"]),
        tool("submit_outline", outline="她沉默了。"),
        text("她没有说话。"),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我问她"}))

        question = collect(ws, "question")[-1]
        assert question["options"] == ["否认", "沉默", "反问"]

        ws.send_text(
            json.dumps(
                {
                    "type": "answer",
                    "question_id": question["question_id"],
                    "selected": "沉默",
                }
            )
        )
        finished = collect(ws, "run_finished")[-1]
        assert finished["node"]["prose"] == "她没有说话。"


def test_an_answer_outside_the_options_is_passed_through(client_for) -> None:
    """**用户可以都不选、自己写一条**（D155）。

    服务端不校验 `selected` 在不在选项里——校验等于把"模型没想到的第四条路"
    判为非法，而那恰恰是最该传下去的一种回答。
    """
    llm = ScriptedLLM(
        tool("ask_question", question="她怎么反应？", options=["否认", "沉默", "反问"]),
        tool("submit_outline", outline="她把杯子推了过去。"),
        text("杯子在桌面上滑了半尺。"),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我问她"}))
        question = collect(ws, "question")[-1]

        ws.send_text(
            json.dumps(
                {
                    "type": "answer",
                    "question_id": question["question_id"],
                    "selected": "都不选：她把杯子推过来，不接话。",
                }
            )
        )
        finished = collect(ws, "run_finished")[-1]
        assert finished["node"]["prose"] == "杯子在桌面上滑了半尺。"

    # **模型必须知道这不是三选一**：回执分开写，否则它会照着"这是我列过的选项"
    # 去理解一句它根本没列过的话。
    asked = next(
        m
        for req in llm.requests
        for m in req["messages"]
        if m.get("role") == "tool" and "杯子" in (m.get("content") or "")
    )
    assert "没有选给出的选项" in asked["content"]


def test_answering_an_unknown_question_errors(client_for) -> None:
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "answer", "question_id": "q_x", "selected": "甲"}))
        assert collect(ws, "error")[-1]["message"]


# -- 队列 --------------------------------------------------------------------


def test_queued_inputs_run_one_at_a_time(client_for) -> None:
    """FIFO，每条独立成一个 run，**不打包**（D26）——打包会跳过中间的世界响应。"""
    llm = ScriptedLLM(
        *one_run(),
        *one_run(outline="两人出去。"),
        state=[one_state("客厅"), one_state("阳台")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        ws.send_text(json.dumps({"type": "submit", "text": "我环顾四周"}))

        first = collect(ws, "run_finished")[-1]
        second = collect(ws, "run_finished")[-1]

    assert first["node"]["id"] != second["node"]["id"]
    assert second["node"]["parent_id"] == first["node"]["id"]
    assert first["node"]["user_input"]["text"] == "我推开门"
    assert second["node"]["user_input"]["text"] == "我环顾四周"


def test_queue_status_is_visible(client_for) -> None:
    """用户应能看到队列中待处理的输入。"""
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        assert collect(ws, "queue")[-1]["pending"] == ["第一条"]


# -- 分支与编辑 ---------------------------------------------------------------


def test_switch_branch_replays_state(client_for) -> None:
    """切换分支必须同时切换状态（D48），否则"文本对、状态悄悄错"。"""
    llm = happy_llm()
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        first = collect(ws, "run_finished")[-1]["node"]["id"]

        ws.send_text(json.dumps({"type": "switch_branch", "node_id": first}))
        snapshot = collect(ws, "state")[-1]
        assert snapshot["leaf_id"] == first
        assert snapshot["values"]["地点"] == "客厅"


def test_a_branch_can_only_be_opened_on_the_current_node(client_for) -> None:
    """**分支只能在当前节点上开**（D147）：重生成非当前节点直接报错。

    以前提交时可以显式指定任意 `parent_id`，于是能在历史上任何一点分叉。收掉它
    是因为「当前节点」从此有唯一含义——想在更早的地方分叉，**先切过去再动手**。
    当场报错而不是静默改写：悄悄重 roll 掉另一轮比报错难查得多。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。"),
        *one_run(outline="第二轮。"),
        state=[one_state("客厅"), one_state("阳台")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        first = collect(ws, "run_finished")[-1]["node"]["id"]
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        collect(ws, "run_finished")

        # 当前叶是第二轮，去重 roll 第一轮——拒绝。
        ws.send_text(json.dumps({"type": "regenerate", "node_id": first}))
        assert "只能重新生成当前节点" in collect(ws, "error")[-1]["message"]


def test_switching_to_an_earlier_node_is_not_restricted(client_for) -> None:
    """**受限的只是创建，不是切换。** 切到更早的节点仍然随意——
    任意分叉因此仍然可达，只是要"先切过去再重生成"两步走。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。"),
        *one_run(outline="第二轮。"),
        *one_run(outline="另一个第二轮。"),
        state=[one_state("客厅"), one_state("阳台"), one_state("门口")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        first = collect(ws, "run_finished")[-1]["node"]["id"]
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        second = collect(ws, "run_finished")[-1]["node"]["id"]

        # 切回第一轮，此刻它就是当前节点，于是可以在它这里分叉。
        ws.send_text(json.dumps({"type": "switch_branch", "node_id": first}))
        collect(ws, "state")
        ws.send_text(json.dumps({"type": "regenerate", "node_id": first}))
        third = collect(ws, "run_finished")[-1]["node"]

    assert third["id"] not in {first, second}
    assert third["parent_id"] is None, "重生成开的是第一轮的兄弟"


def test_user_edit_rewrites_node_directly(client_for) -> None:
    """用户编辑**直接改写节点数据**，不做依赖检查（D65）。"""
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        node_id = collect(ws, "run_finished")[-1]["node"]["id"]

    patched = client.patch(
        f"/sessions/{session_id}/nodes/{node_id}",
        json={"prose": "门开了。", "state_delta": {"set": {"地点": "阳台"}}},
    ).json()

    assert patched["prose"] == "门开了。"
    assert client.get(f"/sessions/{session_id}").json()["state"]["地点"] == "阳台"


def test_switching_back_to_a_regenerated_sibling_restores_its_state(client_for) -> None:
    """重生成之后**切回原来那一支**，状态跟着回去（D48）。

    `⟨2/3⟩` 是回到历史分叉点的唯一路径（D147）——切换器要能用，靠的就是这条：
    切过去之后状态得是那一支的，否则就是"文本对、状态悄悄错"。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。"),
        *one_run(outline="第二轮。"),
        *one_run(outline="另一个第二轮。"),
        state=[one_state("客厅"), one_state("阳台"), one_state("门口")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        collect(ws, "run_finished")
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        second = collect(ws, "run_finished")[-1]["node"]["id"]

        ws.send_text(json.dumps({"type": "regenerate", "node_id": second}))
        third = collect(ws, "run_finished")[-1]["node"]["id"]
        collect(ws, "state")  # 这一轮自己的快照，读掉
        assert client.get(f"/sessions/{session_id}").json()["state"]["地点"] == "门口"

        ws.send_text(json.dumps({"type": "switch_branch", "node_id": second}))
        # 切换的确认里就带着新叶子的状态——前端据此认出"路径整条换过了"。
        snapshot = collect(ws, "state")[-1]

    assert snapshot["leaf_id"] == second
    assert snapshot["values"]["地点"] == "阳台"

    detail = client.get(f"/sessions/{session_id}").json()
    assert [n["id"] for n in detail["path"]][-1] == second
    assert set(detail["siblings"][second]) == {second, third}, "切回来之后切换器照样在"


def test_editing_state_reaches_append_and_remove(client_for) -> None:
    """**改指令是整份替换增量，不只是 `set`。**

    以前这里是 `node.state_delta.set = ...`，于是 D96 补的 `remove`（以及
    `append`）从界面根本够不着——"清单要能减"只有模型做得到。而清单要能减这件事
    正是数据化世界里最先暴露的：消耗品会用完，随从会离队。
    """
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        node_id = collect(ws, "run_finished")[-1]["node"]["id"]

    client.patch(
        f"/sessions/{session_id}/nodes/{node_id}",
        json={"state_delta": {"set": {"沈": {"随身": ["青锋剑", "灵石"]}}}},
    )
    client.patch(
        f"/sessions/{session_id}/nodes/{node_id}",
        json={
            "state_delta": {
                "set": {"沈": {"随身": ["青锋剑", "灵石"]}},
                "append": {"沈": {"随身": ["止血符"]}},
                "remove": {"沈": {"随身": ["灵石"]}},
            }
        },
    )

    state = client.get(f"/sessions/{session_id}").json()["state"]
    assert state["沈"]["随身"] == ["青锋剑", "止血符"]


def test_editing_the_result_is_diffed_back_into_a_delta(client_for) -> None:
    """「改结果」的落地方式（D65）：收下你想要的状态树，和这一轮**之前**求差。

    换算发生在进门那一刻，落盘的仍然只有增量——「**权威是增量**」（D48）
    这句话不会因为多了个视图就被破坏。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。"),
        *one_run(outline="第二轮。"),
        state=[one_state("客厅"), one_state("阳台")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        collect(ws, "run_finished")
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        second = collect(ws, "run_finished")[-1]["node"]["id"]

    # 「改结果」的起点不是侧栏里的当前状态，是**这一轮跑完时**的那一份。
    at = client.get(f"/sessions/{session_id}/nodes/{second}/state").json()
    assert at["values"]["地点"] == "阳台" and at["before"]["地点"] == "客厅"

    want = {"地点": "天台", "林": {"在场": True}}
    patched = client.patch(
        f"/sessions/{session_id}/nodes/{second}", json={"state_result": want}
    ).json()

    assert client.get(f"/sessions/{session_id}").json()["state"] == want
    assert patched["state_delta"]["set"]["地点"] == "天台"
    assert patched["state_delta"]["set"].get("林") == {"在场": True}


def test_the_two_state_views_cannot_both_be_given(client_for) -> None:
    """一起给就不知道该听谁的。**当场报错而不是挑一个**——"我改了结果，它却按
    指令存了"是查不出来的那种错。
    """
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        node_id = collect(ws, "run_finished")[-1]["node"]["id"]

        res = client.patch(
            f"/sessions/{session_id}/nodes/{node_id}",
            json={"state_delta": {"set": {"a": 1}}, "state_result": {"b": 2}},
        )
        assert res.status_code == 422

        # WS 那侧不能因此把连接掀了——一条坏消息只是一条坏消息。
        ws.send_text(
            json.dumps(
                {
                    "type": "edit_node",
                    "node_id": node_id,
                    "state_delta": {"set": {"a": 1}},
                    "state_result": {"b": 2},
                }
            )
        )
        assert "只能给一个" in collect(ws, "error")[-1]["message"]
        ws.send_text(json.dumps({"type": "edit_node", "node_id": node_id, "prose": "还活着。"}))
        # **改完的节点整份回传**：改「结果」时落盘的增量是服务端求差算出来的，
        # 客户端只把发出去的东西塞回本地节点，那份增量就成了旧的。
        assert collect(ws, "node_updated")[-1]["node"]["prose"] == "还活着。"


def test_editing_an_older_node_keeps_the_state_at_the_leaf(client_for) -> None:
    """改的是历史某一轮时，侧栏仍然显示**路径末端**的状态。

    发被改节点处的快照会让侧栏往回跳——而用户看的从来是"现在是什么样"。
    下游照常重放，那是 event-sourced 的正常行为（D48/D49），不是连锁事故。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。"),
        *one_run(outline="第二轮。"),
        state=[one_state("客厅"), one_state("阳台")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        first = collect(ws, "run_finished")[-1]["node"]["id"]
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        second = collect(ws, "run_finished")[-1]["node"]["id"]
        collect(ws, "state")  # 这一轮自己的快照，读掉，别和下面那个混起来

        ws.send_text(
            json.dumps(
                {
                    "type": "edit_node",
                    "node_id": first,
                    "state_delta": {"set": {"天气": "雨"}},
                }
            )
        )
        snapshot = collect(ws, "state")[-1]

    assert snapshot["leaf_id"] == second
    assert snapshot["values"]["地点"] == "阳台", "第二轮的增量照常盖在上面"
    assert snapshot["values"]["天气"] == "雨", "改的那一笔也在"


def test_editing_outline_invalidates_cached_summary(client_for) -> None:
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        node_id = collect(ws, "run_finished")[-1]["node"]["id"]

    patched = client.patch(
        f"/sessions/{session_id}/nodes/{node_id}", json={"outline": "改过的细纲"}
    ).json()
    assert patched["outline_summary"] is None


def test_siblings_endpoint(client_for) -> None:
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        node_id = collect(ws, "run_finished")[-1]["node"]["id"]

    branches = client.get(f"/sessions/{session_id}/nodes/{node_id}/siblings").json()
    assert branches["siblings"] == [node_id]
    assert branches["active_index"] == 0


def test_session_detail_carries_the_sibling_map(client_for) -> None:
    """**哪几轮有兄弟随路径一起给**（D46/D147）。

    切换器要显示 `⟨2/3⟩` 就得先知道有没有第 2 支，而一个节点一次 `siblings`
    请求会让一条 20 轮的路径打 20 个请求——那份数据只在重生成之后才变。

    只列有兄弟的那些：绝大多数节点是独苗，全列一遍等于把路径抄两份。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。"),
        *one_run(outline="第二轮。"),
        *one_run(outline="另一个第二轮。"),
        state=[one_state("客厅"), one_state("阳台"), one_state("门口")],
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "第一条"}))
        first = collect(ws, "run_finished")[-1]["node"]["id"]
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        second = collect(ws, "run_finished")[-1]["node"]["id"]

        assert client.get(f"/sessions/{session_id}").json()["siblings"] == {}, "都是独苗"

        ws.send_text(json.dumps({"type": "regenerate", "node_id": second}))
        third = collect(ws, "run_finished")[-1]["node"]["id"]

    detail = client.get(f"/sessions/{session_id}").json()
    assert list(detail["siblings"]) == [third], "只有当前路径上有兄弟的那一轮"
    assert set(detail["siblings"][third]) == {second, third}
    assert first not in detail["siblings"]


# -- goal 驱动的自动续跑 -------------------------------------------------------


def test_goal_drives_another_run_with_a_simulated_input(client_for) -> None:
    """goal_manager 在 run 末跑，未达标就开下一 run（D28）——**而且它替用户写输入**。

    第一轮之后还没有中间状态，所以那一次是 `plan` + `steer`：下一条输入就在这一轮
    写好，下一轮直接能用。所以第二个 run 是有输入的，只不过那条不是人打的——
    它的 `source` 因此是 `simulated`（D148）。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["他先松口", "两人摊开说"]}),
            ("steer", {"next_input": "我把照片推过去。"}),
        ),
        text("先推到这里。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        tool("steer", next_input="再问一句。"),
        text("接着推。"),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=2)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        first = collect(ws, "run_finished")[-1]["node"]
        second = collect(ws, "run_finished")[-1]["node"]

    assert first["user_input"]["text"] == "开始"
    assert first["user_input"]["source"] == "user"
    # 第二轮没有人打字，输入是 goal_manager 写的那条，**标注成模拟**。
    assert second["user_input"]["text"] == "我把照片推过去。"
    assert second["user_input"]["source"] == "simulated"

    # 中间状态挂在 session 上（D148），不再挂节点——goal 跑着的时候不允许切分支，
    # D141 让它跟着节点走的那个理由（分支隔离）不成立了。
    run = client.get(f"/sessions/{session.id}").json()["session"]["goal_run"]
    assert [s["description"] for s in run["plan"]["subgoals"]] == ["他先松口", "两人摊开说"]
    assert run["spent_runs"] >= 1, "预算计数必须落盘——它以前在内存里，重启就归零"


def opening(next_input: str, *subgoals: str):
    """goal 开场那一次 goal_manager 调用（D168）：拆阶段 + 写出第一条输入，然后收口。"""
    return [
        tools(
            ("plan", {"subgoals": list(subgoals) or ["他先松口"]}),
            ("steer", {"next_input": next_input}),
        ),
        text("先走这一步。"),
    ]


def test_setting_a_goal_plans_first_then_runs(client_for) -> None:
    """**设目标就是"去做"**（D165），而且**先规划再动手**（D168）。

    两件事以前都不是这样：

    - 自动续跑只在一次 run 正常结束之后才打开，所以在空会话上设完目标什么都不会
      发生，界面举着「目标推进中」一动不动，直到用户随便打句话把它推起来。那条
      规则挡的是「带 goal 的 session 一连上就自己开始生成」——**连接不是指令，
      而设目标是**。
    - 打开之后第一轮是**空手推的**：`director` 从来不读 goal（模拟输入和真输入
      在它眼里必须一模一样，D148），所以最该定方向的那一轮完全不知道有目标这回
      事，goal_manager 要等它写完才第一次开口，然后照着一段随机开的剧情拆阶段。

    现在顺序是：goal_manager 规划 → 它写的输入 → director → writer。
    """
    llm = ScriptedLLM(
        *opening("我把照片推过去。", "他先松口", "两人摊开说"),
        *one_run(outline="开场。", prose="正文一。"),
        tool("steer", next_input="再问一句。"),
        text("接着推。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        # 一个字都没打，只是设了个目标。
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌"}))
        first = collect(ws, "run_finished")[-1]["node"]
        second = collect(ws, "run_finished")[-1]["node"]

    assert first["user_input"]["text"] == "我把照片推过去。", "第一轮就该是它写的输入"
    assert first["user_input"]["source"] == "simulated"
    assert second["user_input"]["text"] == "再问一句。"

    # **规划先于推演**：第一次问的是 goal_manager，不是 director。
    assert GOAL_MARK in llm.system_prompts[0]
    # 中间状态在第一轮落地之前就有了——否则面板上要空到第一轮写完。
    run = client.get(f"/sessions/{session_id}").json()["session"]["goal_run"]
    assert [s["description"] for s in run["plan"]["subgoals"]] == ["他先松口", "两人摊开说"]


def test_a_goal_set_mid_story_also_plans_before_it_runs(client_for) -> None:
    """**第几轮设的不影响这条**（D168）：先规划、写出输入，再交给 director。

    空会话和跑了几轮的会话唯一的差别在**它读到什么**：已经写出来的剧情进静态
    前缀、**给细纲**（"故事到这儿了"，够用而且便宜），goal 期间的每一轮才逐条
    追加、给正文（D150）。所以设目标那一刻路径末端就是切点，它之前一条都不算
    goal 期间的轮次。
    """
    llm = ScriptedLLM(
        *one_run(outline="用户自己写的那一轮。", prose="正文一。"),
        *opening("我把照片推过去。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        collect(ws, "run_finished")
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌"}))
        second = collect(ws, "run_finished")[-1]["node"]

    assert second["user_input"]["text"] == "我把照片推过去。"
    assert second["user_input"]["source"] == "simulated"

    planning = [r for r in llm.requests if GOAL_MARK in r["messages"][0]["content"]][0]
    body = "\n".join(m.get("content") or "" for m in planning["messages"])
    assert "用户自己写的那一轮。" in body, "goal 之前的剧情要给它，否则它凭空拆阶段"
    assert "正文一。" not in body, "但给的是细纲——那一段只回答「故事到这儿了」（D150）"
    assert "第 1 轮" not in body, "也不算 goal 期间的轮次：它进前缀，不是逐条追加的记录"


def test_the_opening_conversation_carries_into_the_first_judgement(client_for) -> None:
    """开场那次和紧接着的判定**是同一份对话**，中间不重建（D148/D168）。

    `sync_conversation` 只在"上次看到的节点还在 goal 期间那一段里"时才增量追加，
    否则整份重建。开场恰好卡在这个判据的缝上：那时 goal 期间一轮都还没有，
    上次看到的是**切点**那个节点。照字面判的话，goal 期间的第一轮就会触发一次
    重建——而那时对话完全是连续的，重建只是白白丢掉它开场时写计划的那段过程，
    然后拿一份 `plan_snapshot` 顶上。**恢复时那样降级是有理由的，这里没有。**
    """
    llm = ScriptedLLM(
        *opening("我把照片推过去。"),
        *one_run(outline="第一轮。", prose="正文一。"),
        tool("finish", outcome="done", reason="到了"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌"}))
        collect(ws, "run_finished")
        collect(ws, "goal_finished")

    judged = [r for r in llm.requests if GOAL_MARK in r["messages"][0]["content"]][-1]
    called = [c["function"]["name"] for m in judged["messages"] for c in m.get("tool_calls") or []]
    assert "steer" in called, "开场时它自己那几步还在对话里，没有被重建冲掉"
    assert "第 1 轮" in json.dumps(judged["messages"], ensure_ascii=False), "新的一轮照常追加"


def test_the_opening_plan_belongs_to_the_run_it_opens(client_for) -> None:
    """开场那几次调用**挂在它开出来的那一轮名下**（D168）。

    换句话说：宣告「开始新的一轮」必须排在规划**之前**。规划要跑几十秒，晚宣告
    的话界面上那段时间什么都没有，而这一轮的记录已经在往外推了——前端拿到一批
    属于"还没开始的一轮"的事件，只能扔掉。
    """
    llm = ScriptedLLM(
        *opening("我把照片推过去。"),
        *one_run(outline="开场。", prose="正文一。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌"}))
        messages = collect(ws, "run_finished")

    kinds = [m["type"] for m in messages]
    assert kinds.count("run_started") == 1, "只宣告一次——提前宣告过就不再重复"
    assert kinds.index("run_started") == 0, "宣告排在规划之前"

    run_id = messages[0]["run_id"]
    assert messages[-1]["node"]["id"] == run_id, "run = node，id 是同一个"
    planning = [
        m for m in messages if m["type"] == "trace" and m["event"]["actor"] == "goal_manager"
    ]
    assert planning, "规划留了记录"
    assert {m["event"]["run_id"] for m in planning} == {run_id}, "归到它开出来的那一轮"


def test_an_empty_budget_means_no_budget(client_for) -> None:
    """**留空就是不设上限**（D164）。

    以前留空会被填成配置里的 50 轮——那是替用户做决定：他没填不是"随便给个数"，
    是"别拦我"。而被一个自己没见过的数字在第 50 轮拦下来，只会以为出了故障。

    这里跑三轮：搁在旧默认值下这当然也过，所以真正的断言在落盘的那两个
    `None`，以及 `exhausted()` 一次都没喊停。
    """
    llm = ScriptedLLM(
        *opening("开个头。"),
        *one_run(outline="第一轮。", prose="正文一。"),
        tool("steer", next_input="再走一步。"),
        text("接着推。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        tool("steer", next_input="再来。"),
        text("接着推。"),
        *one_run(outline="第三轮。", prose="正文三。"),
        tool("finish", outcome="done", reason="摊牌了"),
        text("收工。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌"}))
        progress = []
        for _ in range(3):
            seen = collect(ws, "run_finished")
            progress += [m for m in seen if m["type"] == "goal_progress"]
        finished = collect(ws, "goal_finished")[-1]

    assert finished["outcome"] == "done"
    assert progress, "每轮都推一次进度（D161）"
    for p in progress:
        assert p["max_runs"] is None and p["max_total_tokens"] is None, "没设就是 None，不是 0"
    assert not [m for m in progress if m.get("type") == "goal_paused"]


def test_a_zero_budget_is_rejected_rather_than_silently_pausing(client_for) -> None:
    """`0` 表达不了任何意图：一设上就是"已经跑满"，goal 当场暂停。

    它最可能的来源是前端把空输入框转成了数字，而那时用户的意思恰恰是"不设上限"。
    静默接受的话，症状是"目标设完就自己暂停了"——没人会想到去看那个数字。
    """
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌", "max_runs": 0}))
        assert "格式错误" in collect(ws, "error")[-1]["message"]

    assert client.get(f"/sessions/{session_id}").json()["session"]["meta"]["goal"] is None


def test_a_goal_set_while_a_run_is_in_flight_survives_that_run(client_for) -> None:
    """**运行中设的目标不能被那一轮结束时的写回抹掉。**

    元操作与生成是并行的（D24），而 run 手里那份 `session` 是它开始那一刻的
    快照。以前它在末尾把整份写回去，于是这期间设的目标、以及**这一轮自己刚
    冻结的初始状态**（D158）一起没了——一个字的报错都没有。

    这里用 `ask_question` 把 run 挂在半路（D66：挂起不设超时），趁它挂着设目标。
    """
    llm = ScriptedLLM(
        tool("ask_question", question="往哪边走？", options=["左", "右", "回头"]),
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        *opening("接着走。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        question = collect(ws, "question")[-1]

        # run 正挂在问句上——这一刻设目标。
        ws.send_text(json.dumps({"type": "set_goal", "description": "写到摊牌"}))
        ws.send_text(
            json.dumps({"type": "answer", "question_id": question["question_id"], "selected": "左"})
        )
        collect(ws, "run_finished")

        # **先查落盘的那份再等下一轮。** 抹掉的症状是"下一轮永远不来"，而等一条
        # 永远不来的消息只会挂住——那样测试失败会变成超时，看不出是什么坏了。
        body = client.get(f"/sessions/{session_id}").json()["session"]
        assert body["meta"]["goal"] is not None, "运行中设的目标被那一轮的写回抹掉了"
        assert body["meta"]["goal"]["description"] == "写到摊牌"
        assert body["initial_state"] is not None, "这一轮冻结的初始状态也不该被写回抹掉"

        # 目标活下来了，所以它会自己开下一轮。
        second = collect(ws, "run_finished")[-1]["node"]

    # **正在跑的那一轮是唯一的例外**（D168）：它已经交给 director 了，抢不回来。
    # 目标从**下一轮**接管，而那一轮照常是「先规划、再动手」。
    assert second["user_input"]["text"] == "接着走。"
    assert second["user_input"]["source"] == "simulated"

    # **那一轮不算目标的**（D169）：它早就在跑了，判它、把它的开销记到目标账上，
    # 都是在为一件目标没参与的事结账。所以它进静态前缀、给细纲，和它之前的
    # 剧情一样——不是逐条追加的「第 N 轮」记录，也不进 `spent_runs`。
    asked = [r for r in llm.requests if GOAL_MARK in r["messages"][0]["content"]]
    first_words = "\n".join(m.get("content") or "" for m in asked[0]["messages"])
    assert "她推开门。" in first_words, "它仍然要知道故事到哪儿了"
    assert "门轴响了。" not in first_words, "但给的是细纲，不是正文——那一轮不是它的"
    assert "第 1 轮" not in first_words
    run = client.get(f"/sessions/{session_id}").json()["session"]["goal_run"]
    assert run["spent_runs"] == 1, "只有它自己开出来的那一轮算数"


def test_abandoning_during_judgement_leaves_nothing_behind(client_for, tmp_path: Path) -> None:
    """判定是一次真实的模型调用，跑几十秒——用户完全可能在这中间按放弃。

    照写回去的话会留下一份**没有主人的 `goal_run`**：它带着几十万字的对话，
    而且下次设目标时那份旧进度会当成新目标的进度显示出来。goal_manager 这一轮
    判出来的东西——包括它替用户写的下一条输入——也必须一起作废，否则用户放弃完
    还会莫名其妙再看到一轮自动推进。

    **放弃的时机要卡准**：从 WS 发一条"放弃"是抢不到这个窗口的，它会在判定开始
    之前就被处理掉，那时 `_check_goal` 一进门就返回了，走不到写回那一步。所以
    这里在**应答 goal_manager 那次请求的同一刻**直接改盘上的 session——那正好
    是判定跑到一半的时候。
    """
    goal_repo = SessionRepository(tmp_path, "s-abandon")

    def abandon() -> None:
        """用户此刻按下了放弃。"""
        session = goal_repo.load_session()
        session.meta.goal = None
        session.goal_run = None
        goal_repo.save_session(session)

    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tool("steer", next_input="我把照片推过去。"),
        text("先推到这里。"),
        *one_run(outline="不该发生的第二轮。"),
        state=one_state(),
        on_goal_request=abandon,
    )
    client = client_for(llm)
    session = Session(id="s-abandon", meta=SessionMeta(goal=Goal(description="写到摊牌")))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        collect(ws, "run_finished")
        # 拿一条会报错的消息当同步点，确认队列里没有别的东西在跑。
        ws.send_text(json.dumps({"type": "不存在的类型"}))
        assert "格式错误" in collect(ws, "error")[-1]["message"]

    detail = client.get(f"/sessions/{session.id}").json()
    assert detail["session"]["meta"]["goal"] is None
    assert detail["session"]["goal_run"] is None, "没有主人的运行时状态不该留在盘上"
    assert len(detail["path"]) == 1, "放弃之后不该再自动开一轮"


def test_progress_is_pushed_every_turn_not_only_at_the_end(client_for) -> None:
    """**中间状态被一条条划掉，是用户最想看的东西**（D161）。

    它此前只写在盘上的 `goal_run` 里，而 WS 那侧只有「结束」和「暂停」——
    也就是说 goal 跑着的那几轮，界面上什么都不会变，直到它突然结束。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["他先松口", "两人摊开说"]}),
            ("check", {"reached": [1]}),
            ("steer", {"next_input": "再走一步。"}),
        ),
        text("先推到这里。"),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=1)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        collect(ws, "run_finished")
        progress = collect(ws, "goal_progress")[-1]

    assert [s["description"] for s in progress["subgoals"]] == ["他先松口", "两人摊开说"]
    assert [s["done"] for s in progress["subgoals"]] == [True, False]
    assert progress["spent_runs"] == 1
    assert progress["max_runs"] == 1, "预算也要给——不给的话界面显示不出「还剩多少」"
    # **不带那份长对话**：它一轮就上十万字，而界面一个字都用不上。
    assert "conversation" not in progress


def test_a_finished_goal_says_which_way_it_ended(client_for) -> None:
    """两种终态都交回控制权，**但用户必须知道是哪一种**（D141/D148）。"""
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["唯一的一步"]}),
            ("check", {"reached": [1]}),
            ("finish", {"outcome": "done", "reason": "逐条核对：那一步已兑现"}),
        ),
        text("完了。"),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=5)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        collect(ws, "run_finished")
        finished = collect(ws, "goal_finished")[-1]

    assert finished["outcome"] == "done"
    assert "已兑现" in finished["reason"]
    # goal 与它的运行时状态一并清掉——**不留历史 goal**（D148）。
    body = client.get(f"/sessions/{session.id}").json()["session"]
    assert body["meta"]["goal"] is None
    assert body["goal_run"] is None


def test_a_spent_budget_pauses_instead_of_finishing(client_for) -> None:
    """**预算耗尽是暂停，不是失败**（D148）：用户可以把上限调大接着跑。

    所以推出去的是 `goal_paused` 而不是 `goal_finished`，而且 goal 还在
    session 上——对话原样留着，恢复时不用重新判一遍。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["甲", "乙"]}),
            ("steer", {"next_input": "再走一步。"}),
        ),
        text("先推到这里。"),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=1)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        collect(ws, "run_finished")
        paused = collect(ws, "goal_paused")[-1]

    assert "1 轮" in paused["reason"]
    body = client.get(f"/sessions/{session.id}").json()["session"]
    assert body["meta"]["goal"] is not None, "暂停不是结束，目标还在"
    assert body["goal_run"]["paused"] is True
    assert body["goal_run"]["conversation"], "对话原样留着，恢复时不用重新判"


def test_raising_the_budget_resumes_a_paused_goal(client_for) -> None:
    """**这就是"加预算接着跑"**（D148）。

    目标本身禁改（对话是围着它建的），预算恰恰要能在跑到一半时调大——
    两者可改的时机相反，所以是两条不同的消息。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tools(
            ("plan", {"subgoals": ["甲", "乙"]}),
            ("steer", {"next_input": "再走一步。"}),
        ),
        text("先推到这里。"),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=1)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        collect(ws, "run_finished")
        collect(ws, "goal_paused")

        ws.send_text(json.dumps({"type": "set_goal_budget", "max_runs": 9}))
        # 目标定了就不能改——换目标要先放弃。
        ws.send_text(json.dumps({"type": "set_goal", "description": "换一个"}))
        assert "已经有一个目标在跑" in collect(ws, "error")[-1]["message"]

    body = client.get(f"/sessions/{session.id}").json()["session"]
    assert body["meta"]["goal"]["max_runs"] == 9
    assert body["meta"]["goal"]["description"] == "写到摊牌"
    assert body["goal_run"]["paused"] is False, "预算够了就该解除暂停"


def test_abandoning_is_the_only_way_to_swap_a_goal(client_for) -> None:
    """**同一时间只有一个 goal**（D148）。`SetGoal` 直接换会被拒——goal_manager 的
    对话是围着那个目标建起来的，换一个就得整份重来，而那正是 `abandon` 做的事。

    所以放弃之后，目标与它的运行时状态一并清掉，新的目标才设得进去。
    """
    client = client_for(happy_llm())
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=5)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "set_goal", "description": "换一个"}))
        assert "已经有一个目标在跑" in collect(ws, "error")[-1]["message"]

        ws.send_text(json.dumps({"type": "goal_control", "action": "abandon"}))
        ws.send_text(json.dumps({"type": "set_goal", "description": "换一个"}))
        # 这一次没有报错——拿一条会报错的消息当同步点，确认上面两条都处理完了。
        ws.send_text(json.dumps({"type": "不存在的类型"}))
        assert "格式错误" in collect(ws, "error")[-1]["message"]

    body = client.get(f"/sessions/{session.id}").json()["session"]
    assert body["meta"]["goal"]["description"] == "换一个"
    assert body["goal_run"] is not None, "新目标带着自己那份运行时状态"
    # **旧对话没跟过来。** 不能断言它是空的：新目标一设上就自己开跑了（D165），
    # 那份对话到这一刻多半已经有内容——要钉的是内容属于**新**目标。
    assert "写到摊牌" not in json.dumps(body["goal_run"]["conversation"], ensure_ascii=False)


def test_a_paused_goal_neither_judges_nor_continues(client_for) -> None:
    """**暂停 = goal_manager 完全不出场**（D148）。

    不只是"不开下一轮"：连判定也不做。它每轮都是一次真实的模型调用，暂停期间
    还在那儿判，等于用户按了暂停却照样在烧钱，而且判的是用户自己在写的剧情。
    """
    llm = ScriptedLLM(*one_run(outline="第一轮。", prose="正文一。"), *one_run(outline="第二轮。"))
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=5)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "goal_control", "action": "pause"}))
        ws.send_text(json.dumps({"type": "submit", "text": "我自己来"}))
        collect(ws, "run_finished")
        # 第二轮**得自己提交**——暂停期间没有自动续跑，这本身就是要测的东西之一。
        # 它跑完也说明第一轮那次 run 末的检查已经过去了：队列是顺序的，
        # `_check_goal` 在下一件被取走之前就 await 完了。
        ws.send_text(json.dumps({"type": "submit", "text": "再来一句"}))
        second = collect(ws, "run_finished")[-1]["node"]

    assert second["user_input"]["text"] == "再来一句", "自动续跑没有插队进来"

    assert not [r for r in llm.requests if GOAL_MARK in r["messages"][0]["content"]], (
        "暂停期间不该有任何 goal_manager 调用"
    )
    assert second["user_input"]["source"] == "user", "暂停期间的轮次都是用户自己打的"
    body = client.get(f"/sessions/{session.id}").json()["session"]
    assert body["goal_run"]["paused"] is True
    assert body["goal_run"]["paused_reason"] == "用户暂停"


def test_resuming_hands_over_the_turns_the_user_wrote_meanwhile(client_for) -> None:
    """**暂停期间用户自己跑的那几轮，恢复时要交给 goal_manager**（D148）。

    这是「维持对话」最容易漏掉的一段：那几轮它当时没看见，但剧情实实在在动了。
    不补上去的话，它是照着一个几轮之前的局面在判。

    而且**必须标成"用户自己的输入"**——剧情偏了是它自己推错了、还是用户主动
    把剧情带走了，两种偏离长得一模一样而应对完全相反。这一轮它据此判了 `failed`：
    用户把局面带到了走不回去的地方，这正是 `failed` 该出现的场合。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        *opening("接着说。"),
        *one_run(outline="第三轮。", prose="正文三。"),
        tool("finish", outcome="failed", reason="用户把局面带到了走不回去的地方"),
        text("完了。"),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌", max_runs=9)))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "goal_control", "action": "pause"}))
        for said in ("我自己来", "再来一句"):
            ws.send_text(json.dumps({"type": "submit", "text": said}))
            collect(ws, "run_finished")

        # **恢复本身就是"接着跑"**（D165）——第三轮不用用户再打一句话推它。
        ws.send_text(json.dumps({"type": "goal_control", "action": "resume"}))
        third = collect(ws, "run_finished")[-1]["node"]
        finished = collect(ws, "goal_finished")[-1]

    asked = [r for r in llm.requests if GOAL_MARK in r["messages"][0]["content"]]
    # 三次**请求**，两次登场：开场那次调了 `steer` 再空手回来收口（两次请求），
    # 判定那次调了 `finish` 就地收摊（一次）。
    assert len(asked) == 3

    # **交接发生在恢复的那一刻**（D168）：它先补上暂停期间那两轮，再据此写下一条
    # 输入——而不是先空手推一轮、写完了才第一次看见那两轮。
    handover = "\n".join(m.get("content") or "" for m in asked[0]["messages"])
    assert handover.count("用户自己的输入") == 2, "暂停期间那两轮也算，而且都是用户打的"
    for prose in ("正文一。", "正文二。"):
        assert prose in handover, "补的是正文，不是细纲（D150）"
    assert third["user_input"]["text"] == "接着说。"
    assert third["user_input"]["source"] == "simulated"

    judged = "\n".join(m.get("content") or "" for m in asked[-1]["messages"])
    assert judged.count("我写的输入") == 1, "第三轮是它自己写的，来源要标对"
    assert "正文三。" in judged

    # `failed` 也是终态：和 `done` 一样交回控制权、一样把 goal 清掉。
    assert finished["outcome"] == "failed"
    assert "走不回去" in finished["reason"]
    session_body = client.get(f"/sessions/{session.id}").json()["session"]
    assert session_body["meta"]["goal"] is None
    assert session_body["goal_run"] is None


def test_goal_messages_without_a_goal_say_so(client_for) -> None:
    """没有目标时这两条都无事可做——**要出声**，不能悄悄吞掉。

    尤其 `set_goal_budget`：它是"加预算接着跑"的入口，静默失败的表现是
    "我明明调大了预算，怎么还是不动"。
    """
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "set_goal_budget", "max_runs": 9}))
        assert "没有目标" in collect(ws, "error")[-1]["message"]

        ws.send_text(json.dumps({"type": "goal_control", "action": "pause"}))
        assert "没有目标" in collect(ws, "error")[-1]["message"]


def test_a_run_announces_that_it_started(client_for) -> None:
    """**生命周期消息是一组**：开始 / 结束 / 失败 / 被打断。

    `run_started` 在协议里定义了很久却从没被发出过——后三个都有人发，只有它没有。
    少了它前端就不知道一轮什么时候开始：进度指示不亮，上一轮的过程也不会清掉，
    于是两轮的 trace 会叠在一起显示。
    """
    llm = happy_llm()
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    started = [m for m in messages if m["type"] == "run_started"]
    assert len(started) == 1, "一轮只该宣告一次"
    assert started[0]["run_id"] == messages[-1]["node"]["id"], "宣告的必须是这一轮"
    # 顺序也要对：先说开始，再有过程。
    assert [m["type"] for m in messages].index("run_started") < [m["type"] for m in messages].index(
        "trace"
    )


# -- 流式（D160） --------------------------------------------------------------


def test_prose_arrives_in_pieces_while_it_is_written(client_for) -> None:
    """**正文边写边推**（D160）。

    这是流式最值得的地方：一次调用、纯文本、而且是用户真正在等的东西。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    deltas = [m for m in messages if m["type"] == "delta"]
    prose = [m for m in deltas if m["actor"] == "writer" and m["kind"] == "content"]
    assert len(prose) > 1, "得是好几片，不是一整块"
    assert "".join(m["text"] for m in prose) == "门轴响了。"
    # **分片必须带 run_id**：前端要把它归到正确的那一轮名下。
    assert {m["run_id"] for m in prose} == {messages[-1]["node"]["id"]}


def test_the_final_node_is_still_the_authority(client_for) -> None:
    """**分片是旁路，不是权威。**

    它可能因为断线而丢，所以前端据此显示的东西必须能被最终结果整个替换掉。
    这条钉的是：不管流式推了什么，`run_finished` 带回来的仍然是完整正文。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        finished = collect(ws, "run_finished")[-1]

    assert finished["node"]["prose"] == "门轴响了。"


def test_tool_calls_do_not_stream(client_for) -> None:
    """**工具调用不推分片**（D160）。

    它们的 `arguments` 是逐字拼出来的 JSON，半截 JSON 对界面毫无用处，只会逼
    前端去猜。等完整了随 trace 一次性给——而细纲正是走 `submit_outline` 的参数。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    everything = "".join(m["text"] for m in messages if m["type"] == "delta")
    assert "她推开门。" not in everything, "细纲走的是工具参数，不该出现在分片里"
    assert messages[-1]["node"]["outline"] == "她推开门。"


def test_deltas_say_who_wrote_them(client_for) -> None:
    """**`actor` 不是装饰，是必需的**（D160）。

    `content` 分片**两个 agent 都在发**：director 推演途中会写几句自言自语
    （"我先把这一轮推演清楚"），writer 写的才是正文。不按来源分开攒，界面上
    就会把 director 的开场白当成故事的第一句显示出来——实跑时正是这样：
    攒出 1084 字，而节点正文只有 782 字。
    """
    llm = ScriptedLLM(
        # director 先空手回一句（会被催），再提交细纲——那句话是它的自言自语。
        text("我先想想。"),
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    deltas = [m for m in messages if m["type"] == "delta"]
    by_actor: dict[str, str] = {}
    for m in deltas:
        if m["kind"] == "content":
            by_actor[m["actor"]] = by_actor.get(m["actor"], "") + m["text"]

    assert by_actor["writer"] == "门轴响了。"
    assert by_actor["director"] == "我先想想。"
    # 全部 content 拼起来**不等于**正文——这正是必须按来源分开的理由。
    assert "".join(v for v in by_actor.values()) != messages[-1]["node"]["prose"]


def test_the_agents_after_the_writer_stream_too(client_for) -> None:
    """**它们也是 agent**（D166/D167）。

    director 和 writer 的思考一直在推，跑在正文之后那两个（state_manager 与
    goal_manager）却藏着——凭什么。goal_manager 尤其：目标迟迟不达标时，唯一能
    回答"它到底在等什么"的就是这段。

    goal_manager 的分片挂在**刚写完那一轮**的 `run_id` 上，和它的 trace 事件落在
    同一处：它是在那一轮之后跑的，那就是它归属的地方。
    """
    llm = ScriptedLLM(
        *one_run(outline="第一轮。", prose="正文一。"),
        tool("steer", next_input="再走一步。"),
        text("这一步还差着摊牌。"),
        *one_run(outline="第二轮。", prose="正文二。"),
        state=one_state(),
    )
    client = client_for(llm)
    session = Session(meta=SessionMeta(goal=Goal(description="写到摊牌")))
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "开始"}))
        first = collect(ws, "run_finished")
        node_id = first[-1]["node"]["id"]
        second = collect(ws, "run_finished")

    def said_by(actor: str) -> list[dict]:
        return [
            m
            for m in first + second
            if m["type"] == "delta" and m["actor"] == actor and m["kind"] == "content"
        ]

    goal_deltas = said_by("goal_manager")
    assert "".join(m["text"] for m in goal_deltas) == "这一步还差着摊牌。"
    assert {m["run_id"] for m in goal_deltas} == {node_id}, "归到它判定的那一轮名下"
    # 状态维护也跑在正文之后（D143），同样该看得见。
    assert "".join(m["text"] for m in said_by("state_manager")) == "无变化。无变化。"
    # 它们写的东西**都不是正文**——按 actor 分流就是为了挡住这个（同 `deltas_say_who`）。
    assert first[-1]["node"]["prose"] == "正文一。"


def test_streaming_can_be_switched_off(client_for) -> None:
    """**可开关**。关掉则一轮写完才整段出现，产出完全一样。"""
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm, stream=False)
    session = Session()
    client.post("/sessions", json=session.model_dump(mode="json"))

    with client.websocket_connect(f"/sessions/{session.id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    assert not [m for m in messages if m["type"] == "delta"]
    assert messages[-1]["node"]["prose"] == "门轴响了。", "关掉流式不影响产出"
    # 请求那侧也确实没要流。
    assert all(not r.get("stream") for r in llm.requests)


# -- 开发者模式（D156） --------------------------------------------------------


def test_developer_mode_records_every_agents_real_exchange(client_for) -> None:
    """**开发者模式下每个 agent 的完整输入输出都要留下**（D156）。

    别处的 trace 事件都是投影：`context_assembled` 只有条数和字数，
    `model_response` 只有工具名。投影够回答"它做了什么"，回答不了"它为什么
    这么做"——而调 prompt 时要看的恰恰是后者。

    覆盖面是这条测试的重点：**writer 此前连 `model_response` 都没有**，
    等于完全不可见。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm, developer=True)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    exchanges = [
        m["event"]
        for m in messages
        if m["type"] == "trace" and m["event"]["type"] == "model_request"
    ]
    # `orchestrator` 是归段追问那一次（`postrun`）——它挂在 ORCHESTRATOR 而不是
    # DIRECTOR 名下，因为那是系统维护性的追问、不是创作轮次。**它此前一次模型
    # 调用都没留下痕迹**，现在也在里面了。
    assert {e["actor"] for e in exchanges} == {
        "director",
        "writer",
        "state_manager",
        "orchestrator",
    }

    # 原文要能一路读到底：role、内容、以及模型返回的工具调用。
    writer = next(e for e in exchanges if e["actor"] == "writer")
    assert writer["payload"]["messages"][0]["role"] == "system"
    assert any("她推开门。" in (m.get("content") or "") for m in writer["payload"]["messages"])
    assert writer["payload"]["response"]["content"] == "门轴响了。"

    director = next(e for e in exchanges if e["actor"] == "director")
    assert director["payload"]["response"]["tool_calls"][0]["name"] == "submit_outline"
    assert "submit_outline" in director["payload"]["tools"]


def test_without_developer_mode_the_originals_are_not_recorded(client_for) -> None:
    """**默认不记**。一次往返的 messages 动辄几万字，四个 agent 加起来一轮上十万。

    投影照旧——`trace` 事件本身不受影响，少的只是原文那一类。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        messages = collect(ws, "run_finished")

    traces = [m for m in messages if m["type"] == "trace"]
    assert traces, "投影照旧"
    assert not [m for m in traces if m["event"]["type"] == "model_request"]


def test_a_past_run_can_be_read_back_from_disk(client_for) -> None:
    """**开发者面板离了这条只能看当前连接期间的轮次**（D162）。

    事件是推过来的，刷新一次页面之前那些就没了。而开发者模式本来就强制落盘——
    查问题总是事后，记了不给读回来的路等于没记。
    """
    llm = ScriptedLLM(
        tool("submit_outline", outline="她推开门。"),
        text("门轴响了。"),
        state=one_state(),
    )
    client = client_for(llm, developer=True)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        run_id = collect(ws, "run_finished")[-1]["node"]["id"]

    # 换一个"连接"（新客户端），照样读得到。
    events = client.get(f"/sessions/{session_id}/trace/{run_id}").json()
    exchanges = [e for e in events if e["type"] == "model_request"]
    assert {e["actor"] for e in exchanges} >= {"director", "writer", "state_manager"}
    assert exchanges[0]["payload"]["messages"], "原文要在"

    # 没留过痕的轮次返回空，不是 404——那不是错误。
    assert client.get(f"/sessions/{session_id}/trace/run_nope").json() == []


def test_the_frontend_can_tell_which_mode_the_service_started_in(client_for) -> None:
    """模式在启动服务时就定了，前端据此决定要不要显示开发者面板。

    所以是只读的 `/meta`，没有对应的写接口——运行时切换会让"这一轮记了没有"
    变成一个要翻日志才知道的问题。
    """
    assert client_for(happy_llm()).get("/meta").json()["developer"] is False
    assert client_for(happy_llm(), developer=True).get("/meta").json()["developer"] is True


def test_meta_no_longer_carries_the_control_options(client_for) -> None:
    """**`options` 已经删掉**（D173）。

    档位和当前取值都住在 agent 的 YAML 里，前端要看就去 `/agents/{name}`——
    `/meta` 再给一份就是同一件事的第二个入口，而两个入口迟早对不上。

    留下的三样都是**服务自己的性质**，运行时改不了：开发者模式、模型名、
    流不流式。
    """
    meta = client_for(happy_llm()).get("/meta").json()
    assert "options" not in meta
    assert set(meta) == {"developer", "model", "stream"}


def test_no_goal_means_no_autocontinue(client_for) -> None:
    llm = ScriptedLLM(*one_run(), *one_run(outline="第二轮。"))
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        collect(ws, "run_finished")
        ws.send_text(json.dumps({"type": "submit", "text": "第二条"}))
        second = collect(ws, "run_finished")[-1]["node"]

    assert second["user_input"]["text"] == "第二条"


# -- 错误处理 -----------------------------------------------------------------


def test_malformed_message_errors_without_dropping_connection(client_for) -> None:
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "不存在的类型"}))
        assert collect(ws, "error")[-1]["message"]

        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        assert collect(ws, "run_finished")[-1]["node"]["prose"]


def test_stop_without_a_run_errors(client_for) -> None:
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "stop"}))
        assert collect(ws, "error")[-1]["message"]


def test_unknown_session_websocket_is_rejected(client_for) -> None:
    client = client_for(happy_llm())
    # starlette 关闭连接的方式随版本变化，只断言连不上。
    with (
        pytest.raises(Exception),  # noqa: B017
        client.websocket_connect("/sessions/nope/ws") as ws,
    ):
        ws.receive_text()


def test_a_hand_edit_lands_in_the_trace(client_for) -> None:
    """**用户改过的节点必须在 trace 里留痕**（D50/D65）。

    `USER_EDIT` 这个事件类型一直定义着却从没被发出过——于是一段手改过的正文在
    trace 里和 writer 写的一模一样，回看时会被当成模型的产出去归因，而 trace
    存在的头一个理由就是 eval（D8）。

    记的是**改了哪几样**，不是新旧内容：正文动辄几千字，而"这个节点被人动过"
    才是回看时要的那一位信息。
    """
    client = client_for(happy_llm(), developer=True)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我推开门"}))
        node_id = collect(ws, "run_finished")[-1]["node"]["id"]

    client.patch(
        f"/sessions/{session_id}/nodes/{node_id}",
        json={"prose": "我自己改的这一句。", "state_delta": {"set": {"地点": "改过了"}}},
    )

    edits = [
        e
        for e in client.get(f"/sessions/{session_id}/trace/{node_id}").json()
        if e["type"] == "user_edit"
    ]
    assert len(edits) == 1
    assert edits[0]["actor"] == "user"
    assert edits[0]["payload"]["fields"] == ["prose", "state"], "改了哪几样"
    assert "正文" in edits[0]["label"] and "状态" in edits[0]["label"], "标签得人能读（D25）"
    # 没改动就不留痕——空的 patch 不是一次编辑。
    client.patch(f"/sessions/{session_id}/nodes/{node_id}", json={})
    assert (
        len(
            [
                e
                for e in client.get(f"/sessions/{session_id}/trace/{node_id}").json()
                if e["type"] == "user_edit"
            ]
        )
        == 1
    )


def test_importing_a_file_can_create_a_new_worldbook(client_for) -> None:
    """**拿到别人分享的设定文件时，要的是"变成我的一份新设定"**（D172）。

    第一版只有 `/worldbooks/{id}/import`——往一份已有的里追加或替换，于是必须
    先建个空的再往里导。那不是这个动作本来的形状。
    """
    client = client_for(happy_llm())
    bundle = {
        "format": "agenttavern.worldbook",
        "version": 1,
        "name": "旧城区",
        "entries": [
            {"id": "wi_x", "name": "题材与基调", "kind": "world", "content": "现代，悬疑"},
            {"name": "陈屿", "kind": "character", "voice": "说话绕"},
        ],
    }

    created = client.post("/worldbooks/import", json=bundle).json()
    assert created["name"] == "旧城区"

    got = client.get(f"/worldbooks/{created['id']}").json()
    assert {e["name"] for e in got["entries"]} == {"题材与基调", "陈屿"}
    # id 一律重新分配——文件里的 id 是别人存储里的地址，不是条目的身份。
    assert all(e["id"] != "wi_x" for e in got["entries"])
    assert got["sessions"] == [], "刚建出来，还没有故事开在上面"

    # 坏格式照样当场拒绝，不能尽力而为地导进一份半懂的东西。
    assert (
        client.post("/worldbooks/import", json={"format": "别的", "version": 1}).status_code == 400
    )
    assert len(client.get("/worldbooks").json()) == 1


# -- 一条坏条目不该让整个设定库瘫痪（D179）------------------------------------


def _plant(root, worldbook_id: str, name: str, body: dict) -> None:
    """往盘上直接放一条**模型读不出来**的条目。

    必须绕开模型写：`WorldbookEntry` 根本装不下这些旧值——而那正是要测的东西。
    """
    where = root / "worldbooks" / worldbook_id / "entries"
    where.mkdir(parents=True, exist_ok=True)
    (where / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def test_a_stale_entry_does_not_take_the_whole_list_down(client_for, tmp_path) -> None:
    """**实测踩过的那个**：盘上一条 `kind: "lore"` 的旧条目让 `GET /worldbooks`
    整个 500——设定库列不出来，新建之后的刷新也炸，而用户看到的症状是
    「新建没反应」。**一条八百年前的脏数据让整个功能瘫痪，报错离原因十万八千里。**

    列表这一层只为了显示"几条"，不喂模型，所以它要宽容：好的照数，
    坏的**点出来**（不是跳过——悄悄少一条用户根本不知道自己丢了什么，D62）。
    """
    client = client_for(happy_llm())
    wid = client.post("/worldbooks", json={"name": "旧城区"}).json()["id"]
    client.put(
        f"/worldbooks/{wid}/entries",
        json={"name": "好的", "kind": "world", "content": "旧货店在窄巷尽头。"},
    )
    _plant(tmp_path, wid, "wi_broken", {"id": "wi_broken", "name": "坏的", "kind": "根本不是档位"})

    res = client.get("/worldbooks")
    assert res.status_code == 200, "一条读不出来不该让整张列表挂掉"

    card = next(c for c in res.json() if c["worldbook"]["id"] == wid)
    assert card["entries"] == 1, "好的那条照数"
    assert card["broken"] == ["wi_broken.json"], "坏的那条要点出来，不能悄悄跳过"


def test_the_model_facing_path_still_refuses_to_guess(client_for, tmp_path) -> None:
    """**喂模型那条路径照旧严格抛。**

    宽容只属于列表。`entries()` 静默跳过一条设定，模型就会据此写出矛盾的剧情，
    而没有任何地方会报错——那比 500 坏得多。
    """
    from pydantic import ValidationError

    from agenttavern.storage import Store

    client = client_for(happy_llm())
    wid = client.post("/worldbooks").json()["id"]
    _plant(tmp_path, wid, "wi_broken", {"id": "wi_broken", "name": "坏的", "kind": "lore"})

    with pytest.raises(ValidationError):
        Store(tmp_path).worldbook(wid).entries()


def test_old_entry_kinds_are_migrated_on_startup(tmp_path) -> None:
    """`kind` 换过两次名，旧值要搬过来（D159/D171/D175）。

    - `lore` → `world`
    - `player` → `character` + `is_player`：**用户扮演的角色不是第四种 kind**，
      它是一条角色条目加一个标记。那次改形状时漏了迁移，于是盘上留着一批
      读不出来的条目——直到某天整个设定库 500。

    正文、名字、语言习惯**一个字都不能丢**：这是用户自己写的世界。
    """
    from agenttavern.models import EntryKind
    from agenttavern.storage import Store

    store = Store(tmp_path)
    book = store.create_worldbook(Worldbook(name="旧城区"))
    _plant(
        tmp_path,
        book.worldbook_id,
        "a",
        {"id": "a", "name": "女多男少", "kind": "lore", "content": "男女比例失衡。"},
    )
    _plant(
        tmp_path,
        book.worldbook_id,
        "b",
        {"id": "b", "name": "程飞", "kind": "player", "content": "唯一的男教师。", "voice": "话少"},
    )

    assert store.migrate_kinds() == 2
    assert store.migrate_kinds() == 0, "跑第二遍是空操作"

    entries = {e.id: e for e in book.entries()}
    assert entries["a"].kind is EntryKind.WORLD
    assert entries["a"].content == "男女比例失衡。", "正文一个字都不能丢"

    assert entries["b"].kind is EntryKind.CHARACTER
    assert entries["b"].is_player is True, "「用户扮演谁」搬到标记上，不是丢掉"
    assert entries["b"].voice == "话少"
