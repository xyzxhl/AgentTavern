"""服务端：REST、WebSocket、队列与挂起语义。"""

import json
from pathlib import Path

import pytest
from conftest import ScriptedLLM, text, tool, tools
from fastapi.testclient import TestClient

from agenttavern.config import Settings
from agenttavern.models import Goal, Session, SessionMeta
from agenttavern.server import create_app


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
    def build(llm: ScriptedLLM):
        app = create_app(Settings(data_dir=tmp_path, llm_model="m"), gateway=llm.gateway())
        return TestClient(app)

    return build


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

    assert client.get("/sessions").json() == [session["id"]]

    patched = client.patch(
        f"/sessions/{session['id']}",
        json={"meta": {"title": "雨夜", "prose_style": "短句，冷调"}},
    ).json()
    assert patched["meta"]["title"] == "雨夜"

    detail = client.get(f"/sessions/{session['id']}").json()
    assert detail["path"] == []
    assert detail["state"] == {}


def test_missing_session_is_404(client_for) -> None:
    client = client_for(happy_llm())
    assert client.get("/sessions/nope").status_code == 404


def test_worldinfo_roundtrip(client_for) -> None:
    client = client_for(happy_llm())
    session_id = client.post("/sessions").json()["id"]

    client.put(
        f"/sessions/{session_id}/worldinfo",
        json={"id": "wi_1", "name": "教团", "content": "很长的设定"},
    )
    entries = client.get(f"/sessions/{session_id}/worldinfo").json()
    assert entries[0]["name"] == "教团"


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
        tool("ask_question", question="她怎么反应？", options=["否认", "沉默"]),
        tool("submit_outline", outline="她沉默了。"),
        text("她没有说话。"),
    )
    client = client_for(llm)
    session_id = client.post("/sessions").json()["id"]

    with client.websocket_connect(f"/sessions/{session_id}/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "submit", "text": "我问她"}))

        question = collect(ws, "question")[-1]
        assert question["options"] == ["否认", "沉默"]

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
        json={"prose": "门开了。", "state_set": {"地点": "阳台"}},
    ).json()

    assert patched["prose"] == "门开了。"
    assert client.get(f"/sessions/{session_id}").json()["state"]["地点"] == "阳台"


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
