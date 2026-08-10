"""director 的内置工具。清单与理由见 docs/Tools.md。

**明确不提供**（同样重要）：任何读正文的工具、调用 writer 的工具、生成子 agent、
修改元指令 / 控制维度、写文件 / 执行代码。前两条是 D54 的基础——director 能覆盖
五倍历史跨度，正是因为它从不读正文；工具返回值会回流到调用者上下文，正文一旦
回流，整个隔离设计就没了。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..agents.summary import summarize_text
from ..models import ModelAuthority, Node, StateDelta
from .base import Tool, ToolOutcome, ToolResult, error
from .context import RunContext

MAX_READ_CHARS = 8000
"""单次工具返回的体量上限——防止一次调用撑爆上下文。"""


# --------------------------------------------------------------------------
# 产出与终止
# --------------------------------------------------------------------------


class SubmitOutlineParams(BaseModel):
    outline: str = Field(
        description="本轮细纲：**只写事实，不做描写**。\n"
        "事实指发生了什么——动作、对白的具体内容、在场的人、关键物件、"
        "谁知道了什么。用平实的话按顺序写下来，不必是条目，但也不要写成成品散文。\n"
        "描写、感官、修辞、节奏一概不写——那是下一个环节的活，**你写了它只会照抄**。"
    )


async def _submit_outline(ctx: RunContext, params: SubmitOutlineParams) -> ToolResult:
    # state 能力关闭时 E0 契约随之失效——没有 state_write 可调，就不能要求它。
    if ctx.capabilities.state and ctx.state_write_count == 0:
        # E0 契约。空 delta 合法，但必须显式提交过——否则无法区分
        # "确实没有变化"与"模型忘了提交"。
        return error(
            "还没有提交状态增量。请先调用 state_write（没有变化就传空的 delta），再提交细纲。",
            label="缺少状态增量，退回",
        )

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
        description="覆盖写：键 -> 新值。值为 null 表示删除该键。\n"
        "键可自由命名；用 `.` 表示从属，如 `林.性格`、`时间.距上轮`。"
        "**优先复用上下文里已有的键名**，避免同一概念出现多个写法。",
    )
    append: dict[str, list[Any]] = Field(
        default_factory=dict,
        description="追加写：键 -> 要追加的条目"
        "（用于清单，如某角色的知情、身上带的东西）。自动去重。",
    )
    remove: dict[str, list[Any]] = Field(
        default_factory=dict,
        description="从清单里拿掉条目：键 -> 要拿掉的条目（东西用完了、人离队了、疑问解开了）。"
        "**要改清单里的某一项，就 remove 旧的再 append 新的**，"
        "不要用 set 整体重写——重写容易丢项。不存在的条目会被忽略。",
    )


_STATE_WRITE_DESC = """记录本轮的状态变化。可多次调用，累积生效。本轮没有任何变化时也**必须**\
调用一次（不带任何参数即可）。

**事情本身已经记在前情里了。state 不是第二份记录，是一条捷径**——把你每一轮
都要用到、又不该每次回头去翻的那几条，拎出来摆在眼前。

**先过一道门槛：你需要随时看见它，不该为它去翻前情。** 这一轮的气氛、谁站在
柜台哪一侧、物件的朝向——前情里本来就有，抄进来只是占地方。

过了门槛之后，**该记的东西有两类，它们"忘了的后果"完全不同**：

**一、约束类：忘了会写出「不可能发生」的事。**
谁死了、谁不在场、谁不知道什么、受了什么伤、身上还剩几块钱、法术还在冷却、
什么时候要还债。写错这些，读者一眼就看出来。

**二、悬置类：忘了不会出错，但会烂尾。**
枪被掏出来搁在桌上、他答应了三天后给答复、她问的那句话他没有回答、
有人撂下"我还会回来"、一封信没拆开、一扇门谁也没提要不要开。
这些**什么也不禁止**——忘了它不会产生任何矛盾，只会让埋下的东西再也没有下文。

**悬置类是最容易漏的一类，因为它过不了"会不会出逻辑错误"这一关。**
枪挂在墙上，不写它照样写得下去；正因为如此，它才需要被特意记住。

