"""内置工具。清单与理由见 docs/Tools.md。

**这里装的是全部工具，不是某一个 agent 的工具面**——谁拿到哪几个由
`registry.py` 决定。`state_write` 只给 state_manager，`submit_outline` 只给
director（D143）。

**明确不提供**（同样重要）：任何让 director 读正文的工具、调用 writer 的工具、
生成子 agent、修改元指令 / 控制维度、写文件 / 执行代码。头两条是 D54 的基础——
director 能覆盖五倍历史跨度，正是因为它自己从不读正文；工具返回值会回流到调用者
上下文，正文一旦这样回流，隔离当场就没了。（正文经 state_manager 折成键值再进
state 是另一回事：那是一次有损压缩，且跨了 run 边界。）
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..config import LIMITS
from ..models import ModelAuthority, Node, StateDelta
from .base import Tool, ToolOutcome, ToolResult, error
from .context import RunContext

MAX_READ_CHARS = LIMITS.context.tool_read_chars
"""单次工具返回的体量上限——防止一次调用撑爆上下文。"""


# --------------------------------------------------------------------------
# 产出与终止
# --------------------------------------------------------------------------


class SubmitOutlineParams(BaseModel):
    # 「细纲该写什么、不该写什么」整节在 `director.yaml` 的 `output_contract` 里，
    # 这里不重复（D152）。
    outline: str = Field(description="本轮细纲，一整段文字。")


async def _submit_outline(ctx: RunContext, params: SubmitOutlineParams) -> ToolResult:
    # **E0 契约已作废**（D143）：它原先要求提交细纲前必须写过一次 state_write。
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
    keys: list[str] = Field(description="要查的键。上下文里已列出全部可用的键。")


async def _state_query(ctx: RunContext, params: StateQueryParams) -> ToolResult:
    state = ctx.current_state()
    found = {k: state.values[k] for k in params.keys if k in state.values}
    missing = [k for k in params.keys if k not in state.values]

    if not found:
        # 未知键是模型可恢复的错误——把可用的键回给它，顺便起到防键名漂移的作用。
        return error(
            f"没有这些键：{'、'.join(missing)}。当前可用：{'、'.join(state.keys()) or '（无）'}",
            label=f"查 {'、'.join(missing)} 未命中",
        )

    content = json.dumps(found, ensure_ascii=False)
    if missing:
        content += f"\n（不存在的键：{'、'.join(missing)}）"
    return ToolResult(
        content=content,
        label=f"查了 {'、'.join(params.keys)}",
        payload={"found": list(found), "missing": missing},
    )


class StateWriteParams(BaseModel):
    set: dict[str, Any] = Field(
        default_factory=dict,
        description="覆盖写：键 -> 新值。值为 null 表示删除该键。键可自由命名，"
        "`.` 表示从属（`林.性格`）。",
    )
    append: dict[str, list[Any]] = Field(
        default_factory=dict, description="往清单里追加条目：键 -> 条目。自动去重。"
    )
    remove: dict[str, list[Any]] = Field(
        default_factory=dict,
        description="从清单里拿掉条目：键 -> 条目。不存在的忽略。"
        "**改清单里的某一项就 remove 旧的再 append 新的**，别用 set 整体重写。",
    )


_STATE_WRITE_DESC = "记录本轮的状态变化。可多次调用，累积生效；没有变化就一次也不调。"


async def _state_write(ctx: RunContext, params: StateWriteParams) -> ToolResult:
    delta = StateDelta(set=params.set, append=params.append, remove=params.remove)
    ctx.staged_delta = ctx.staged_delta.merge(delta)
    ctx.state_write_count += 1

    if delta.is_empty():
        return ToolResult(content="已记录：本轮无状态变化。", label="确认本轮无状态变化")

    touched = list(params.set) + list(params.append) + list(params.remove)
    return ToolResult(
        content="状态增量已记录。",
        label=f"更新了 {'、'.join(touched)}",
        payload=delta.model_dump(exclude_defaults=True),
    )


# --------------------------------------------------------------------------
# history —— 全量索引 + 按需展开（D63）
# --------------------------------------------------------------------------


class HistorySearchParams(BaseModel):
    query: str = Field(
        default="",
        description="要找的字眼，会在**各轮细纲全文**里比对（不是只比总结）。留空则只列索引。",
    )
    limit: int = Field(default=20, ge=1, le=100)


async def _history_search(ctx: RunContext, params: HistorySearchParams) -> ToolResult:
    """**命中只做标注，不做筛选**（D62）。

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

    **索引按段给 ref，展开也必须按段给回全文**（D110）。摘要在上下文里
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
    # 模型拿到一份看起来完整的结果，不知道自己丢了什么。那正是 D62 判
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
    **任何经过末节点的路径都得到同一段**，分支天然安全（D48）。
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
        entry = ctx.repo.load_worldbook_entry(ref)
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
# summarize —— 懒惰生成 + 系统缓存（D69）
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
        entry = ctx.repo.load_worldbook_entry(ident)
        if entry is None:
            return error(f"没有这个设定条目：{ident}", label="压缩失败")
        if entry.summary:
            return ToolResult(content=entry.summary, label="复用了已有摘要")
        summary = await _llm_summarize(ctx, entry.content)
        entry.summary = summary
        ctx.repo.save_worldbook_entry(entry)

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
    question: str = Field(description="向用户提出的问题。")
    options: list[str] = Field(description="供用户选择的选项，2–4 个。", min_length=2)


