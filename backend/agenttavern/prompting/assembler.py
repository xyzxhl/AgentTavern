"""装配器本身：把 YAML 读成一串消息。

**这里不认识 director 和 writer**——它只认识 `sections` 的四种条目、
`{{var}}` 的替换规则、以及"空条目整条跳过"。谁收到什么在 `prompts.py`。

## 为什么提示词正文在 YAML 里

提示词是这个项目**改得最频繁**的东西，而它此前是一堆 Python 字符串常量：
改一句话要动源码、要过 ruff/format、大段替换还容易切坏文件（真发生过一次，
203 个测试全红）。搬进 YAML 之后，改提示词就只是改文本。

## 一个条目 = 一条消息，**role 必须写出来**

装配出来的不是一整块文本，是**一串消息**。`sections` 里的每个条目就是其中一条，
它的 `role` 写在条目上。这样一来「破限放在历史之后」「上文用 user 还是 assistant」
这类决定都在 YAML 里，不在代码里。

## 一节内容有三种来源，YAML 里要**分别声明**

写死的文字直接写在 YAML 里；剩下三种在这里各有各的写法，**看一眼就知道
这一节的内容从哪来**：

- ``context``  **系统控制的 context**：由系统拥有并整段注入的内容——世界设定、
  当前状态、否定项、角色语言习惯、前情与摘要、正文上文、本轮细纲。
  YAML 只决定它出现在哪、叫什么标题、用什么 role。
  **注入进来的内容不当模板解析**，它是数据（用户的世界设定里完全可能出现 ``{{``）。

  **不含轮内的东西**——`tool_calls`、`role=tool` 的结果、编排层的催促、用户的插话。
  那些是 loop 边跑边追加的，位置由协议决定（工具结果必须紧跟它的调用），
  编排不了，也不该编排。

- ``{{var}}``  模板替换。题材、视角、文风这类**单值**字段，格式写在 YAML 里
  （``题材与基调：{{genre_tone}}``），代码只负责给值。
  **值为空就把那一行删掉**；一行都不剩，整节就不出现。
- ``choice``   选择控制。铺陈密度、抢话、输入处理这类由控制维度切换的内容，
  **二级标题就是选项名**，代码只负责说选哪个。
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


@functools.cache
def load(name: str) -> dict[str, Any]:
    """读一份提示词文件。**进程内只读一次**，它跨请求不变。"""
    data = yaml.safe_load((_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{name}.yaml 顶层必须是映射")
    return data


def text(name: str, key: str) -> str:
    """取一段提示词。**缺键直接报错**，不静默返回空串。

    静默返回空串会让一整节提示词悄悄消失，而产出看起来只是"变差了一点"——
    那是最难查的一类问题（同 D125：没生效的东西不会报错）。
    """
    section = load(name)
    if key not in section:
        raise KeyError(f"{name}.yaml 里没有 `{key}`")
    value = section[key]
    if isinstance(value, str):
        return value.strip()
    raise TypeError(f"{name}.yaml 的 `{key}` 不是字符串（可能是想用 choice）")


def choice(name: str, key: str, option: str) -> str:
    """取一组「选一」里的某一项。**二级标题就是选项名。**

    比如 `density.rich`、`speaking_rule.forbidden`——选项名直接来自控制维度的
    取值，所以 YAML 里加一档就是加一个二级标题，代码那侧不用改。
    """
    group = load(name)[key]
    if not isinstance(group, dict):
        raise TypeError(f"{name}.yaml 的 `{key}` 不是选项组")
    if option not in group:
        raise KeyError(f"{name}.yaml 的 `{key}` 里没有 `{option}`")
    return str(group[option] or "").strip()


_VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def fill(body: str, values: Mapping[str, str]) -> str:
    """把 ``{{var}}`` 换成实际内容。

    **变量为空就删掉它所在的那一行。** 不这么做就会留下「视角：」这种
    半截的行——比没有更糟，模型会以为这里本该有内容而自己补一个。

    所以约定**一行一个变量**：一行里有两个变量而其中一个为空，整行都会消失。

    变量名没给值直接报错：那说明 YAML 和代码脱节了（同 `text()` 的理由）。
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
    才能把这一轮新增的消息单独存进节点（D114）。
    """


def assemble(
    name: str,
    *,
    contexts: Mapping[str, Context] | None = None,
    values: Mapping[str, str] | None = None,
    options: Mapping[str, str] | None = None,
) -> Prompt:
    """按 YAML 里 ``sections`` 的顺序装配一串消息。

    **顺序即列表顺序，一个条目就是一条消息。** 想调位置就移动条目，
    想加一节就插一条，都只改 YAML，不动代码。

    每一条声明自己的 ``role``，正文是四种之一：

    - ``text``    —— 正文直接写在这一条里。**新增短提示词走这条**。
    - ``key``     —— 引用本文件顶层的一个命名块。长文本留在原处更好编辑，
      `sections` 只排顺序。两者都会做 ``{{var}}`` 替换。
    - ``context`` —— 系统控制的 context，取 ``contexts[名字]``，
      **不做模板替换**（那是数据）。
    - ``choice``  —— 顶层的一个选项组，选哪一项由 ``options[组名]`` 给。

    ``title`` 可选：给了就用 ``<标题>…</标题>`` 把正文包起来（见 `titled`）。

    ``intro`` / ``outro`` 可选：**写在 YAML 里的话，垫在正文前后**，同样做
    ``{{var}}`` 替换。它们是给 ``context`` 和 ``choice`` 用的——那两种的正文
    由代码渲染或按选项切换，YAML 那侧本来只能决定标题和位置，一个字也写不进去。
    结果就是「世界设定」「上文」「本轮细纲」这类节**光秃秃一个值**，
    没有任何一句话说明该拿它干什么；而 `render_state` 那样的说明文字只好写回
    Python 里——正是搬进 YAML 要治的病。有了这两个字段，
    **三种来源的节都能配上可编辑的提示词**。

    **正文为空的条目整条跳过**，连标题、连这条消息都不出现——空消息白占体量，
    还会让模型以为这里本该有内容。

    一段 context 若给的是**一串消息**（只有前情是这样），它们原样铺进去，
    各自带着自己的 role；这时条目上**不要写 role**，写了也不会生效，
    所以直接报错（同 D125：没生效的东西不该悄悄没生效）。

    名字对不上（未知的 context / 缺值的变量 / 没给选项的组 / 没写 role）一律报错：
    那说明 YAML 和代码脱节了，静默跳过会让一整节悄悄消失，
    而产出看起来只是"变差了一点"（同 D125）。
    """
    contexts, values, options = contexts or {}, values or {}, options or {}
    prompt = Prompt()

    for index, item in enumerate(load(name).get("sections") or []):
        where = f"{name}.yaml sections[{index}]"
        if not isinstance(item, dict):
            raise TypeError(f"{where} 必须是映射")

        if "text" in item:
            body: Context = fill(str(item["text"] or ""), values)
        elif "key" in item:
            body = fill(text(name, str(item["key"])), values)
        elif "context" in item:
            slot = str(item["context"])
            if slot not in contexts:
                raise KeyError(f"{where} 注入了未知的 context `{slot}`")
            body = contexts[slot]
            prompt.starts[slot] = len(prompt.messages)
        elif "choice" in item:
            group = str(item["choice"])
            if group not in options:
                raise KeyError(f"{where} 的选项组 `{group}` 没人给选项")
            body = choice(name, group, options[group])
        else:
            raise KeyError(f"{where} 需要 text / key / context / choice 之一")

        if not isinstance(body, str):
            for extra in ("role", "intro", "outro"):
                if extra in item:
                    raise KeyError(f"{where} 的 context 是一串消息，不要给它写 `{extra}`")
            prompt.messages.extend(body)
            continue

        body = body.strip()
        if not body:
            continue
        if "role" not in item:
            raise KeyError(f"{where} 没写 role")

        intro = fill(str(item.get("intro") or ""), values)
        outro = fill(str(item.get("outro") or ""), values)
        body = "\n\n".join(part for part in (intro, body, outro) if part)

        title = item.get("title")
        prompt.messages.append(
            Message(
                role=Role(str(item["role"])), content=titled(str(title), body) if title else body
            )
        )
    return prompt


def titled(title: str, body: str) -> str:
    """给一节加标题：**标签包裹，不是 Markdown 标题。**

    以前是 ``## 标题``。问题是提示词正文里本来就有 ``# 核心原则``、``# 对白设计``
    这样的小标题，两者混在同一套记号里——一节的边界和它内部的分节长得一样，
    模型只能靠井号数量去猜哪一级是哪一级。标签有闭合，边界是明确的。
    """
    return f"<{title}>\n{body}\n</{title}>"