判据是：**有人特意让它出现，而它还没有下文。**
不是所有出现过的物件——没人在意的东西不是伏笔，是布景。桌上的杯子不记，
被人拿起来又放下的那个杯子才可能要记。

**兑现之后立刻删掉**（`set` 成 `null`）。枪响了、答复给了、问题答了，那个键就完成了它的任务。
这样 state 里的悬置项始终等于**当前还欠着的账**，一眼能看完。

---

**一个更快的自检：下一轮开始时，这条还成立吗？**「他正扑过来」「她刚把照片推过去」
「即将落身」到下一轮就不成立了——那是**这一秒的画面**，而画面归正文。
真正该记的是画面留下的东西：他受了伤、她把照片给出去了、那件事被说破了。

**存在时间长短不是判据。**「冷却还剩两回合」「身上只剩一块灵石」活不过几轮，
但漏了立刻出错，必须记；「照片正面朝上」可以一直成立，却什么也约束不了，不必记。

## 写成能一眼看清的形状

它是给你自己用的速查表，所以形状比文采重要：

- **能用数字就别用句子**：`沈砚.灵石: 1`，不要 `"身上还剩一块灵石"`。
- **能用是/否就别用描述**：`陈屿.在场: true`。
- **一类东西归进一个清单**：`沈砚.随身`、`林昭.知情`。
- **一个键只装一件事。** 值里出现分号，通常说明它该拆成几个键，或者根本不该记。
  键要写成「谁的什么」（`陈屿.在场`、`沈砚.灵石`），光写实体名（`许平`）会让这个键
  变成杂物堆——什么都往里塞，以后谁也改不动它。
- **同类的事用同一套键名。** 别一个人叫 `沈砚.灵石`、另一个叫 `孙执事.身上的钱`。
- **值的形状保持不变。** 原来是数字就还写数字（`3` 不是 `"3"`），原来是清单就还是清单——
  把清单拍成一句话，以后就再也没法单独增删其中一项了。

**不要记「未明」「不确定」「尚未」。** 没记的东西本来就是未知的，写出来只是占地方。
也不要记谁说的、怎么知道的——那属于「某人.知情」，不属于被说的那个东西。

**该删就删**，两类各有各的了结方式：约束类**不再约束任何后续内容**了就删；
悬置类**兑现了**就删（枪响了、答复给了、信拆了）。把它 `set` 成 `null` 即可。
删了不算丢东西——那件事仍然在前情里。**state 越短，你越不容易看漏真正要紧的几条。**

## 过期的值比没有值更糟

上下文里已经列出的键是**当前世界的样子**。**只要本轮之后某个键的值不再成立，
就必须改它或删它**——留在那儿的旧值不会被当成过时信息，会被当成事实。

场景换了尤其容易漏：换了地方、隔了一夜、人走了，原来记着的地点、时间、
谁在场就全都不对了，却常常只顾着记新东西。

**带倒计时的值每一轮都要重算一遍——它不会自己往下走。**
「还剩三回合」「三天后到期」「一个时辰后关门」这类，本轮过去了就得改成新的数；
你不改，它下一轮读起来仍是三回合。**漏一轮不会报错，只会在几轮后变成一个对不上的数。**