async def _ask_question(ctx: RunContext, params: AskQuestionParams) -> ToolResult:
    if ctx.ask_user is None:
        return error("当前没有可交互的用户。", label="无法提问")

    # 不设超时（D66）：等待期间逻辑上什么都没发生，上下文不会过期。
    answer = await ctx.ask_user(params.question, params.options)
    return ToolResult(
        content=f"用户选择：{answer}",
        label=f"问了用户：{params.question}",
        payload={"question": params.question, "options": params.options, "answer": answer},
    )


# --------------------------------------------------------------------------


def _tool(name: str, description: str, params: type[BaseModel], handler: Any) -> Tool[Any]:
    return Tool(name=name, description=description, params_type=params, handler=handler)


def submit_tool() -> Tool[Any]:
    """**没有前置条件了**（D143）：E0 契约（提交前必须写过一次 state）随
    director 卸下 `state_write` 一并作废。
    """
    return _tool(
        "submit_outline",
        "提交本轮细纲。**调用即结束这一轮**，之后不能再做任何事。",
        SubmitOutlineParams,
        _submit_outline,
    )


def state_query_tool() -> Tool[Any]:
    """读侧。**两个 agent 都可能拿到它**：director 读状态推演，state_manager
    读状态决定该改哪个键。只在按需展开开着时才注册（D62）。
    """
    return _tool(
        "state_query",
        "读取当前世界状态里指定的键。上下文中已给出全部键名与体量小的值，"
        "这里用于展开体量大的那些。",
        StateQueryParams,
        _state_query,
    )


def state_write_tool() -> Tool[Any]:
    """写侧。**只给 state_manager**（D143）。

    它以前在 director 手上，边推演边记；现在挪到 writer 之后，读着这一轮的细纲和
    正文去记。换来两件事：记的人看得见**最终写出来的是什么**（正文常常落实了细纲
    没写死的东西），以及 director 那一轮少了一件要惦记的事。
    """
    return _tool("state_write", _STATE_WRITE_DESC, StateWriteParams, _state_write)


_HISTORY_WHEN = (
    "要原话、要兑现伏笔、要说清先后与因果时才翻——"
    "**当前状态和总结共同的盲区，是「当时不要紧、现在才要紧」的细节。**"
)
"""这一句是**这两个工具各自的适用场合**，属于工具描述（D140）。

它以前有十五行，逐条列了六种该翻、三种不该翻的情形——那是判据，不是用法。
判据本该在提示词里，但 D143 把 `flow_lookup` 那一节删掉之后没有落脚点，于是
一直堆在这儿。收成一句（D152）：**长度本身会稀释它**，六条并列的规则读起来
和一条也没什么区别。真要展开就得先给它一个能按能力开关的 section。
"""


def history_tools() -> list[Tool[Any]]:
    return [
        _tool(
            "history_search",
            "在**各轮细纲全文**里找一个字眼，并列出轮次索引（总结与体量）。\n"
            "**命中只做标注，不筛掉任何一轮**——没命中不等于没发生过，当时可能换了说法。\n\n"
            + _HISTORY_WHEN,
            HistorySearchParams,
            _history_search,
        ),
        _tool(
            "history_read",
            "按 ref 展开某几轮细纲的**全文**。ref 在上下文的历史索引里已经给出，"
            "知道是哪几轮就直接读，不必先 history_search。\n\n" + _HISTORY_WHEN,
            HistoryReadParams,
            _history_read,
        ),
    ]


def worldinfo_tools() -> list[Tool[Any]]:
    return [
        _tool(
            "worldinfo_list",
            "列出按需展开的设定条目索引（名称、摘要、体量）。"
            "常驻设定已直接在上下文里，不在这个索引中。",
            WorldInfoListParams,
            _worldinfo_list,
        ),
        _tool("worldinfo_read", "展开指定设定条目的全文。", WorldInfoReadParams, _worldinfo_read),
    ]


def summarize_tool() -> Tool[Any]:
    return _tool(
        "summarize",
        "把过长的内容压缩成摘要。结果会被缓存，之后出现在索引里。",
        SummarizeParams,
        _summarize,
    )


_ASK_QUESTION_EAGERNESS = {
    ModelAuthority.HIGH: "**这一档不要调用它，自己拿主意。**",
    ModelAuthority.MEDIUM: "剧情走向以你为主。本轮若存在真正的分岔，可以调用一次让用户选。",
    ModelAuthority.LOW: (
        "用户主导剧情走向。遇到任何值得由用户决定的分岔都调用它征询，一轮内可以多次。"
    ),
}
"""**剧情控制权直接反映为这个工具的调用积极性**（D39/D58）——越高越少问。

这段措辞以前是 `director.yaml` 里一个叫 `authority` 的选项组，单独占一条消息。
搬到这里的理由是它讲的**只是一个工具该多积极地调用**，而不是创作要求：
读到「不要调用 ask_question」的地方就该是 `ask_question` 自己。
控制维度本身没变，仍然是 `ControlDimensions.model_authority` 三档（D140）。
"""


def ask_question_tool(authority: ModelAuthority = ModelAuthority.MEDIUM) -> Tool[Any]:
    return _tool(
        "ask_question",
        "向用户提问并等待选择。用于剧情方向不明、或用户指令与既有设定冲突时。\n"
        + _ASK_QUESTION_EAGERNESS[authority],
        AskQuestionParams,
        _ask_question,
    )


__all__ = [
    "ask_question_tool",
    "history_tools",
    "state_query_tool",
    "state_write_tool",
    "submit_tool",
    "summarize_tool",
    "worldinfo_tools",
]
