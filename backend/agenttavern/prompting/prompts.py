"""谁收到什么。

**哪些 context 给谁，是整个拓扑的地基**（D33 / Generation.md §3）。切法是：
director 只读细纲历史、不读正文；writer 读正文上文以保证衔接。这一刀切开了最大的
共享项，两侧才真正可分。

因此这里必须守住：

- **文风与角色语言习惯只进 writer。** 它们不参与检索，参与每一个 token。
- **正文只进 writer 和 state_manager。** director 能覆盖五倍历史跨度正是因为它
  自己从不读正文（D54）；state_manager 读，但它把正文折成几个键值，不往回带原文。

D35（否定项两边都要）**已删除**（D139）：否定项这个字段整个不存在了。
不能提前揭示的东西属于世界设定，写进 worldinfo 或 state 即可；
「不许写什么」这类写作纪律属于提示词，写进 YAML 即可。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..config import LIMITS
from ..llm import Message
from ..models import (
    Capabilities,
    ControlDimensions,
    Node,
    SessionMeta,
    StateDelta,
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


STATE_INLINE_LIMIT = LIMITS.context.state_inline_limit
"""单个状态值超过这个长度就只给体量、不给内容，由模型按需 state_query 展开。"""


HIDDEN = "（{chars} 字，用 state_query 展开）"
"""长字符串的占位。**它自己是字符串，所以只能顶替字符串值**——见 `_hide_long`。"""

DEEPER = "（{count} 项，用 state_query 展开）"
"""超过展开层级的子树的占位（D157）。"""


def _hide_long(value: object) -> object:
    """体量大的值只给体量，由模型按需 `state_query` 展开。

    **只对字符串生效。** 清单即使很长也原样给出：顶替掉它会把形状一起顶替成
    一个字符串，而形状正是这里最要紧的东西（D96）——模型照着写回，清单就塌了。
    长清单该做的是拆一层嵌套，不是藏起来。
    """
    if isinstance(value, str) and len(value) > STATE_INLINE_LIMIT:
        return HIDDEN.format(chars=len(value))
    return value


def _fold(tree: dict[str, object], depth: int) -> dict[str, object]:
    """按**层级**折叠（D157）：深过 ``depth`` 的子树只给条数。

    这是点分键名换成真嵌套之后才做得成的事——平铺结构里没有"层"，
    当时只能按字符数藏，而字符数和"这一块要不要给"没有关系。

    ``depth<=0`` 表示不折叠。折叠只发生在**字典**上：清单是一个值，
    折它等于改形状。
    """
    out: dict[str, object] = {}
    for key, value in tree.items():
        if isinstance(value, dict) and value:
            if depth == 1:
                out[key] = DEEPER.format(count=len(value))
            else:
                out[key] = _fold(value, depth - 1)
        else:
            out[key] = _hide_long(value)
    return out


def render_state(state: WorldState, *, expandable: bool = True, depth: int = 0) -> str:
    """状态默认包：**JSON**，全量键名 + 体量大的字符串只给体量。

    ## 为什么是 JSON

    此前是「键：值」的文本，清单渲染成 `清单：青锋剑、止血符 ×2`。加那个「清单：」
    前缀是 D96 的修法——不加的话清单和字符串长得一模一样，模型照着写回就把清单
    拍平了。**但实测（`data` 场景第 5 轮）模型把「清单：」当成了值的开头抄了回来**，
    写回一个以「清单：」四个字打头的字符串，形状照样塌，还多了四个字的垃圾。
    形状标记只要落在值这一侧就是可抄的。

    JSON 从根上消掉这件事：`[...]` 和 `"..."`、`3` 和 `"3"`、`{}` 和 `""`
    自带区分，不需要任何额外标记，而且**和 `state_write` 的参数是同一套写法**——
    模型读到什么形状就写回什么形状，中间没有一次翻译。

    **层次靠嵌套，不靠点分键名**（D157）：`depth` 决定给到第几层，更深的只给条数，
    模型按需 `state_query` 展开。`depth<=0` 表示全给。

    **不预设任何维度。** 键由模型自由命名，时间、地点、角色属性的粒度与形态都
    取决于故事，定不了固定 schema（见 models/state.py）。

    **这里只渲染键值，一句提示词都不带**——那些话是提示词，写在 YAML 里。
    """
    if not state.values:
        return "（尚无记录）"

    values: dict[str, object] = dict(state.values)
    if expandable:
        values = _fold(values, depth)
    return json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True)


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
            # 题材与基调是这个故事自己写的话——**数据，不是模板**，所以走 context。
            "genre_tone": meta.genre_tone,
            "worldinfo": render_worldinfo(worldinfo or [], expandable=caps.worldinfo_read),
            "history": history,
            "state": render_state(state, expandable=caps.state_query) if caps.state else "",
            "user_input": user_text or text("director", "no_user_input"),
        },
        values={
            "content_scale": meta.content_scale,
            "goal": meta.goal.description if meta.goal else "",
        },
        options={
            "narration_and_advancement": control.input_handling.value,
            "input_handling": control.input_handling.value,
            # **事件密度是「铺陈密度」的反向投影**：director 不该知道篇幅（D93），
            # 所以同一个控制维度在两侧读的是两组不同的措辞。
            "event_density": control.expansion_density.value,
            # 剧情控制权不在这里了——它讲的是 `ask_question` 该多积极地调用，
            # 所以写进了那个工具的描述（D140）。
            "speaking_rule": _speaking(control.allow_speaking_for_user),
            # 流程那四条已经删掉（D143）——每一条都有工具描述覆盖它，
            # 于是 `_on_off` / `_has_lookup_tools` 两个 helper 也一并没了。
        },
    )


def render_delta(delta: StateDelta) -> str:
    """一次状态更新。给 state_manager 看它前几轮**自己写过什么**。

    **原样给 `state_write` 的参数 JSON，不隐藏也不改写。** 这一节的全部用处就是
    做示范，而示范必须和它要模仿的那次调用逐字一致——换个形式重述一遍，模仿的
    就是那个形式了。
    """
    if delta.is_empty():
        return "（本轮无变化）"

    return json.dumps(delta.model_dump(exclude_defaults=True), ensure_ascii=False, indent=2)


def render_recent(nodes: Sequence[Node]) -> str:
    """前几轮的「细纲 → 成文 → 当时记下的」三元组。

    **三样缺一不可。** 少了细纲看不出"定下的事实"和"记下的键"之间的取舍，
    少了正文看不出细纲留白处最后落成了什么，少了增量就根本没有示范。
    """
    if not nodes:
        return "（这是第一轮，前面没有可参照的记录。）"

    blocks: list[str] = []
    for offset, node in enumerate(nodes):
        ago = len(nodes) - offset
        label = "上一轮" if ago == 1 else f"上 {ago} 轮"
        blocks.append(
            f"## {label}\n\n### 细纲\n{node.outline}\n\n### 成文\n{node.prose}"
            f"\n\n### 当时记下的\n{render_delta(node.state_delta)}"
        )
    return "\n\n".join(blocks)


def state_manager_prompt(
    state: WorldState,
    *,
    recent: Sequence[Node] = (),
    outline: str = "",
    prose: str = "",
    capabilities: Capabilities | None = None,
) -> Prompt:
    """装配 state_manager 收到的整串消息（D143）。

    它拿不到世界设定、拿不到目标、拿不到控制维度——那些决定"该写什么"，
    而它不写任何内容。它只回答"这一轮之后，速查表该改哪几个键"。

    **注入的是 `base_state`，本 run 开始时的状态。** 它自己 loop 中途写下的增量
    由 `state_query` 反映（那个走 `current_state`），不必也不该重新装配一次。
    """
    caps = capabilities or Capabilities()
    return assemble(
        "state_manager",
        contexts={
            "state": render_state(state, expandable=caps.state_query),
            "recent": render_recent(recent),
            "outline": outline,
            "prose": prose,
        },
    )


def writer_prompt(
    meta: SessionMeta,
    control: ControlDimensions,
    *,
    outline: str = "",
    upstream: str = "",
) -> Prompt:
    """装配 writer 收到的整串消息：**只有文风相关的东西 + 上文 + 细纲**。

    它拿不到 state 的值、拿不到世界设定、拿不到用户原始指令，那些都已经在细纲里了。
    它的职责是改写，不是补充。
    """
    return assemble(
        "writer",
        contexts={
            "character_voices": "\n".join(
                f"- {name}：{how}" for name, how in meta.character_voices.items()
            ),
            "upstream": upstream,
            "outline": outline,
        },
        # 用户扮演谁是**这个故事的设定**（D138 那条判据：换个人来读仍然成立），
        # 所以它在 meta；第几人称是读者偏好，所以在 control，且已改成选项组。
        values={"user_character": meta.user_character},
        # **文风与视角都是选项名，不是自由文本**：档位全文写在 writer.yaml 里，
        # 这里只说选哪一档。它们和铺陈程度一样来自 `control`——都是读者这一侧的
        # 偏好，不是这个世界的属性。
        options={
            "prose_style": control.prose_style,
            "pov": control.pov,
            "density": control.expansion_density.value,
        },
    )
