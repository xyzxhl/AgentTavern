"""SessionRunner 的生命周期与分支参数传递。

这两条都是**只有真跑起来才会暴露**的问题：前者表现为连接挂死（不报错，
只是永远不返回），后者表现为分支挂错父节点（数据是对的，位置是错的）。
"""

import asyncio
from pathlib import Path

import pytest
from conftest import ScriptedLLM, text, tool

from agenttavern.models import Session
from agenttavern.server.runner import SessionRunner
from agenttavern.storage import Store


def one_run(place: str = "客厅", outline: str = "她推开门。", prose: str = "门轴响了。"):
    return [
        tool("state_write", set={"地点": place}),
        tool("submit_outline", outline=outline),
        text(prose),
    ]


async def _until(predicate, timeout: float = 3.0) -> bool:
    """等到条件成立，最多等 ``timeout`` 秒。

    **不能用 `await asyncio.sleep(0)` 空转。** 那样只轮转就绪队列，任何依赖
    计时器或线程的机制都推进不了——现象是"永远等不到"，很容易被误读成死锁。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.001)
    return predicate()


def make_runner(tmp_path: Path, llm: ScriptedLLM):
    store = Store(tmp_path)
    session = Session()
    repo = store.create_session(session)
    sent: list[object] = []

    async def broadcast(message: object) -> None:
        sent.append(message)

    return SessionRunner(repo, llm.gateway(), broadcast), repo, sent


async def test_aclose_returns_while_a_run_is_in_flight(tmp_path: Path) -> None:
    """**连接断开时 `aclose()` 必须能返回。**

    `_loop` 里的 ``except CancelledError: pass`` 原本分不清"这个 run 被 stop 了"
    和"整个 runner 在关闭"。关闭时异常被吞掉，循环转去 ``await queue.get()``，
    而取消**只投递一次**——``aclose`` 里的 ``await task`` 就再也等不到了，
    WebSocket handler 随之挂死。

    没有超时的话，这个测试本身也会挂住而不是失败。
    """
    llm = ScriptedLLM(*one_run())
    runner, _repo, _sent = make_runner(tmp_path, llm)
    runner.start()

    await runner.submit("我推开门")
    await asyncio.sleep(0)  # 让 loop 把这一条取出来开跑

    await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_aclose_returns_when_idle(tmp_path: Path) -> None:
    """空闲时（loop 阻塞在 queue.get 上）关闭同样要能返回。"""
    llm = ScriptedLLM(*one_run())
    runner, _repo, _sent = make_runner(tmp_path, llm)
    runner.start()
    await asyncio.sleep(0)

    await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_stop_cancels_only_the_run_not_the_loop(tmp_path: Path) -> None:
    """对照：``stop()`` 之后 loop 必须还活着，能继续消费下一条输入。

    这正是那个 ``pass`` 存在的理由——修复不能把它整个删掉。
    """
    llm = ScriptedLLM(*one_run(), *one_run(prose="第二轮。"))
    runner, repo, _sent = make_runner(tmp_path, llm)
    runner.start()

    try:
        await runner.submit("第一条")
        await _until(lambda: runner._current is not None)
        assert runner.stop(), "应当有一个正在进行的 run 可以中断"
        await asyncio.sleep(0.01)

        await runner.submit("第二条")
        await _until(lambda: bool(repo.all_nodes()))
        assert repo.all_nodes(), "stop 之后 loop 应当还能跑下一条输入"
    finally:
        # **断言失败也必须关掉 runner。** 否则挂起的任务会把失败变成拆解挂死，
        # 那时看到的现象是"测试卡住"，不是"测试失败"——最难查的一种。
        await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_regenerate_carries_its_parent_through_the_queue(tmp_path: Path) -> None:
    """**重生成的父节点必须随输入入队**，不能靠改 `session.active_leaf_id` 传递。

    活跃叶是共享状态：若提交重生成时正有一个 run 在飞，那个 run 结束时会把
    活跃叶覆盖成自己，重生成就挂到了错误的父节点上——**产出的文本是对的，
    位置是错的**，这类错误在树里看不出来，要等切分支时才发现状态对不上。
    """
    llm = ScriptedLLM(*one_run(), *one_run(prose="第二轮。"), *one_run(prose="重生成。"))
    runner, repo, _sent = make_runner(tmp_path, llm)
    runner.start()

    try:
        await runner.submit("第一条")
        await _until(lambda: bool(repo.all_nodes()))
        first = next(iter(repo.all_nodes().values()))

        # 模拟 app.py 的重生成路径：显式带上父节点（这里是根）
        await runner.submit("第一条", parent_id=first.parent_id, explicit_parent=True)
        await _until(lambda: len(repo.all_nodes()) > 1)

        nodes = list(repo.all_nodes().values())
        assert len(nodes) == 2
        # 两个节点是**兄弟**（同一个父），不是父子
        assert {n.parent_id for n in nodes} == {first.parent_id}
    finally:
        await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_ordinary_submit_still_chains_onto_the_active_leaf(tmp_path: Path) -> None:
    """对照：不带 explicit_parent 的普通输入仍然接在活跃叶后面。

    ``parent_id=None`` 本身是合法的（根节点），所以不能用 None 来表示
    "没指定"——这就是 `explicit_parent` 这个旗标存在的理由。
    """
    llm = ScriptedLLM(*one_run(), *one_run(prose="第二轮。"))
    runner, repo, _sent = make_runner(tmp_path, llm)
    runner.start()

    try:
        await runner.submit("第一条")
        await _until(lambda: bool(repo.all_nodes()))
        first = next(iter(repo.all_nodes().values()))

        await runner.submit("第二条")
        await _until(lambda: len(repo.all_nodes()) > 1)

        nodes = repo.all_nodes()
        assert len(nodes) == 2
        second = next(n for n in nodes.values() if n.id != first.id)
        assert second.parent_id == first.id
    finally:
        await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_goal_judging_is_visible_and_counted(tmp_path: Path) -> None:
    """goal 判定是一次真实的模型调用，**必须和 run 一样可见、一样计入预算**。

    此前它用的是默认参数的 Tracer——既不推事件也不落盘，于是"goal 为什么迟迟
    不达标"无从查起；而且它的 token 从未计入 `_goal_tokens`，**D28 那个用来兜住
    自主运行成本的预算漏掉了每一轮都会发生的一次调用**。
    """
    from agenttavern.models import Goal, SessionMeta

    store = Store(tmp_path)
    session = Session(meta=SessionMeta(goal=Goal(description="让她说出真相")))
    repo = store.create_session(session)
    sent: list[object] = []

    async def broadcast(message: object) -> None:
        sent.append(message)

    llm = ScriptedLLM(
        *one_run(),
        text("未达成：她还没开口。"),
        *one_run(prose="第二轮。"),
        text("未达成：仍未开口。"),
    )
    runner = SessionRunner(repo, llm.gateway(), broadcast)
    runner.start()

    try:
        await runner.submit("我推开门")
        await _until(lambda: runner._goal_tokens > 0)

        # 判定用掉的 token 必须算进预算——否则自主运行会超出 D28 设的上限
        run_only = sum(
            m.tokens_in + m.tokens_out  # type: ignore[attr-defined]
            for m in sent
            if type(m).__name__ == "UsageUpdate"
        )
        assert runner._goal_tokens > run_only, "goal 判定的 token 没有计入预算"
    finally:
        await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_the_loop_is_cancellable_by_anyone_not_only_by_aclose(tmp_path: Path) -> None:
    """循环**不能吞掉自己的取消**。

    第一版修复靠一个 `_closing` 旗标区分"这个 run 被 stop 了"和"runner 在关闭"，
    副作用是**除了 `aclose()` 谁也取消不掉这个循环**：测试断言失败、连接异常、
    事件循环拆解——任何外部取消都会被吞掉，任务变成不死的，然后卡住拆解。

    现在循环用 ``asyncio.wait`` 等子任务：子任务的取消不会传播给它，
    而它自己的取消照常抛出。两个要求互不牵扯。
    """
    llm = ScriptedLLM(*one_run())
    runner, _repo, _sent = make_runner(tmp_path, llm)
    runner.start()
    await runner.submit("我推开门")
    await asyncio.sleep(0)

    assert runner._loop_task is not None
    runner._loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(runner._loop_task, timeout=2.0)

    await asyncio.wait_for(runner.aclose(), timeout=2.0)


# -- 插话的两个边缘（D119） -----------------------------------------------------


async def test_guidance_left_in_the_inbox_becomes_the_next_turn(tmp_path: Path) -> None:
    """这一轮已经没有操作边界了，落进信箱的话不能被吞掉。

    两种情形走同一条路：引导正好落在 `submit_outline` 上（它是终止点，直接返回，
    **走不到边界**），或者落在 writer 阶段（那是一次不可分的生成，本来就没有边界）。
    用户的输入在任何情况下都不该消失，所以改开新的一轮。

    这里卡在 writer 那次调用上，因为那一刻**可以确定**信箱已经关了——
    卡在 `submit_outline` 上要赌调度顺序，会时红时绿。
    """
    llm = ScriptedLLM(*one_run(), *one_run("阳台", "她走到阳台。", "风起来了。"))
    runner, repo, _ = make_runner(tmp_path, llm)
    runner.start()

    await runner.submit("我推开门")

    # 先等信箱开，再等它关——`accepting` 起始就是 False，只等"关"会立刻命中，
    # 那样测的是 run 还没开始的时刻，什么也说明不了。
    assert await _until(lambda: runner._inbox is not None and runner._inbox.accepting)
    inbox = runner._inbox
    assert await _until(lambda: not inbox.accepting), "tool loop 结束后信箱必须关掉"
    inbox.push("等等，让她去阳台")

    assert await _until(lambda: len(repo.all_nodes()) == 2), "落进信箱的话应该变成新的一轮"
    inputs = [n.user_input.text for n in repo.all_nodes().values() if n.user_input]
    assert "等等，让她去阳台" in inputs

    await runner.aclose()


async def test_interjecting_when_nothing_is_running_is_refused(tmp_path: Path) -> None:
    """没有 run 在跑时 `interject` 返回 False，由 WS 层回落到 `submit`。

    **不能静默吞掉**——静默吞掉的输入是查不出来的。
    """
    runner, _, _ = make_runner(tmp_path, ScriptedLLM(*one_run()))
    runner.start()

    assert runner.interject("插一句", interrupt=False) is False
    assert runner.stop() is False

    await runner.aclose()
