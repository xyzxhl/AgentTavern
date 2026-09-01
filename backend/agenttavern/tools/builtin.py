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

from ..disclosure import expand, keys_of
from ..models import StateDelta, WorldbookMode
from ..models.state import Tree, paths_of
from ..models.state import label_path as _label
from ..prompting import tool_description
from .base import Tool, ToolOutcome, ToolResult, error
from .context import RunContext

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
# 展开：三个域，一份实现（S157）
# --------------------------------------------------------------------------
#
# 折叠时留下的占位符和这里接受的地址**必须是同一套 key**，所以折叠那一半
# （`disclosure.fold`，由 `prompting/prompts.py` 调用）和展开这一半共用一个模块。
# 三个工具因此长得一模一样：各自把自己那个域摊成一棵 `key → content` 的树，
# 剩下的（取、报缺、拼回执）走同一个 `_expand`。
#
# **不是一个 `expand(域, 路径)` 工具**：工具面按开关增减（S21），历史关掉时
# 它的展开工具要跟着消失，而一个大工具只能"注册着但有一半用不了"。


class ExpandParams(BaseModel):
    """三个展开工具共用的参数形状：**一条路径一个数组，一段一个键**（S31）。

    不用点分字符串：点号在这个项目里刚被取消掉结构含义，再拿它当寻址语法
    等于把刚拆掉的歧义装回去，而且键里本来就可能有点。
    """

    paths: list[list[str]] = Field(
        # **不举例子。** 参数描述里的示例键名会被当成摆法照抄，而上下文里已经有
        # 一份真的树摆在那里——那才是该照着写的东西。
        description="要展开的位置，每个是一条路径，一段一个键。"
    )


async def _expand(
    tree: Tree,
    paths: list[list[str]],
    *,
    what: str,
) -> ToolResult:
    """按 key 取回内容。**三个域共用**（S157）。

    一条都没命中时**把可用的 key 整个回给模型**：一是让它下一轮改对，二是顺带
    压键名漂移——它看见"心情"已经存在，就不会再造一个"情绪"。这是模型可恢复的
    错误，不是系统故障（S28）。

    **不分页、不按体量截断**：展开就是全给。以前这里各有一套预算和「没给全」的
    话术，那是在这一层猜"多少算多"；真的超了上下文是整轮失败，不是这一层能补救
    的事，而一个猜出来的预算会让模型在不知情的情况下少看几段（S26）。
    """
    got = expand(tree, paths)

    if got.empty:
        available = "、".join(keys_of(tree)) or "（无）"
        return error(
            f"没有这些位置：{'、'.join(got.missing)}。当前有：{available}",
            label=f"展开{what} {'、'.join(got.missing)} 未命中",
        )

    content = json.dumps(got.found, ensure_ascii=False)
    if got.missing:
        content += f"\n（不存在的位置：{'、'.join(got.missing)}）"
    return ToolResult(
        content=content,
        label=f"展开了{what} {'、'.join(got.found)}",
        payload={"found": list(got.found), "missing": got.missing},
    )


# -- state ------------------------------------------------------------------


async def _state_query(ctx: RunContext, params: ExpandParams) -> ToolResult:
    """状态那棵树。**读的是 `current_state`**：本轮已经暂存的写入要算进去，
    否则 state_manager 刚写完一笔、回头一查却查不到。
    """
    return await _expand(ctx.current_state().values, params.paths, what="状态")


