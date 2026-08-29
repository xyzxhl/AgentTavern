"""装配器本身：把 YAML 读成一串消息。

**这里不认识 director 和 writer**——它只认识三类条目、`{{var}}` 的替换规则、
以及"空条目整条跳过"。谁收到什么在 `prompts.py`。

## 为什么提示词正文在 YAML 里

提示词是这个项目**改得最频繁**的东西，而它此前是一堆 Python 字符串常量：
改一句话要动源码、要过 ruff/format、大段替换还容易切坏文件（真发生过一次，
203 个测试全红）。搬进 YAML 之后，改提示词就只是改文本。

## 一个条目 = 一条消息，**role 必须写出来**

装配出来的不是一整块文本，是**一串消息**。`sections` 里的每个条目就是其中一条，
它的 `role` 写在条目上。这样一来「破限放在历史之后」「上文用 user 还是 assistant」
这类决定都在 YAML 里，不在代码里。

## 只有三类条目（R31）

| 类型 | 用户能改什么 | 内容从哪来 |
|---|---|---|
| ``text``    | 正文 | 用户写的字，可含 ``{{var}}`` 单值占位 |
| ``context`` | **只有位置** | 代码整段生成——世界设定、当前状态、前情、细纲、上文 |
| ``choice``  | 每一档的正文 **+ 当前选哪一档** | 用户写的字 |

**类型是显式字段**（`type: text`），不再靠"出现了哪个键"去猜——拖动排序的界面
要先读出这一条是什么类型才画得出来。

`{{var}}` 与 `context` 的分界是**体量与位置**：前者是嵌在句子里的单值
（`用户扮演：{{user_character}}`），后者是独占一条消息的一大块。两个都要。

## 装配器做减法（R31）

判据是**所见即所得**：编辑器里显示的那段文字，必须就是模型收到的那段文字。
装配器每替用户多做一件事，两者就差一点。所以下面三样**已经删掉**：

- ``title`` 自动包 ``<标题>…</标题>`` —— 要标签就自己写进 ``body``。
- ``intro`` / ``outro`` —— 在它前面 / 后面放一条 ``text``，有拖动排序之后这更直接。
- ``key`` 引用顶层命名块 —— 它的理由是「长文本留在原处更好编辑」，
  **有了编辑界面这条就不成立了**；正文一律内联进 section。

**取消自动标签有一个要盯的风险**：每个 section 本来就是一条独立消息，消息边界
即是边界，所以标签在理论上是冗余的——但**部分供应商会把连续的同 role 消息合并**，
合并之后边界就没了。真出问题时的补法是**让用户自己在 `body` 里写标签**（那时它
可见、可编），不是把自动包裹加回来。

## 一份文件 = 一个 agent 收到的**全部**文字（S3/S76/R31）

所以除了 `sections`，一份文件还装着：

- ``tools``     它拿得到的工具，每个一条描述（描述是模型读到的字，就必须可编）。
- ``specials``  运行时**另外追加的整条消息**：追加输入、空回催促、归段追问。
- ``rendering`` 嵌在渲染里的短句：`（尚无记录）`、`用 worldinfo_read 按 ref 展开。`
  这类。量不大但每轮都进上下文，其中几条还是**明确的操作指令**——改了工具名却
  忘了改它们，模型会去调一个不存在的工具。
- ``enabled``   这个 agent 跑不跑。
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..llm import Message, Role

_DIR = Path(__file__).parent

AGENTS = ("director", "writer", "state_manager", "goal_manager")
"""**真正的 agent**：会推演、会产出剧情、有自己的上下文与工具面（S147）。"""

HELPERS = ("summarizer", "rerank")
"""**机械的辅助调用**：输入固定、输出固定、不参与剧情（S147）。

`summarizer` 一直被算作「第五个 agent」，而它和 rerank 一样只是一次工具性的
调用。并排列成 agent 会让人以为它们和 director 是同一类东西——
而那个误解会影响判断：比如以为该给它们工具面，或者以为它们该读世界设定。

