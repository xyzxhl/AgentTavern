"""SessionRunner 的生命周期与分支参数传递。

这两条都是**只有真跑起来才会暴露**的问题：前者表现为连接挂死（不报错，
只是永远不返回），后者表现为分支挂错父节点（数据是对的，位置是错的）。
"""

import asyncio
from pathlib import Path

import pytest
from conftest import ScriptedLLM, text, tool

from agenttavern.models import Actor, Session
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


async def test_regenerate_opens_a_sibling_of_the_current_leaf(tmp_path: Path) -> None:
    """重生成 = 在**当前叶的父**下新建兄弟（D46/D147）。

    父节点只有一个来源：出队那一刻的活跃叶。以前它随输入一起入队，因为可以
    重生成任意历史节点，而活跃叶是共享状态（有 run 在飞时会被覆盖）。**分支只能
    在当前节点上开之后，那个来源自然只剩一个，也就没有对不上的余地。**
    """
    llm = ScriptedLLM(*one_run(), *one_run(prose="第二轮。"), *one_run(prose="重生成。"))
    runner, repo, _sent = make_runner(tmp_path, llm)
    runner.start()

    try:
        await runner.submit("第一条")
        await _until(lambda: bool(repo.all_nodes()))
        first = next(iter(repo.all_nodes().values()))

        await runner.submit("第一条", regenerate=True)
        await _until(lambda: len(repo.all_nodes()) > 1)

        nodes = list(repo.all_nodes().values())
        assert len(nodes) == 2
        # 两个节点是**兄弟**（同一个父），不是父子
        assert {n.parent_id for n in nodes} == {first.parent_id}
    finally:
        await asyncio.wait_for(runner.aclose(), timeout=2.0)


async def test_ordinary_submit_still_chains_onto_the_active_leaf(tmp_path: Path) -> None:
    """对照：普通输入接在活跃叶后面——那是**延长**，不是分支。"""
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


async def test_goal_spending_is_counted_and_lands_on_disk(tmp_path: Path) -> None:
    """goal_manager 是一次真实的模型调用，**必须和 run 一样可见、一样计入预算**。

    它以前用默认参数的 Tracer——既不推事件也不落盘，于是"goal 为什么迟迟不达标"
    无从查起；而且**计数在 `SessionRunner` 的实例字段上，进程重启就归零**，
    D28 那个用来兜住自主运行成本的上限实际上从来没生效过（D148 修掉）。

    而**算进预算的是这一轮的全部开销**，不只是 goal_manager 自己那一两次判定调用：
    钱主要花在它开出来的 run 上（细纲 + 正文 + 状态）。只算判定的话，一个名义上
    "一百万 token" 的预算兜住的其实是零头，`max_runs` 之外等于没有第二道闸。
    """
    from agenttavern.models import Goal, SessionMeta

    store = Store(tmp_path)
    session = Session(meta=SessionMeta(goal=Goal(description="让她说出真相", max_runs=1)))
    repo = store.create_session(session)
    sent: list[object] = []

    async def broadcast(message: object) -> None:
        sent.append(message)

    llm = ScriptedLLM(
        *one_run(),
        tool("plan", subgoals=["她开口"]),
        text("先推到这里。"),
    )
    runner = SessionRunner(repo, llm.gateway(), broadcast)
    runner.start()

    try:
        await runner.submit("我推开门")
        # 等**最后一条**消息。出站走的是单条队列、顺序送出，所以它到了就说明前面
        # 那些也到了；只等 `goal_run` 落盘的话，`UsageUpdate` 可能还在队列里，
        # 下面那两笔就会一笔算成 0（原来那条 `>` 断言正是这样悄悄成立的）。
        assert await _until(lambda: any(type(m).__name__ == "GoalPaused" for m in sent))

        run = repo.load_session().goal_run
        assert run is not None
        # **落在盘上**，不在内存里——这正是那个 bug 的形状。
        assert run.spent_runs == 1
        # `UsageUpdate` 推的是这一轮 run 自己的开销；goal_manager 那次的落在
        # 它自己的 trace 事件上。两笔都得在里面。
        run_only = sum(
            m.tokens_in + m.tokens_out  # type: ignore[attr-defined]
            for m in sent
            if type(m).__name__ == "UsageUpdate"
        )
        judged = sum(
            (e.tokens_in or 0) + (e.tokens_out or 0)  # type: ignore[attr-defined]
            for m in sent
            if type(m).__name__ == "Trace"
            for e in [m.event]  # type: ignore[attr-defined]
            if e.actor is Actor.GOAL_MANAGER
        )
        assert run_only and judged, "两笔开销都得真的发生过，否则下面那条断言是空的"
        assert run.spent_tokens == run_only + judged, "预算算的是这一轮的全部开销"
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

    **在 writer 那次调用里推信箱**，因为那一刻同时满足两个条件：tool loop
    已经结束（信箱关了），而这一轮还没落地（`drain()` 还没发生）。

    早先这里是在外面轮询等 `not inbox.accepting` 再推。那是**竞态**：脚本化
    的模型没有真实延迟，两次轮询之间整轮就跑完并 drain 了，推进去的是一个
    已经排空的信箱。Windows 上一直是绿的（计时器粒度粗、文件写入慢，窗口
    恰好够宽），**Linux 上稳定失败**——CI 第一次跑就抓到了。
    """
    llm = ScriptedLLM(*one_run(), *one_run("阳台", "她走到阳台。", "风起来了。"))
    runner, repo, _ = make_runner(tmp_path, llm)

    def push_while_the_turn_is_still_running() -> None:
        # 只推第一轮那一次：第二轮也会走到 writer，再推一次就无限开新轮。
        llm.on_writer_request = None
        inbox = runner._inbox
        assert inbox is not None, "run 在跑，信箱必须存在"
        assert not inbox.accepting, "tool loop 已经结束，信箱此时必须是关的"
        inbox.push("等等，让她去阳台")

    llm.on_writer_request = push_while_the_turn_is_still_running
    runner.start()

    await runner.submit("我推开门")

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
