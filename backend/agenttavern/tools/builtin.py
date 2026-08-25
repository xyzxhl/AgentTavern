"""内置工具。清单与理由见 docs/design/Tools.md。

**这里装的是全部工具，不是某一个 agent 的工具面**——谁拿到哪几个由
`registry.py` 决定。`state_write` 只给 state_manager，`submit_outline` 只给
director（S7）。

**明确不提供**（同样重要）：任何让 director 读正文的工具、调用 writer 的工具、
生成子 agent、修改元指令 / 控制维度、写文件 / 执行代码。头两条是 S5 的基础——
director 能覆盖五倍历史跨度，正是因为它自己从不读正文；工具返回值会回流到调用者
上下文，正文一旦这样回流，隔离当场就没了。（正文经 state_manager 折成键值再进
state 是另一回事：那是一次有损压缩，且跨了 run 边界。）
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..config import LIMITS
from ..models import Node, StateDelta
from ..models.state import label_path as _label
from ..models.state import paths_of
from ..prompting import tool_description
from .base import Tool, ToolOutcome, ToolResult, error
from .context import RunContext

MAX_READ_CHARS = LIMITS.context.tool_read_chars
"""单次工具返回的体量上限——防止一次调用撑爆上下文。"""


# --------------------------------------------------------------------------
# 产出与终止
# --------------------------------------------------------------------------


class SubmitOutlineParams(BaseModel):
    # 「细纲该写什么、不该写什么」整节在 `director.yaml` 的 `output_contract` 里，
    # 这里不重复（S27）。
    outline: str = Field(description="本轮细纲，一整段文字。")


async def _submit_outline(ctx: RunContext, params: SubmitOutlineParams) -> ToolResult:
    # **E0 契约已作废**（S7）：它原先要求提交细纲前必须写过一次 state_write。
    # director 不再维护状态，这里也就没有前置条件了。
    ctx.outline = params.outline
    return ToolResult(
        content="细纲已提交。",
        outcome=ToolOutcome.TERMINATE,
        label="提交了细纲",
        payload={"chars": len(params.outline)},
    )


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


class StateQueryParams(BaseModel):
    paths: list[list[str]] = Field(
        description='要展开的位置，每个是一条路径，一段一个键（如 `["林", "知情"]`）。'
    )


async def _state_query(ctx: RunContext, params: StateQueryParams) -> ToolResult:
    state = ctx.current_state()
    found: dict[str, Any] = {}
    missing: list[str] = []
    for path in params.paths:
        value = state.at(path)
        if value is None:
            missing.append(_label(path))
        else:
            found[_label(path)] = value

    if not found:
        # 未知路径是模型可恢复的错误——把现有结构回给它，顺便压键名漂移。
        available = "、".join(_label(p) for p in state.paths()) or "（无）"
        return error(
            f"没有这些位置：{'、'.join(missing)}。当前有：{available}",
            label=f"查 {'、'.join(missing)} 未命中",
        )

    content = json.dumps(found, ensure_ascii=False)
    if missing:
        content += f"\n（不存在的位置：{'、'.join(missing)}）"
    return ToolResult(
        content=content,
        label=f"查了 {'、'.join(found)}",
        payload={"found": list(found), "missing": missing},
    )


class StateWriteParams(BaseModel):
    set: dict[str, Any] = Field(
        default_factory=dict,
        description="覆盖写，**深合并**：只动你提到的那几个键，同层别的原样留着。"
        '从属关系用嵌套表达（`{"林": {"性格": "谨慎"}}`）。值为 null 表示删掉该键。',
    )
    append: dict[str, Any] = Field(
        default_factory=dict,
        description="往清单里追加：结构同 set，叶子是要加的条目。自动去重。",
    )
    remove: dict[str, Any] = Field(
        default_factory=dict,
        description="从清单里拿掉：结构同 set，叶子是要去掉的条目。不存在的忽略。"
        "**改清单里的某一项就 remove 旧的再 append 新的**，别用 set 整体重写。",
    )


async def _state_write(ctx: RunContext, params: StateWriteParams) -> ToolResult:
    delta = StateDelta(set=params.set, append=params.append, remove=params.remove)
    ctx.staged_delta = ctx.staged_delta.merge(delta)
    ctx.state_write_count += 1

    if delta.is_empty():
        return ToolResult(content="已记录：本轮无状态变化。", label="确认本轮无状态变化")

    # 标签给人看，所以列的是**叶子路径**而不是顶层键——顶层往往只是个角色名，
    # 十次更新九次都写着同一个字。
    touched = [_label(p) for p in paths_of(delta.set)]
    touched += [_label(p) for p in paths_of(delta.append)]
    touched += [_label(p) for p in paths_of(delta.remove)]
    return ToolResult(
        content="状态增量已记录。",
        label=f"更新了 {'、'.join(touched)}",
        payload=delta.model_dump(exclude_defaults=True),
    )


# --------------------------------------------------------------------------
# history —— 全量索引 + 按需展开（S53）
# --------------------------------------------------------------------------


class HistorySearchParams(BaseModel):
    query: str = Field(
        default="",
        description="要找的字眼，会在**各轮细纲全文**里比对（不是只比总结）。留空则只列索引。",
    )
    limit: int = Field(default=20, ge=1, le=100)


async def _history_search(ctx: RunContext, params: HistorySearchParams) -> ToolResult:
    """**命中只做标注，不做筛选**（P13）。

    此前 ``query`` 是个子串过滤器：查不中就返回空列表，而模型会把"空"读成
    "没这回事"——这正是 lorebook 关键词触发不合格的那个理由，我们自己却犯了。
    现在索引永远覆盖全部轮次，命中的那些多一个标记。
    """
    if ctx.parent_id is None:
        return ToolResult(content="[]", label="历史为空")

    nodes = ctx.repo.path_to(ctx.parent_id)
    truncated = max(0, len(nodes) - params.limit)
    shown = nodes[-params.limit :]

    index: list[dict[str, Any]] = []
    hits = 0
    for node in shown:
        entry: dict[str, Any] = {
            "run_id": node.id,
            "summary": node.summary_or_head(),
            "chars": len(node.outline),
        }
        if params.query and params.query in node.outline:
            entry["命中"] = True
            hits += 1
        index.append(entry)

    body: dict[str, Any] = {"轮次": index}
    if truncated:
        body["更早还有"] = f"{truncated} 轮未列出，调大 limit 可以看到"
    if params.query:
        body["说明"] = (
            f"「{params.query}」在 {hits} 轮里逐字出现。"
            "**没命中不等于没发生过**——当时可能换了个说法。"
            "该找的东西没找到时，按总结挑几轮 history_read 看全文。"
        )

    return ToolResult(
        content=json.dumps(body, ensure_ascii=False),
        label=f"检索历史：{params.query or '索引'}（{len(index)} 轮，命中 {hits}）",
        payload={"query": params.query, "hits": hits, "listed": len(index)},
    )


class HistoryReadParams(BaseModel):
    run_ids: list[str] = Field(
        description="要展开的 ref，来自上下文的历史索引。"
        "索引里一条 ref 可能代表**一整段**（好几轮），展开时会把整段都给出来。"
    )


async def _history_read(ctx: RunContext, params: HistoryReadParams) -> ToolResult:
    """展开索引里的一条 ref。

    **索引按段给 ref，展开也必须按段给回全文**（S57）。摘要在上下文里
    顶替的是**整段**——那几轮的细纲、当时查了什么、写了什么，全被它替掉了。
    只还回末节点那一轮，等于告诉模型"这段就这么点内容"，比不展开更糟：
    它会以为自己已经看全了。
    """
    known = ctx.repo.all_nodes()
    missing = [rid for rid in params.run_ids if rid not in known]
    if missing:
        return error(f"这些 ref 不存在：{', '.join(missing)}", label="展开历史失败")

    out: list[dict[str, Any]] = []
    budget = MAX_READ_CHARS
    served: set[str] = set()
    for rid in params.run_ids:
        if budget <= 0:
            break
        served.add(rid)
        for node in _segment_of(ctx, known[rid]):
            if budget <= 0:
                break
            outline = node.outline[:budget]
            budget -= len(outline)
            entry: dict[str, Any] = {"run_id": node.id, "segment": rid, "outline": outline}
            if len(outline) < len(node.outline):
                entry["截断"] = f"这一轮只给了前 {len(outline)} 字，全文共 {len(node.outline)} 字"
            out.append(entry)

    body: dict[str, Any] = {"轮次": out}
    # **预算耗尽必须说出来。** 以前这里直接 break，少返回的那几条无声消失——
    # 模型拿到一份看起来完整的结果，不知道自己丢了什么。那正是 P13 判
    # lorebook 关键词触发不合格的同一条理由，只不过换成了体量而不是相关性。
    unserved = [rid for rid in params.run_ids if rid not in served]
    if unserved or budget <= 0:
        body["没给全"] = (
            f"单次上限 {MAX_READ_CHARS} 字已用尽。"
            + (f"这几条一个字都没返回：{'、'.join(unserved)}。" if unserved else "")
            + "**分几次读**，一次少给几个 ref。"
        )

    return ToolResult(
        content=json.dumps(body, ensure_ascii=False),
        label=f"展开了 {len(out)} 轮细纲" + (f"，{len(unserved)} 条没给" if unserved else ""),
        payload={"run_ids": [o["run_id"] for o in out], "unserved": unserved},
    )


def _segment_of(ctx: RunContext, end: Node) -> list[Node]:
    """由段落末节点上溯，取回整段。

    按「末节点 + 跨度」寻址：父指针唯一，所以上溯 ``span - 1`` 步得到的链是确定的，
    **任何经过末节点的路径都得到同一段**，分支天然安全（S42）。
    """
    nodes = ctx.repo.all_nodes()
    chain = [end]
    current = end
    for _ in range(max(1, end.segment_span) - 1):
        if current.parent_id is None or current.parent_id not in nodes:
            break
        current = nodes[current.parent_id]
        chain.append(current)
    chain.reverse()
    return chain


# --------------------------------------------------------------------------
# worldinfo —— 与 history 同一模式
# --------------------------------------------------------------------------


class WorldInfoListParams(BaseModel):
    pass


async def _worldinfo_list(ctx: RunContext, _: WorldInfoListParams) -> ToolResult:
    entries = ctx.repo.worldbook()
    index = [
        {"ref": e.id, "name": e.name, "summary": e.summary_or_head(), "chars": len(e.content)}
        for e in entries
    ]
    return ToolResult(
        content=json.dumps(index, ensure_ascii=False),
        label=f"列出设定索引（{len(index)} 条）",
        payload={"count": len(index)},
    )


class WorldInfoReadParams(BaseModel):
    refs: list[str] = Field(description="要展开的条目 ref，来自 worldinfo_list。")
    start: int = Field(default=0, ge=0, description="起始字符位置，用于分页。")
    max_chars: int = Field(default=MAX_READ_CHARS, ge=1, le=MAX_READ_CHARS)


async def _worldinfo_read(ctx: RunContext, params: WorldInfoReadParams) -> ToolResult:
    out: list[dict[str, Any]] = []
    budget = params.max_chars
    served: set[str] = set()
    for ref in params.refs:
        if budget <= 0:
            break
        entry = ctx.repo.book().load_entry(ref)
        if entry is None:
            return error(f"没有这个设定条目：{ref}", label="展开设定失败")
        served.add(ref)
        chunk = entry.content[params.start : params.start + budget]
        budget -= len(chunk)
        read_to = params.start + len(chunk)
        item: dict[str, Any] = {"ref": ref, "name": entry.name, "content": chunk}
        if read_to < len(entry.content):
            # 分页是无损的，但**得说清怎么接着读**——只给一个 `truncated: true`
            # 等于让模型自己去猜下一个 start 是多少。
            item["还没读完"] = (
                f"已读到第 {read_to} 字，全文共 {len(entry.content)} 字。"
                f"接着读就再调一次，start={read_to}。"
            )
        out.append(item)

    body: dict[str, Any] = {"条目": out}
    unserved = [ref for ref in params.refs if ref not in served]
    if unserved:
        body["没给全"] = (
            f"单次上限 {params.max_chars} 字已用尽，这几条一个字都没返回："
            f"{'、'.join(unserved)}。**分几次读**。"
        )

    return ToolResult(
        content=json.dumps(body, ensure_ascii=False),
        label=f"展开了 {len(out)} 条设定" + (f"，{len(unserved)} 条没给" if unserved else ""),
        payload={"refs": [o["ref"] for o in out], "unserved": unserved},
    )


# --------------------------------------------------------------------------
# summarize —— 懒惰生成 + 系统缓存（S25）
# --------------------------------------------------------------------------


class SummarizeParams(BaseModel):
    source: str = Field(
        description="要压缩的对象：``run:<run_id>``（某轮细纲）或 ``wi:<ref>``（某条设定）。"
    )


async def _summarize(ctx: RunContext, params: SummarizeParams) -> ToolResult:
    kind, _, ident = params.source.partition(":")

    if kind == "run":
        node = ctx.repo.all_nodes().get(ident)
        if node is None:
            return error(f"没有这个 run：{ident}", label="压缩失败")
        if node.outline_summary:
            return ToolResult(content=node.outline_summary, label="复用了已有摘要")
        summary = await _llm_summarize(ctx, node.outline)
        node.outline_summary = summary
        ctx.repo.save_node(node)

    elif kind == "wi":
        entry = ctx.repo.book().load_entry(ident)
        if entry is None:
            return error(f"没有这个设定条目：{ident}", label="压缩失败")
        if entry.summary:
            return ToolResult(content=entry.summary, label="复用了已有摘要")
        summary = await _llm_summarize(ctx, entry.content)
        entry.summary = summary
        ctx.repo.book().save_entry(entry)

    else:
        return error(f"source 必须是 run:<id> 或 wi:<ref>，收到：{params.source}", label="压缩失败")

    # 写回后即成为索引里的 summary 字段，之后不必重算。
    return ToolResult(
        content=summary,
        label=f"压缩了 {params.source}",
        payload={"source": params.source, "chars": len(summary)},
    )


async def _llm_summarize(ctx: RunContext, content: str) -> str:
    # **函数内 import 是有意的**：模块级会成环（tools → agents → director → tools）。
    # 环一直在，只是恰好没人单独 import 过 `agenttavern.tools`——测试全跑时
    # 别的模块先把 agents 装好了，环就看不见。
    from ..agents.summarizer import summarize_text

    return await summarize_text(ctx.gateway, content)


# --------------------------------------------------------------------------
# ask_question
# --------------------------------------------------------------------------


class AskQuestionParams(BaseModel):
    question: str = Field(description="题干：说清这一处岔在哪里。")
    options: list[str] = Field(
        description="恰好三个选项，每个是一条**不同的剧情发展方向**。",
        min_length=3,
        max_length=3,
    )


async def _ask_question(ctx: RunContext, params: AskQuestionParams) -> ToolResult:
    if ctx.ask_user is None:
        return error("当前没有可交互的用户。", label="无法提问")

    # 不设超时（S24）：等待期间逻辑上什么都没发生，上下文不会过期。
    answer = await ctx.ask_user(params.question, params.options)

    # **用户可以不选，直接自己写**（S23）。所以回执要分得清是哪一种：
    # 三选一是在模型给的岔路里挑，自由作答是**模型没想到的第四条路**，
    # 两者对后续推演的分量完全不同。合成一句「用户选择：…」会把后者说成前者，
    # 于是模型照着"这是我列过的选项"去理解一句它其实没列过的话。
    free_form = answer not in params.options
    return ToolResult(
        content=(
            f"用户没有选给出的选项，自己回答：{answer}" if free_form else f"用户选择：{answer}"
        ),
        label=f"问了用户：{params.question}",
        payload={
            "question": params.question,
            "options": params.options,
            "answer": answer,
            "free_form": free_form,
        },
    )


# --------------------------------------------------------------------------


def _tool(agent: str, name: str, params: type[BaseModel], handler: Any) -> Tool[Any]:
    """**描述从那个 agent 的 YAML 里取**（S84）。

    S10 当时判「工具描述不搬进 YAML」，理由是「一个工具怎么描述自己，跟它的参数、
    错误信息、注册条件是同一件事，拆开写两处必然对不上」。**上面那条判据压过它**：
    描述是模型读到的字，就必须可审查可修改。

    但 S10 担心的漂移是真的，所以**参数 schema 仍由 Python 生成**（`params_type`
    导出），在 agent 管理页上与描述并排只读显示——漂移因此摆在人眼前，
    而不是藏在两个文件里。错误文案仍然留在代码里：它不进上下文，是工具实现的一部分。

    带上 `agent` 是因为**同一个工具在两个 agent 手上可以有两份描述**：
    `state_query` director 拿它是为了推演，state_manager 拿它是为了比对该改哪个键。
    """
    return Tool(
        name=name,
        description=tool_description(agent, name),
        params_type=params,
        handler=handler,
    )


def submit_tool(agent: str = "director") -> Tool[Any]:
    """**没有前置条件了**（S7）：E0 契约（提交前必须写过一次 state）随
    director 卸下 `state_write` 一并作废。
    """
    return _tool(agent, "submit_outline", SubmitOutlineParams, _submit_outline)


def state_query_tool(agent: str) -> Tool[Any]:
    """读侧。**两个 agent 都可能拿到它**：director 读状态推演，state_manager
    读状态决定该改哪个键。只在按需展开开着时才注册（P13）。
    """
    return _tool(agent, "state_query", StateQueryParams, _state_query)


def state_write_tool(agent: str = "state_manager") -> Tool[Any]:
    """写侧。**只给 state_manager**（S7）。

    它以前在 director 手上，边推演边记；现在挪到 writer 之后，读着这一轮的细纲和
    正文去记。换来两件事：记的人看得见**最终写出来的是什么**（正文常常落实了细纲
    没写死的东西），以及 director 那一轮少了一件要惦记的事。
    """
    return _tool(agent, "state_write", StateWriteParams, _state_write)


def history_tools(agent: str = "director") -> list[Tool[Any]]:
    return [
        _tool(agent, "history_search", HistorySearchParams, _history_search),
        _tool(agent, "history_read", HistoryReadParams, _history_read),
    ]


def worldinfo_tools(agent: str = "director") -> list[Tool[Any]]:
    return [
        _tool(agent, "worldinfo_list", WorldInfoListParams, _worldinfo_list),
        _tool(agent, "worldinfo_read", WorldInfoReadParams, _worldinfo_read),
    ]


def summarize_tool(agent: str = "director") -> Tool[Any]:
    return _tool(agent, "summarize", SummarizeParams, _summarize)


def ask_question_tool(agent: str = "director") -> Tool[Any]:
    """**一个题干 + 恰好三个选项**（S23）。

    三个是定死的，不是 2–4 的区间：两个把岔路压成二选一，四个开始出现凑数的那条。
    而**用户随时可以不选、自己写一条**，所以不必留一个「其他」位——实测模型自己
    造过这么一个选项（「其他：你另指定这一轮实际要用的手段」），那是在没有一等
    自由作答入口时的将就，白白花掉三分之一的选项位。

    **剧情控制权那三档就是它描述里的 `choice`**（S10②/S23）：越高越少问，讲的
    正是「这个工具该多积极地调用」，不是创作要求——读到「不要调用 ask_question」
    的地方就该是 `ask_question` 自己。所以这里不再收一个 `authority` 参数，
    当前选哪一档写在 `director.yaml` 的 `tools.ask_question.selected` 上。
    """
    return _tool(agent, "ask_question", AskQuestionParams, _ask_question)


__all__ = [
    "ask_question_tool",
    "history_tools",
    "state_query_tool",
    "state_write_tool",
    "submit_tool",
    "summarize_tool",
    "worldinfo_tools",
]