**新建一个键之前，先看上面有没有已经在说同一件事的键。**「位置」和「地点」、
「在场」和「在不在」是同一件事的两种写法；一旦两个都存在，它们就再也对不齐了，
而且你还会以为自己已经更新过了。"""


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
    for rid in params.run_ids:
        for node in _segment_of(ctx, known[rid]):
            outline = node.outline[:budget]
            budget -= len(outline)
            out.append({"run_id": node.id, "segment": rid, "outline": outline})
            if budget <= 0:
                break
        if budget <= 0:
            break

    return ToolResult(
        content=json.dumps(out, ensure_ascii=False),
        label=f"展开了 {len(out)} 轮细纲",
        payload={"run_ids": [o["run_id"] for o in out]},
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
    for ref in params.refs:
        entry = ctx.repo.load_worldbook_entry(ref)
        if entry is None:
            return error(f"没有这个设定条目：{ref}", label="展开设定失败")
        chunk = entry.content[params.start : params.start + budget]
        budget -= len(chunk)
        out.append(
            {
                "ref": ref,
                "name": entry.name,
                "content": chunk,
                "truncated": params.start + len(chunk) < len(entry.content),
            }
        )
        if budget <= 0:
            break

    return ToolResult(
        content=json.dumps(out, ensure_ascii=False),
        label=f"展开了 {len(out)} 条设定",
        payload={"refs": [o["ref"] for o in out]},
    )


# --------------------------------------------------------------------------
# summarize —— 懒惰生成 + 系统缓存（D69）
# --------------------------------------------------------------------------


class SummarizeParams(BaseModel):
    source: str = Field(
        description="要压缩的对象：``run:<run_id>``（某轮细纲）或 ``wi:<ref>``（某条设定）。"
    )
    max_chars: int = Field(default=200, ge=40, le=1000)


async def _summarize(ctx: RunContext, params: SummarizeParams) -> ToolResult:
    kind, _, ident = params.source.partition(":")

    if kind == "run":
        node = ctx.repo.all_nodes().get(ident)
        if node is None:
            return error(f"没有这个 run：{ident}", label="压缩失败")
        if node.outline_summary:
            return ToolResult(content=node.outline_summary, label="复用了已有摘要")
        summary = await _llm_summarize(ctx, node.outline, params.max_chars)
        node.outline_summary = summary
        ctx.repo.save_node(node)

    elif kind == "wi":
        entry = ctx.repo.load_worldbook_entry(ident)
        if entry is None:
            return error(f"没有这个设定条目：{ident}", label="压缩失败")
        if entry.summary:
            return ToolResult(content=entry.summary, label="复用了已有摘要")
        summary = await _llm_summarize(ctx, entry.content, params.max_chars)
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


async def _llm_summarize(ctx: RunContext, content: str, max_chars: int) -> str:
    return await summarize_text(ctx.gateway, content, max_chars)


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


def submit_tool(require_state_write: bool) -> Tool[Any]:
    contract = "调用前必须至少调用过一次 state_write。" if require_state_write else ""
    return _tool(
        "submit_outline",
        "提交本轮细纲并结束。**每轮有且仅有一次**，调用后不能再做任何事。" + contract,
        SubmitOutlineParams,
        _submit_outline,
    )


def state_tools(*, query: bool = True) -> list[Tool[Any]]:
    """``query=False`` 时只注册写入——state 改为每轮全量注入，没有可展开的东西。"""
    tools = [_tool("state_write", _STATE_WRITE_DESC, StateWriteParams, _state_write)]
    if query:
        tools.insert(
            0,
            _tool(
                "state_query",
                "读取当前世界状态里指定的键。上下文中已给出全部键名与体量小的值，"
                "这里用于展开体量大的那些。",
                StateQueryParams,
                _state_query,
            ),
        )
    return tools


_HISTORY_WHEN = """**当前状态和总结共同的盲区，是「当时不要紧、现在才要紧」的细节。**

当前状态只记会约束后续的东西，别的一概不记；总结保留的是**写它那一刻**认为重要的东西。
而伏笔的价值天生是后来才显现的——埋下去的时候，它两边都进不去。

这几种情形去翻原文：

- 要**引用早前说过的原话**。总结会改写措辞，当前状态里根本没有原话。
- 要**兑现或推翻一个伏笔**：早前提过的名字、日期、物件、一句没头没尾的话。
- 要说清**先后与因果**。当前状态只有此刻的值，总结把顺序压平了。
- **总结和当前状态对不上**。总结是压缩过的，以原文为准。
- 用户**直接指某一轮**（「回到他们在酒店那次」「像上次那样」）。
- 你要写的东西**可能和早前矛盾，但说不清矛盾在哪**。

**这几种不用翻**：想知道现在是什么样（看当前状态）、想接住上一轮的语气
（近几轮细纲已经在上下文里）、只想知道大概发生过什么（总结够用）。"""


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
    "state_tools",
    "submit_tool",
    "summarize_tool",
    "worldinfo_tools",
]