**两组都可编辑**——R31 管的是「模型会读到的字」，不分是谁在读。
"""

PROMPT_FILES = (*AGENTS, *HELPERS)
"""`prompting/` 底下有哪几份 YAML。**列表在这里，不在前端**——
加一份就是加一个名字加一个文件。"""

_root: Path | None = None


def use_dir(path: Path | None) -> None:
    """让装配器改读另一份提示词目录。``None`` 恢复默认。

    **只有一个用户：bench 的消融**（S80）。取值上调到 YAML 之后，`Variant`
    原先那套「往 session 上写 capabilities / control」失去了全部着力点，
    而 P19 要求同一批素材跑多档。

    **补法是换一份 YAML，不是开一个 `overrides` 后门。** 一个"只有程序能用"的
    第二取值入口迟早会有第二个调用者，而那正是 R31 要消掉的东西——取值仍然
    只住在 YAML 里，bench 只是让装配器读了另一份 YAML。
    """
    global _root
    _root = path
    load.cache_clear()


def _dir() -> Path:
    return _root or _DIR


@functools.cache
def load(name: str) -> dict[str, Any]:
    """读一份提示词文件。**进程内只读一次**，它跨请求不变（写盘时清缓存）。

    顶层必须是映射。**「顶层直接是列表」那条特例已经删掉**——它是为片段文件
    （`director_append.yaml`）开的，而片段已经并进 `director.yaml` 的 `specials`，
    没有第二个用户了。
    """
    data = yaml.safe_load(path_of(name).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{name}.yaml 顶层必须是映射")
    return data


def path_of(name: str) -> Path:
    return _dir() / f"{name}.yaml"


def save(name: str, data: Mapping[str, Any]) -> None:
    """写回一份提示词文件。**调用方必须先试装配**（见 `server/agents.py`）。

    这里只管落盘和清缓存：`load` 有 `functools.cache`，不清的话新内容要等
    进程重启才生效——而那正是"改了没反应"这类问题里最难想到的一种。
    """
    text = yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False, width=10_000)
    path_of(name).write_text(text, encoding="utf-8")
    load.cache_clear()


def enabled(name: str) -> bool:
    """这个 agent 跑不跑。**默认跑。**

    以前是 `Capabilities.state`（关掉 state_manager 那一路）。上调之后它是
    agent 自己的属性，和"它有哪些工具""它收到哪些话"住在同一份文件里（S80）。
    """
    return bool(load(name).get("enabled", True))


_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def fill(body: str, values: Mapping[str, str]) -> str:
    """把 ``{{var}}`` 换成实际内容。

    **变量为空就删掉它所在的那一行。** 不这么做就会留下「视角：」这种
    半截的行——比没有更糟，模型会以为这里本该有内容而自己补一个。

    所以约定**一行一个变量**：一行里有两个变量而其中一个为空，整行都会消失。

    变量名没给值直接报错：那说明 YAML 和代码脱节了（同 P29）。
    """
    out: list[str] = []
    for line in body.split("\n"):
        names = _VAR.findall(line)
        if not names:
            out.append(line)
            continue
        for name in names:
            if name not in values:
                raise KeyError(f"提示词里的 `{{{{{name}}}}}` 没有对应的值")
        if any(not values[name].strip() for name in names):
            continue
        out.append(_VAR.sub(lambda m: values[m.group(1)].strip(), line))
    return "\n".join(out).strip()


Context = str | Sequence[Message]
"""一段系统控制的 context：渲染好的文本，或者**已经成形的一串消息**。