class StateWriteParams(BaseModel):
    set: dict[str, Any] = Field(
        default_factory=dict,
        description="覆盖写，**深合并**：只动你提到的那几个键，同层别的原样留着。"
        "从属关系用嵌套表达。值为 null 表示删掉该键。",
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


# -- worldinfo：条目名 → 正文 -------------------------------------------------


async def _worldinfo_read(ctx: RunContext, params: ExpandParams) -> ToolResult:
    """设定那棵树，**一层：条目名 → 正文**（S157）。

    地址是**名字**，不是 id：名字既是提要也是地址，和状态那棵树一样。
    重名在写入时就收敛掉了（`WorldbookRepository.save_entry`），所以这里
    不会有两条同名条目争一个地址。
    """
    return await _expand(_worldinfo_tree(ctx), params.paths, what="设定")


def _worldinfo_tree(ctx: RunContext) -> Tree:
    """**只有按需条目在这棵树上**：常驻的那些正文已经在上下文里了。

    把它们也放进来，模型会去展开一段它眼前就有的东西——白花一轮往返。
    """
    return {
        entry.name: entry.content
        for entry in ctx.repo.worldbook()
        if entry.is_lore and entry.mode is WorldbookMode.ON_DEMAND
    }


# -- history：段落 id → 那几轮的细纲 ------------------------------------------


async def _history_read(ctx: RunContext, params: ExpandParams) -> ToolResult:
    """前情那棵树，**两层：段落 id → 轮次 id → 细纲**（S57/S157）。

    **段落是唯一用 id 当地址的域**——它没有名字，只有一段摘要，而摘要当不了
    地址（太长、会重、传回来会被改写一两个字）。所以上下文里的占位符是
    「id + 摘要」：摘要是提要，id 是地址。

    **索引按段折叠，展开也必须按段给回全文**：摘要在上下文里顶替的是**整段**
    ——那几轮的细纲全被它替掉了。只还回末节点那一轮，等于告诉模型"这段就这么点
    内容"，比不展开更糟：它会以为自己已经看全了。
    """
    return await _expand(_history_tree(ctx), params.paths, what="前情")


def _history_tree(ctx: RunContext) -> Tree:
    """路径上每一段：``{段落 id: {轮次 id: 细纲}}``。

    段落按「末节点 + 跨度」寻址：父指针唯一，所以从末节点上溯 ``span - 1`` 步
    得到的链是确定的，**任何经过末节点的路径都得到同一段**，分支天然安全（S42）。
    """
    if ctx.parent_id is None:
        return {}

    nodes = ctx.repo.all_nodes()
    tree: Tree = {}
    for end in ctx.repo.path_to(ctx.parent_id):
        if not end.outline_summary:
            # 没归过段的轮次原文还在上下文里，不必也不该从这里再取一次。
            continue
        chain, current = [end], end
        for _ in range(max(1, end.segment_span) - 1):
            if current.parent_id is None or current.parent_id not in nodes:
                break
            current = nodes[current.parent_id]
            chain.append(current)
        tree[end.id] = {n.id: n.outline for n in reversed(chain)}
    return tree


# --------------------------------------------------------------------------
# close_segment —— 只在生成后追问里出现，绝不进 loop（S22/S58）
# --------------------------------------------------------------------------


class CloseSegmentParams(BaseModel):
    after: int = Field(
        ge=1,
        description="断在待归段列表的第几条之后。第 1 到第 k 条合成一段，第 k+1 条起属于下一段。",
    )


async def _close_segment(ctx: RunContext, params: CloseSegmentParams) -> ToolResult:
    """**没有实现，也不该有。**

    它不走 `ToolRegistry.dispatch`：生成后追问是一次**独立的、不带 loop 的调用**，
    编排层直接读回复里的这次调用（`agents/postrun.py`），拿到那个数字就完了。
    它做成一个工具是为了**答案有形状**——以前是让模型回一个数字，代码从自由
    文本里抓第一串数字，而抓错的后果不可逆且不出声。

    留这个 handler 只为让它和别的工具同形（描述从 YAML 取、参数由 Python 生成，
    在提示词页上并排显示）。真被调到说明它被错误地注册进了某个工具面。
    """
    return error(
        "close_segment 只在生成后追问里可用，不在这一轮的工具面里。",
        label="close_segment 不该出现在这里",
    )


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
    return _tool(agent, "state_query", ExpandParams, _state_query)


def state_write_tool(agent: str = "state_manager") -> Tool[Any]:
    """写侧。**只给 state_manager**（S7）。

    它以前在 director 手上，边推演边记；现在挪到 writer 之后，读着这一轮的细纲和
    正文去记。换来两件事：记的人看得见**最终写出来的是什么**（正文常常落实了细纲
    没写死的东西），以及 director 那一轮少了一件要惦记的事。
    """
    return _tool(agent, "state_write", StateWriteParams, _state_write)


def history_read_tool(agent: str = "director") -> Tool[Any]:
    """前情的展开。**`history_search` 没了**（S54 整条撤销）。

    它做的是"在细纲全文里逐字比对，命中的多一个标记"。而索引本来就覆盖全部
    段落、摘要就在眼前——命中标记能加的信息只有"这几个字逐字出现过"，
    而伏笔常常换了个说法，逐字查不中是常态。它买到的那点东西不值一个工具位。
    """
    return _tool(agent, "history_read", ExpandParams, _history_read)


def worldinfo_read_tool(agent: str = "director") -> Tool[Any]:
    """设定的展开。**`worldinfo_list` 没了**：索引每轮都在上下文里
    （`render_worldinfo`），再给一个"列出索引"的工具等于让模型花一轮
    去要一份它眼前就有的东西。"""
    return _tool(agent, "worldinfo_read", ExpandParams, _worldinfo_read)


def close_segment_tool(agent: str = "director") -> Tool[Any]:
    """归段那一刀。**不进任何工具面**（S22）——只有生成后追问那一次调用带上它。

    `summarize` 曾经在这里，是 director 手上的一个工具（S25：按引用寻址、
    懒惰生成）。**它删掉了**：压缩由编排层在轮次滑出全文窗口时自己做
    （`orchestration/run.py` 的 `close_segment`），模型不该有一个能主动
    触发压缩的入口——判定和压缩本来就不同步，给它这个工具等于请它去动
    一件它管不着、也看不见后果的事。
    """
    return _tool(agent, "close_segment", CloseSegmentParams, _close_segment)


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
    "close_segment_tool",
    "history_read_tool",
    "state_query_tool",
    "state_write_tool",
    "submit_tool",
    "worldinfo_read_tool",
]
