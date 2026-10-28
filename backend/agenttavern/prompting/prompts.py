"""谁收到什么。

**哪些 context 给谁，是整个拓扑的地基**（S3 / Generation.md §3）。完成后的剧情只
以正文为准：director 与 writer 都读正文历史，但只有 writer 读文风、角色语言习惯
和本轮细纲。

因此这里必须守住：

- **文风与角色语言习惯只进 writer。** 它们不参与检索，参与每一个 token。
- **正文进入所有需要剧情历史的 agent。** 细纲只在当前 run 内交给 writer，不进入
  后续 turn 的任何模型上下文。

**否定项两侧都不注入**（S91）：这个字段整个不存在。
不能提前揭示的东西属于世界设定，写进 worldinfo 或 state 即可；
「不许写什么」这类写作纪律属于提示词，写进 YAML 即可。

**这里不再回答"选哪一档"**（R31）：控制维度的当前取值住在各 agent 的 YAML 里
（`choice` 条目上的 `selected`），一处定义、一处取值、一处修改。所以这个模块
现在只回答一个问题：**每段 context 注入什么。**
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from ..config import LIMITS
from ..disclosure import FoldText, fold
from ..llm import Message
from ..models import (
    EntryKind,
    Goal,
    Node,
    WorldbookEntry,
    WorldbookMode,
    WorldState,
)
from ..models.state import Tree
from .assembler import (
    Prompt,
    assemble,
    choice_text,
    param,
    rendering,
    special_text,
    tool_enabled,
)
from .assembler import enabled as agent_enabled


def player_character(worldinfo: Sequence[WorldbookEntry] = ()) -> str:
    """用户扮演的是谁：**标了 `is_player` 的那条角色条目**（S67/S65）。

    此前另有一个 `user_character` 字段，于是「用户扮演谁」和「这个人是什么样」
    分在两个地方——改了一处另一处不动，而 writer 两样都要。那个字段已经删了
    （S65）：条目自己就带着正文，名字从它来，没有第二个入口可对不上。
    """
    for entry in worldinfo:
        if entry.kind is EntryKind.CHARACTER and entry.is_player:
            return entry.name
    return ""


def render_voices(worldinfo: Sequence[WorldbookEntry] = ()) -> str:
    """角色语言习惯。**只进 writer**——它不参与检索，参与每一个 token（S3）。

    取的是角色条目自己的 `voice`（S65）。此前另有一个 `名字 → 说话方式` 的字典，
    于是「这个角色是谁」和「他怎么说话」分在两个地方、靠名字对上——改个名字就
    悄悄断了，而断了不报错，只是那个角色忽然不再那样说话。那个字段已经删了（S65）。
    """
    return "\n".join(
        f"- {e.name}：{e.voice}" for e in worldinfo if e.kind is EntryKind.CHARACTER and e.voice
    )


def upstream_runs() -> int:
    """给 writer 几轮**正文**上文。**参数住在 `upstream` 那条 context 上**（S80）。

    它只用来接住语气和现场；director 的长程历史同样以正文为准。
    """
    return param("writer", "upstream", "runs", LIMITS.windows.upstream_runs)


def state_recent_runs() -> int:
    """给 state_manager 几轮正文上文（S7）。

    **这是 state_manager 最大的一笔开销**：滑动窗口，每轮全变，一点缓存前缀都
    吃不到，而前几轮的正文是其中的大头。往下调省钱，代价是键名和值的形状更容易
    漂移。**和 `upstream_runs` 分开**：writer 少一轮只是接得生硬些，
    state_manager 少一轮会减少它核对连续变化的依据。
    """
    return param("state_manager", "recent", "runs", LIMITS.windows.state_recent_runs)


def _fold_text(agent: str, kind: str) -> FoldText:
    """折起来的地方摆哪句话。**从 YAML 取，一个字都不在代码里拼**（R31）。

    `kind` 是 `state` 或 `worldinfo`，对应 `rendering` 里那两组键。
    **按 agent 各存一份**：director 和 state_manager 都会读到状态那一节，
    两份 YAML 的 `rendering` 里各有一份。这不是"一件事两个入口"——它们是发给
    两个不同 agent 的两条不同消息，本来就该能分别改。
    """
    if kind == "worldinfo":
        return FoldText(leaf=lambda chars: rendering(agent, "worldinfo_on_demand", chars=chars))
    return FoldText(deeper=lambda count: rendering(agent, "state_deeper", count=count))


def render_state(state: WorldState, *, agent: str, expandable: bool = True) -> str:
    """状态默认包：**JSON**，全量外层路径 + 叶端两层对象折起来。

    ## 为什么是 JSON

    此前是「键：值」的文本，清单渲染成 `清单：青锋剑、止血符 ×2`。加那个「清单：」
    前缀是 S32 的修法——不加的话清单和字符串长得一模一样，模型照着写回就把清单
    拍平了。**但实测（`data` 场景第 5 轮）模型把「清单：」当成了值的开头抄了回来**，
    写回一个以「清单：」四个字打头的字符串，形状照样塌，还多了四个字的垃圾。
    形状标记只要落在值这一侧就是可抄的。

    JSON 从根上消掉这件事：`[...]` 和 `"..."`、`3` 和 `"3"`、`{}` 和 `""`
    自带区分，不需要任何额外标记，而且**和 `state_write` 的参数是同一套写法**——
    模型读到什么形状就写回什么形状，中间没有一次翻译。

    **层次靠嵌套，不靠点分键名**（S31）：从叶端起最后两层作为折叠单元，外层路径
    全部保留，模型按需 `state_query` 展开。不按字符串字数折叠。

    **不预设任何维度。** 键由模型自由命名，时间、地点、角色属性的粒度与形态都
    取决于故事，定不了固定 schema（见 models/state.py）。

    **这里只渲染键值，一句提示词都不带**——那些话是提示词，`agent` 决定从哪份
    YAML 的 `rendering` 里取（R31）。
    """
    if not state.values:
        return rendering(agent, "state_empty")

    values: Tree = dict(state.values)
    if expandable:
        values = fold(
            values,
            tail_depth=2,
            texts=_fold_text(agent, "state"),
        )
    return json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True)


def render_worldinfo(entries: list[WorldbookEntry], *, expandable: bool = True) -> str:
    """常驻设定全量注入；按需展开的**只给名字和体量**（S53/S157）。

    索引**覆盖全部按需条目**，不做任何预筛选（P13），筛选子集式的规则
    （如 lorebook 的关键词触发）不合格：漏命中时条目完全缺失，
    而模型根本不知道它存在。

    **条目是 `名字 → 正文`**：名字既是提要也是地址，和状态那棵树一样
    （S157「有名字的用名字」）。索引里因此不再有 id、也不再有正文开头——
    那个开头曾经顶着「摘要」的名字，而它其实是位置偏置最重的一种预览
    （开头原样保留、结尾完全消失）。
    """
    # 初始状态和开场白都属于 worldbook，但不是 worldinfo：前者走状态树，后者在
    # session 创建时冻结成历史起点。先收窄，避免开场正文又在设定区重复注入（S168）。
    entries = [entry for entry in entries if entry.is_lore]

    # **关闭按需展开时，所有条目一律全文注入**——没有展开的路还只给索引，
    # 就成了模型看得见名字却永远拿不到内容的有损筛选（P13）。
    # 设定跨轮不变，全量注入的代价只付一次（它在缓存前缀里，S60）。
    if not expandable:
        return "\n\n".join(f"### {e.name}\n{e.content}" for e in entries if e.content)

    always = [e for e in entries if e.mode is WorldbookMode.ALWAYS]
    on_demand = [e for e in entries if e.mode is WorldbookMode.ON_DEMAND]

    parts = [f"### {e.name}\n{e.content}" for e in always if e.content]
    if on_demand:
        # **按需条目一律折起来**，不看它有多长。
        folded = fold(
            {e.name: e.content for e in on_demand},
            tail_depth=0,
            texts=_fold_text("director", "worldinfo"),
            fold_text_leaves=True,
        )
        index = "\n".join(f"- {name}{mark}" for name, mark in folded.items())
        # 「用 worldinfo_read 按名字展开。」是一句**明确的操作指令**，所以它也归
        # YAML（R31）——改了工具名却忘了改它，模型会去调一个不存在的工具。
        parts.append(f"### 可展开的条目\n{index}\n\n{rendering('director', 'worldinfo_expand')}")
    return "\n\n".join(parts)


def director_prompt(
    state: WorldState,
    user_text: str | None,
    *,
    history: Sequence[Message] = (),
    worldinfo: list[WorldbookEntry] | None = None,
    goal: Goal | None = None,
) -> Prompt:
    """装配 director 收到的整串消息。

    **顺序、role、选哪一档都不在这里，在 director.yaml 里**（S75–S9/R31）。
    这里只回答一个问题：**每段 context 注入什么。**

    但有一条约束是这里保证不了的、也不该由这里保证：**前情之前的部分跨轮不变**
    （S60）。对话跨轮持续，那一段就是缓存前缀，凡是每轮会变的东西排在它前面，
    缓存就永远打不中，而且旧值会以"写在最前面的权威信息"的姿态留着。
    所以 state 与本轮要求都排在 `history` 之后——**这是 YAML 里的顺序决定的**，
    改动它会付出缓存代价。
    """
    return assemble(
        "director",
        contexts={
            # **题材与基调、剧情尺度不在这里了**（S64）：它们是 `WORLD` 条目，
            # 跟着 `worldinfo` 这一整块进来。`WORLD` 的定义就是「一段关于这个
            # 世界的字」，而它们正是那个——凭什么要特殊处理。
            "worldinfo": render_worldinfo(
                worldinfo or [], expandable=tool_enabled("director", "worldinfo_read")
            ),
            "history": history,
            # **没有 state_manager 就不给状态那一节**（S80）：没人写它，
            # 给出去的永远是空的，白占体量还让模型以为这里本该有内容。
            "state": render_state(
                state,
                agent="director",
                expandable=tool_enabled("director", "state_query"),
            )
            if agent_enabled("state_manager")
            else "",
            # 没有新输入时顶上的那句话是**提示词**，所以在 YAML 的 specials 里。
            "user_input": user_text or special_text("director", "no_input"),
        },
        # **`{{goal}}` 现在没人用**：`director.yaml` 里一个引用都没有，因为
        # director 不该读目标（S102——模拟输入和真输入在它眼里必须一模一样，
        # 而知道目标会让它对前者区别对待）。值留着是**一个可选的接口**：
        # 想让它看见，在 YAML 里写一句 `{{goal}}` 即可；不写就什么也不发生。
        # 不留的话那一句会直接报 KeyError，而那是个坏得多的发现方式。
        values={"goal": goal.description if goal else ""},
    )


def render_recent(nodes: Sequence[Node]) -> str:
    """前几轮正文。剧情事实只从最终落成的正文读取。"""
    if not nodes:
        return rendering("state_manager", "recent_empty")

    blocks: list[str] = []
    for offset, node in enumerate(nodes):
        ago = len(nodes) - offset
        label = "上一轮" if ago == 1 else f"上 {ago} 轮"
        blocks.append(f"## {label}\n\n{node.prose}")
    return "\n\n".join(blocks)


def state_manager_prompt(
    state: WorldState,
    *,
    recent: Sequence[Node] = (),
    prose: str = "",
) -> Prompt:
    """装配 state_manager 收到的整串消息（S7）。

    它拿不到世界设定、拿不到目标、拿不到文风——那些决定"该写什么"，
    而它不写任何内容。它只回答"这一轮之后，速查表该改哪几个键"。

    **注入的是 `base_state`，本 run 开始时的状态。** 它自己 loop 中途写下的增量
    由 `state_query` 反映（那个走 `current_state`），不必也不该重新装配一次。
    """
    return assemble(
        "state_manager",
        contexts={
            "state": render_state(
                state,
                agent="state_manager",
                expandable=tool_enabled("state_manager", "state_query"),
            ),
            "recent": render_recent(recent),
            "prose": prose,
        },
    )


def writer_prompt(
    *,
    outline: str = "",
    upstream: str = "",
    worldinfo: Sequence[WorldbookEntry] = (),
) -> Prompt:
    """装配 writer 收到的整串消息：**只有文风相关的东西 + 上文 + 细纲**。

    它拿不到 state 的值、拿不到世界设定、拿不到用户原始指令，那些都已经在细纲里了。
    它的职责是改写，不是补充。

    **文风、视角、铺陈程度都在 `writer.yaml` 里选**（R31）：一档就是一段可编辑
    的提示词，加一档只是加一个二级标题；当前选哪一档写在那个条目的 `selected` 上。
    """
    return assemble(
        "writer",
        contexts={
            "character_voices": render_voices(worldinfo),
            "upstream": upstream,
            # 用户角色代言边界是服务级选择：director 决定剧情时与 writer 落成
            # 正文时必须读到同一段正文，不能让两份 choice 独立漂移（S91）。
            "speaking_rule": choice_text("director", "speaking_rule"),
            "outline": outline,
        },
        # 用户扮演谁是**这个世界的设定**（S76 那条判据：换个人来读、换局故事
        # 来读都仍然成立），所以它跟着 worldbook 走（S63）——具体说，跟着那条
        # 标了 `is_player` 的角色条目走；第几人称才是读者偏好，在 writer.yaml。
        values={"user_character": player_character(worldinfo)},
    )