只有前情是后者——它本来就是对话本身，摘要、历史轮次各自带着自己的 role，
不可能压成一条。这是唯一一个「一个条目对应多条消息」的例外。
"""


@dataclass
class Prompt:
    """装配的产物：**一串消息**，外加每段 context 落在哪。"""

    messages: list[Message] = field(default_factory=list)
    starts: dict[str, int] = field(default_factory=dict)
    """每个 context 条目的起点下标。

    给调用方定位用——比如 director 要知道「本轮从哪一条开始」，
    才能把这一轮新增的消息单独存进节点（S60）。
    """


def _resolve(item: Mapping[str, Any], where: str) -> str:
    """一个 ``choice`` 条目当前选的那一档。**`selected` 写在条目自己身上**（R31）。

    以前当前取值住在 session 上、由代码通过 `options` 参数传进来，于是"有哪些档"
    在 YAML、"选哪一档"在别处，改一档文风要动两个地方。现在一处定义、一处取值、
    一处修改。
    """
    options = item.get("options")
    if not isinstance(options, dict):
        raise TypeError(f"{where} 的 choice 少了 `options`")
    picked = str(item.get("selected") or "")
    if picked not in options:
        raise KeyError(f"{where} 选的是 `{picked}`，而 options 里只有：{', '.join(options)}")
    return str(options[picked] or "")


def assemble(
    name: str,
    *,
    contexts: Mapping[str, Context] | None = None,
    values: Mapping[str, str] | None = None,
    sections: Sequence[Mapping[str, Any]] | None = None,
) -> Prompt:
    """按 ``sections`` 的顺序装配一串消息。

    **顺序即列表顺序，一个条目就是一条消息。** 想调位置就移动条目，
    想加一节就插一条，都只改 YAML，不动代码。

    ``sections`` 参数是给「特殊」那几块用的（追加输入、空回催促、归段追问）——
    它们不在 agent 的消息序列里，是运行时另外追加的整条消息，但形状完全一样，
    所以走同一个装配器。

    **正文为空的条目整条跳过**，连这条消息都不出现——空消息白占体量，
    还会让模型以为这里本该有内容。

    一段 context 若给的是**一串消息**（只有前情是这样），它们原样铺进去，
    各自带着自己的 role；这时条目上**不要写 role**，写了也不会生效，
    所以直接报错（同 P29：没生效的东西不该悄悄没生效）。

    名字对不上（未知的 context / 缺值的变量 / `selected` 不在 options 里 /
    没写 role）一律报错：那说明 YAML 和代码脱节了，静默跳过会让一整节悄悄消失，
    而产出看起来只是"变差了一点"。
    """
    contexts, values = contexts or {}, values or {}
    prompt = Prompt()
    items = sections if sections is not None else (load(name).get("sections") or [])

    for index, item in enumerate(items):
        where = f"{name}.yaml sections[{index}]"
        if not isinstance(item, dict):
            raise TypeError(f"{where} 必须是映射")

        kind = str(item.get("type") or "")
        body: Context
        if kind == "text":
            body = fill(str(item.get("body") or ""), values)
        elif kind == "context":
            slot = str(item.get("source") or "")
            if slot not in contexts:
                raise KeyError(f"{where} 注入了未知的 context `{slot}`")
            body = contexts[slot]
            prompt.starts[slot] = len(prompt.messages)
        elif kind == "choice":
            body = fill(_resolve(item, where), values)
        else:
            raise KeyError(
                f"{where} 的 `type` 必须是 text / context / choice 之一（现在是 {kind!r}）"
            )

        if not isinstance(body, str):
            if "role" in item:
                raise KeyError(f"{where} 的 context 是一串消息，不要给它写 `role`")
            prompt.messages.extend(body)
            continue

        body = body.strip()
        if not body:
            continue
        if "role" not in item:
            raise KeyError(f"{where} 没写 role")
        prompt.messages.append(Message(role=Role(str(item["role"])), content=body))
    return prompt


# --------------------------------------------------------------------------
# context 条目上的参数
# --------------------------------------------------------------------------


def selected(name: str, group: str) -> str:
    """某个 ``choice`` 当前选的是哪一档。**给代码里那几处要按档位分支的地方用。**

    只有一处真的需要它：偏离检查按铺陈程度定「每个事件至少几个字」的阈值
    （`orchestration/checks.py`）。那不是提示词，是一个数值判据，所以它读档位名
    而不是档位正文。找不到就返回空串，调用方自己兜底——**一个数值阈值不该因为
    用户删了一个选项组而让整轮生成失败**。
    """
    for item in load(name).get("sections") or []:
        if isinstance(item, dict) and item.get("type") == "choice" and item.get("name") == group:
            return str(item.get("selected") or "")
    return ""


def param(name: str, source: str, key: str, default: int) -> int:
    """某条 ``context`` 上的参数，比如「上文给几轮」。

    以前这些是 `Capabilities.upstream_runs` / `state_recent_runs`——**它们从来
    不是独立的配置，是某段 context 的属性**（S80），所以搬到那一条上。
    那一条被删掉时回退到默认值，不报错：整段 context 都不要了，它的参数自然
    也不必存在。
    """
    for item in load(name).get("sections") or []:
        if not isinstance(item, dict) or item.get("type") != "context":
            continue
        if item.get("source") == source:
            value = item.get(key)
            return int(value) if isinstance(value, int) else default
    return default


# --------------------------------------------------------------------------
# 工具描述（R31 / S84）
# --------------------------------------------------------------------------


def tool_enabled(name: str, tool: str) -> bool:
    """这个工具注不注册。**关掉 = 模型根本看不到它**。

    这比「注册了但拒绝调用」强：后者仍然占 prompt 体量、仍会被尝试、失败还要
    消耗一轮往返。工具面越小，模型的决策空间越小（分配原则 P9）。
    """
    spec = (load(name).get("tools") or {}).get(tool)
    return bool(spec.get("enabled", True)) if isinstance(spec, dict) else False


def tool_description(name: str, tool: str) -> str:
    """一个工具的描述。**它是模型读到的字，所以必须可审查可修改**（R31）。

    **描述可编，参数只读并排显示**（S84）。曾经判过「工具描述不搬进 YAML」，理由是「一个工具怎么
    描述自己，跟它的参数、错误信息、注册条件是同一件事，拆开写两处必然对不上」。
    **那个风险是真的**，所以参数 schema 仍由 Python 生成、在页面上与描述并排
    只读显示——漂移因此摆在人眼前，而不是藏在两个文件里。

    描述本身也可以是一个 ``choice``（`ask_question` 就是：控制权三档讲的正是
    「这个工具该多积极地调用」，S10②）——三类不必为工具再加第四类。
    """
    spec = (load(name).get("tools") or {}).get(tool)
    if not isinstance(spec, dict):
        raise KeyError(f"{name}.yaml 的 tools 里没有 `{tool}`")
    where = f"{name}.yaml tools.{tool}"
    if "options" in spec:
        return _resolve(spec, where).strip()
    return str(spec.get("body") or "").strip()


# --------------------------------------------------------------------------
# 「特殊」：运行时另外追加的整条消息
# --------------------------------------------------------------------------


def special(name: str, key: str) -> dict[str, Any]:
    spec = (load(name).get("specials") or {}).get(key)
    if not isinstance(spec, dict):
        raise KeyError(f"{name}.yaml 的 specials 里没有 `{key}`")
    return spec


def special_on(name: str, key: str) -> bool:
    """这条附属提示词发不发。

    `nudge`（空回催促）和 `segment`（归段追问）的开关以前是 `Capabilities` 上的
    两个字段，现在是它们各自那一条的属性——**它们从来就是某条提示词的开关**（S80）。
    """
    return bool(special(name, key).get("enabled", True))


def special_messages(
    name: str,
    key: str,
    *,
    contexts: Mapping[str, Context] | None = None,
    values: Mapping[str, str] | None = None,
) -> list[Message]:
    """一条附属提示词装配出来的消息。关掉时是空列表。"""
    if not special_on(name, key):
        return []
    sections = special(name, key).get("sections") or []
    return assemble(name, contexts=contexts, values=values, sections=sections).messages


def special_text(name: str, key: str, *, values: Mapping[str, str] | None = None) -> str:
    """只有一条消息的附属提示词，直接取那条的正文。关掉时是空串。"""
    messages = special_messages(name, key, values=values)
    return messages[0].content or "" if messages else ""


# --------------------------------------------------------------------------
# 渲染文案
# --------------------------------------------------------------------------


def rendering(name: str, key: str, **values: object) -> str:
    """嵌在渲染里的那些短句。**它们也是模型读到的字**（R31）。

    量不大但每轮都进上下文，其中几条是**明确的操作指令**（"用 X 展开"）——
    改了工具名却忘了改它们，模型就会去调一个不存在的工具。

    **不做成一份跨 agent 的共用表，而是分到各自的 agent 名下。** `render_state`
    那几句 director 和 state_manager 都会读到，于是两份文件里各有一份——这不是
    "一件事两个入口"：它们是发给两个不同 agent 的两条不同消息，本来就该能分别改
    （director 读 state 是为了推演，state_manager 读它是为了比对该记什么）。
    """
    table = load(name).get("rendering") or {}
    if key not in table:
        raise KeyError(f"{name}.yaml 的 rendering 里没有 `{key}`")
    body = str(table[key] or "")
    return _VAR.sub(lambda m: str(values.get(m.group(1), "")), body).strip()
