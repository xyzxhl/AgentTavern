"""谁收到什么。

**哪些 context 给谁，是整个拓扑的地基**（D33 / Generation.md §3）。切法是：
director 只读细纲历史、不读正文；writer 读正文上文以保证衔接。这一刀切开了最大的
共享项，两侧才真正可分。

因此这里必须守住：

- **文风与角色语言习惯只进 writer。** 它们不参与检索，参与每一个 token。
- **正文只进 writer。** director 能覆盖五倍历史跨度正是因为它从不读正文（D54）。
- **否定项两边都要。** 细纲说"会发生什么"，否定项说"不能写什么"，按定义不在细纲里；
  writer 为求文采添细节时可能泄露伏笔（D35）。体量极小，不破坏隔离。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..llm import Message
from ..models import (
    Capabilities,
    ControlDimensions,
    SessionMeta,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
)
from .assembler import Prompt, assemble, text


def _speaking(allowed: bool) -> str:
    """抢话是个二选一的控制维度，所以在 YAML 里是选项组，选项名就是这两个。

    **只有 director 侧有这个选项组。** writer 侧那条曾经也在（D35：否定项两边都要），
    后来 writer 的输入契约收紧成「除非细纲已经给出，否则不要自行增加新行为、
    新决定、新心理」——用户的角色被这条一并盖住了，再单给一条就是重复。
    """
    return "allowed" if allowed else "forbidden"


STATE_INLINE_LIMIT = 200
"""单个状态值超过这个长度就只给体量、不给内容，由模型按需 state_query 展开。"""


def _render_value(value: object, *, expandable: bool = True) -> str:
    """渲染一个状态值，**并让它的形状可见**。

    清单原先渲染成 `青锋剑、止血符 ×2、定身符 ×1`，与一个字符串**长得完全一样**。
    模型据此写回时自然写成字符串，清单就被拍平了，此后再也无法单独增删其中一项
    （D96 的实测来源）。标出“清单”是最便宜的修法：它同时告诉模型这里可以
    append / remove。
    """
    if isinstance(value, list):
        if not value:
            return "（清单，空）"
        body = "、".join(str(v) for v in value)
        prefix = "清单："
    elif isinstance(value, dict):
        # 空字典曾渲染成「（无）」，形状信息全丢——与 D96 里空清单的病一模一样。
        # 实测后果：种子给的 `沈砚.冷却: {}` 被写回成一个字符串。
        if not value:
            return "（无，可逐项填）"
        body = "；".join(f"{k}={v}" for k, v in value.items())
        prefix = ""
    elif isinstance(value, bool):
        body, prefix = ("是" if value else "否"), ""
    else:
        body, prefix = str(value), ""

    # **只有还能展开时才允许截断。** 截断 + 按需展开是无损降维；一旦没了展开的路，
    # 它就变成有损筛选——模型看得见键名却永远拿不到内容，而且不知道自己丢了什么。
    # 那正是 D62 判 lorebook 关键词触发不合格的同一条理由。
    if expandable and len(body) > STATE_INLINE_LIMIT:
        return f"（{len(body)} 字，用 state_query 展开）"
    return prefix + body


def render_state(state: WorldState, *, expandable: bool = True) -> str:
    """状态默认包：**全量键名 + 体量小的值内联，大的只给体量**。

    这与 history / worldinfo 是同一个模式（D63 全量索引 + 按需展开），因此同样
    满足 D62 的准入标准：规则只调精度、不做相关性筛选，模型始终知道有哪些键存在。

    **不预设任何维度。** 键由模型自由命名，`.` 前缀仅用于此处分组显示，
    系统不赋予它语义，时间、地点、角色属性的粒度与形态都取决于故事，
    定不了固定 schema（见 models/state.py 的说明）。
    """
    if not state.values:
        return "（尚无记录）"

    lines: list[str] = []
    for group, items in state.grouped().items():
        if not group:
            lines += [f"{k}：{_render_value(v, expandable=expandable)}" for k, v in items.items()]
    for group, items in state.grouped().items():
        if group:
            lines.append(f"\n{group}")
            lines += [f"  {k}：{_render_value(v, expandable=expandable)}" for k, v in items.items()]

    lines.append(
        "\n这是一张**速查表**，不是完整记录，事情本身在前情里。"
        "**新增记录时优先复用上面已有的键名与形状**，避免同一概念出现多个写法。\n"
        "**上面任何一个键，只要本轮之后它的值不再成立，就要改掉或删掉**，"
        "留着的旧值会被当成事实，不会被当成过时信息。不再约束后续的键 `set` 成 `null`；"
        "删了不算丢东西，那件事仍然在前情里。"
    )
    return "\n".join(lines)


def render_worldinfo(entries: list[WorldbookEntry], *, expandable: bool = True) -> str:
    """常驻设定全量注入；按需展开的只给索引（名称 + 摘要 + 体量）。

    索引**覆盖全部按需条目**，不做任何预筛选（D62），筛选子集式的规则
    （如 lorebook 的关键词触发）不合格：漏命中时条目完全缺失，
    而模型根本不知道它存在。
    """
    # **关闭按需展开时，所有条目一律全文注入**——没有展开的路还只给索引，
    # 就成了模型看得见名字却永远拿不到内容的有损筛选（D62）。
    # 设定跨轮不变，全量注入的代价只付一次（它在缓存前缀里，D114）。
    if not expandable:
        return "\n\n".join(f"### {e.name}\n{e.content}" for e in entries if e.content)

    always = [e for e in entries if e.mode is WorldbookMode.ALWAYS]
    on_demand = [e for e in entries if e.mode is WorldbookMode.ON_DEMAND]

    parts = [f"### {e.name}\n{e.content}" for e in always if e.content]
    if on_demand:
        index = "\n".join(
            f"- `{e.ref_hint()}` {e.name}（{len(e.content)} 字）：{e.summary_or_head()}"
            for e in on_demand
        )
        parts.append(f"### 可展开的条目\n{index}\n\n用 worldinfo_read 按 ref 展开。")
    return "\n\n".join(parts)


def _flow(caps: Capabilities, worldinfo: list[WorldbookEntry] | None) -> str:
    """流程步骤。跨轮不变，因此属于静态前缀。"""
    steps = ["需要更多信息时用工具查。"] if _has_lookup_tools(caps, worldinfo) else []
    steps.append("想清楚本轮发生什么，把事实写下来。")
    if caps.state:
        steps.append(
            "调用 `state_write` 记录本轮的状态变化，**没有变化也必须调用一次**"
            "（不带参数即可），否则无法区分“确实没变”和“忘了记”。\n"
            "   写之前先对每一个键问一句：**下一轮开始时，还有人需要看它吗？**\n"
            "   “他正扑过来”“她刚把照片推过去”答案是否，那是这一秒的画面，\n"
            "   下一轮它已经不成立了，而画面在正文里。答不上来的键就不要写。\n"
            "   **再问一句：这一轮有什么东西被特意摆出来，却还没有下文？**\n"
            "   掏出来的枪、许下的承诺、没被回答的问题、没拆的信，它们什么也不禁止，\n"
            "   所以最容易漏；漏了不会出错，只会烂尾。"
        )
    steps.append("调用 `submit_outline` 提交。**这一步会结束本轮，只能调用一次。**")
    return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))


def director_prompt(
    meta: SessionMeta,
    control: ControlDimensions,
    state: WorldState,
    user_text: str | None,
    *,
    history: Sequence[Message] = (),
    capabilities: Capabilities | None = None,
    worldinfo: list[WorldbookEntry] | None = None,
) -> Prompt:
    """装配 director 收到的整串消息。

    **顺序、标题、role 都不在这里，在 director.yaml 的 `sections` 里**（D135–D137）。
    这里只回答三个问题：每段 context 注入什么、变量的值是什么、选项组选哪一档。

    但有一条约束是这里保证不了的、也不该由这里保证：**前情之前的部分跨轮不变**
    （D114）。对话跨轮持续，那一段就是缓存前缀，凡是每轮会变的东西排在它前面，
    缓存就永远打不中，而且旧值会以"写在最前面的权威信息"的姿态留着。
    所以 state 与本轮要求都排在 `history` 之后——**这是 YAML 里的顺序决定的**，
    改动它会付出缓存代价。
    """
    caps = capabilities or Capabilities()
    return assemble(
        "director",
        contexts={
            "worldinfo": render_worldinfo(worldinfo or [], expandable=caps.worldinfo_read),
            "forbidden": "\n".join(f"- {item}" for item in meta.forbidden),
            "flow": _flow(caps, worldinfo),
            "history": history,
            "state": render_state(state, expandable=caps.state_query) if caps.state else "",
            "user_input": user_text or text("director", "no_user_input"),
        },
        values={
            "genre_tone": meta.genre_tone,
            "content_scale": meta.content_scale,
            "pov": meta.pov,
            "goal": meta.goal.description if meta.goal else "",
        },
        options={
            "narration_and_advancement": control.input_handling.value,
            "input_handling": control.input_handling.value,
            # **事件密度是「铺陈密度」的反向投影**：director 不该知道篇幅（D93），
            # 所以同一个控制维度在两侧读的是两组不同的措辞。
            "event_density": control.expansion_density.value,
            "authority": control.model_authority.value,
            "speaking_rule": _speaking(control.allow_speaking_for_user),
        },
    )


def _has_lookup_tools(caps: Capabilities, worldinfo: list[WorldbookEntry] | None) -> bool:
    return (
        caps.state
        or caps.history_search
        or any(e.mode is WorldbookMode.ON_DEMAND for e in worldinfo or [])
    )


def writer_prompt(
    meta: SessionMeta,
    control: ControlDimensions,
    *,
    outline: str = "",
    upstream: str = "",
) -> Prompt:
    """装配 writer 收到的整串消息：**只有文风相关的东西 + 否定项 + 上文 + 细纲**。

    它拿不到 state 的值、拿不到世界设定、拿不到用户原始指令，那些都已经在细纲里了。
    它的职责是改写，不是补充。
    """
    return assemble(
        "writer",
        contexts={
            "character_voices": "\n".join(
                f"- {name}：{how}" for name, how in meta.character_voices.items()
            ),
            "forbidden": "\n".join(f"- {item}" for item in meta.forbidden),
            "upstream": upstream,
            "outline": outline,
        },
        values={"prose_style": meta.prose_style, "pov": meta.pov},
        options={"density": control.expansion_density.value},
    )
