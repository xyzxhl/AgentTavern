"""上下文装配。

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

from ..models import (
    Capabilities,
    ControlDimensions,
    InputHandling,
    SessionMeta,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
)
from .prompt_text import assemble, choice, text


def _section(title: str, body: str) -> str:
    return f"## {title}\n{body}" if body.strip() else ""


def _speaking_rule(allowed: bool) -> str:
    """抢话规则。

    **转述与抢话必须对照着说**，两者最容易混，而它们的边界是可以钉死的：
    转述处理的是**已经写出的**，抢话是替用户写**尚未发生的**。
    只分开说各自的规则，模型会在边界上摇摆。
    """
    if allowed:
        return text("director", "speaking_allowed")
    return text("director", "speaking_forbidden")


def _writer_speaking_rule(allowed: bool) -> str:
    """writer 侧的抢话规则。

    **这条以前不需要**：writer 只会照抄细纲，够不着这条线。现在它有了发挥空间，
    就会顺手把用户角色再往前推一步，“她低头，指尖压了一下照片”这种。
    在 director 那边写了不算数，越界发生在这一侧（D35：否定项两边都要）。
    """
    if allowed:
        return ""
    return text("writer", "speaking_forbidden")


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
        text = "、".join(str(v) for v in value)
        prefix = "清单："
    elif isinstance(value, dict):
        # 空字典曾渲染成「（无）」，形状信息全丢——与 D96 里空清单的病一模一样。
        # 实测后果：种子给的 `沈砚.冷却: {}` 被写回成一个字符串。
        if not value:
            return "（无，可逐项填）"
        text = "；".join(f"{k}={v}" for k, v in value.items())
        prefix = ""
    elif isinstance(value, bool):
        text, prefix = ("是" if value else "否"), ""
    else:
        text, prefix = str(value), ""

    # **只有还能展开时才允许截断。** 截断 + 按需展开是无损降维；一旦没了展开的路，
    # 它就变成有损筛选——模型看得见键名却永远拿不到内容，而且不知道自己丢了什么。
    # 那正是 D62 判 lorebook 关键词触发不合格的同一条理由。
    if expandable and len(text) > STATE_INLINE_LIMIT:
        return f"（{len(text)} 字，用 state_query 展开）"
    return prefix + text


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


def director_static_prefix(
    meta: SessionMeta,
    control: ControlDimensions,
    *,
    capabilities: Capabilities | None = None,
    worldinfo: list[WorldbookEntry] | None = None,
) -> str:
    """**跨轮不变的那部分**，用作持续对话的 system prompt（D114）。

    对话跨轮持续之后，system prompt 就是缓存前缀的开头，**凡是每轮都会变的
    东西都不能放在这里**，否则前缀每轮都变，缓存永远打不中，而且旧值会以
    "写在最前面的权威信息"的姿态留在上下文里。

    因此 state 与前情移到每一轮的用户消息里（``director_turn_prompt``），
    这里只留：身份与分工、题材设定、世界设定、写法规则、禁止事项、流程。
    """
    setting = "\n".join(
        filter(
            None,
            [
                f"题材与基调：{meta.genre_tone}" if meta.genre_tone else "",
                f"尺度：{meta.content_scale}" if meta.content_scale else "",
                f"视角：{meta.pov}" if meta.pov else "",
            ],
        )
    )
    caps = capabilities or Capabilities()
    narration = (
        text("director", "narration")
        if control.input_handling is not InputHandling.ADVANCE_ONLY
        else ""
    )
    forbidden = "\n".join(f"- {item}" for item in meta.forbidden)
    goal = ""
    if meta.goal:
        goal = f"本次创作的目标：{meta.goal.description}\n达成之前会持续推进。"

    # **顺序不在这里，在 director.yaml 的 `sections` 里**（D135）。
    # 这里只负责算出那些代码才知道的内容，YAML 决定它们出现在哪、叫什么标题。
    return assemble(
        "director",
        {
            "setting": setting,
            "worldinfo": render_worldinfo(worldinfo or [], expandable=caps.worldinfo_read),
            "narration": narration,
            "forbidden": forbidden,
            "goal": goal,
            "flow": _flow(caps, worldinfo),
        },
    )


def director_turn_prompt(
    control: ControlDimensions,
    state: WorldState,
    user_text: str | None,
    *,
    capabilities: Capabilities | None = None,
) -> str:
    """**每一轮都会变的那部分**，放在这一轮的用户消息里。

    state 必须在这里而不是 system prompt 里：持续对话中 system 是缓存前缀，
    放在那儿的状态会过期，而且会以"写在最前面"的权威姿态误导模型。
    放在最新一轮，则它天然是最新的一份。
    """
    caps = capabilities or Capabilities()
    handling = choice("director", "input_handling", control.input_handling.value)
    # **事件密度是「铺陈密度」的反向投影**：director 不该知道篇幅（D93），
    # 所以同一个控制维度在两侧读的是两组不同的措辞。
    density = choice("director", "event_density", control.expansion_density.value)
    authority = choice("director", "authority", control.model_authority.value)
    directives = "\n".join(
        [
            f"- 输入处理：{handling}",
            f"- 事件密度：{density}",
            f"- 剧情控制权：{authority}",
            "- " + _speaking_rule(control.allow_speaking_for_user),
        ]
    )
    rendered_state = render_state(state, expandable=caps.state_query) if caps.state else ""
    parts = [
        _section("当前状态", rendered_state),
        _section("本轮要求", directives),
        _section("用户输入", user_text or "（没有新的用户输入，按当前设定继续推进。）"),
    ]
    return "\n\n".join(p for p in parts if p)


def _has_lookup_tools(caps: Capabilities, worldinfo: list[WorldbookEntry] | None) -> bool:
    return (
        caps.state
        or caps.history_search
        or any(e.mode is WorldbookMode.ON_DEMAND for e in worldinfo or [])
    )


def writer_system_prompt(meta: SessionMeta, control: ControlDimensions) -> str:
    """writer 的上下文：**只有文风相关的东西 + 否定项**。

    它拿不到 state 的值、拿不到世界设定、拿不到用户原始指令，那些都已经在细纲里了。
    它的职责是改写，不是补充。
    """
    voices = "\n".join(f"- {name}：{how}" for name, how in meta.character_voices.items())
    forbidden = "\n".join(f"- {item}" for item in meta.forbidden)

    # **顺序不在这里，在 writer.yaml 的 `sections` 里**（D135）。
    return assemble(
        "writer",
        {
            "prose_style": meta.prose_style,
            "character_voices": voices,
            "pov": meta.pov,
            "speaking_rule": _writer_speaking_rule(control.allow_speaking_for_user),
            "forbidden": forbidden,
            "density": choice("writer", "density", control.expansion_density.value),
        },
    )
