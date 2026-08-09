"""提示词正文从 YAML 加载，不写在代码里。

## 为什么搬出来

提示词是这个项目**改得最频繁**的东西，而它此前是一堆 Python 字符串常量：
改一句话要动源码、要过 ruff/format、大段替换还容易切坏文件（真发生过一次，
203 个测试全红）。搬进 YAML 之后，改提示词就只是改文本。

**只有拓扑留在 Python 里**（`agents/prompts.py`）——哪一节给谁是 D33 的地基，
不是文案。顺序、标题、措辞、有没有这一节，全在 YAML。

## 分文件的判据：按接收方分，不按主题分

`writer.yaml` / `director.yaml` 各自是一个 agent 收到的全部文字。
**这样才看得出隔离有没有被破坏**——文风混进 director、正文混进哪一侧，
翻开对应文件就知道。按主题分（比如「对白」一个文件）会把两侧的内容搅在一起，
而两侧的分工正是整个拓扑的地基。

## 一节内容有三种来源，YAML 里要**分别声明**

写死的文字直接写在 YAML 里；剩下三种在这里各有各的写法，**看一眼就知道
这一节的内容从哪来**：

- ``inject``  整段注入。世界设定、否定项、角色语言习惯、流程——由代码渲染成
  一整块，YAML 只决定它出现在哪、叫什么标题。**注入进来的内容不当模板解析**，
  它是数据（用户的世界设定里完全可能出现 ``{{``）。
- ``{{var}}``  模板替换。题材、视角、文风这类**单值**字段，格式写在 YAML 里
  （``题材与基调：{{genre_tone}}``），代码只负责给值。
  **值为空就把那一行删掉**；一行都不剩，整节就不出现。
- ``choice``   选择控制。铺陈密度、抢话、输入处理这类由控制维度切换的内容，
  **二级标题就是选项名**，代码只负责说选哪个。
"""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

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


def assemble(
    name: str,
    *,
    blocks: Mapping[str, str] | None = None,
    values: Mapping[str, str] | None = None,
    options: Mapping[str, str] | None = None,
) -> str:
    """按 YAML 里 ``sections`` 的顺序装配一份提示词。

    **顺序即列表顺序。** 想调位置就移动条目，想加一节就插一条，
    都只改 YAML，不动代码。

    每一条是四种之一：

    - ``text``   —— 正文直接写在这一条里。**新增短提示词走这条**。
    - ``key``    —— 引用本文件顶层的一个命名块。长文本留在原处更好编辑，
      `sections` 只排顺序。两者都会做 ``{{var}}`` 替换。
    - ``inject`` —— 整段注入 ``blocks[名字]``，**不做模板替换**（那是数据）。
    - ``choice`` —— 顶层的一个选项组，选哪一项由 ``options[组名]`` 给。

    ``title`` 可选：给了就渲染成 ``## 标题``，不给就直接拼正文。

    **正文为空的条目整条跳过**，连标题都不出现——空标题白占体量，
    还会让模型以为这里本该有内容。

    名字对不上（未知的 inject / 缺值的变量 / 没给选项的组）一律报错：
    那说明 YAML 和代码脱节了，静默跳过会让一整节悄悄消失，
    而产出看起来只是"变差了一点"（同 D125）。
    """
    blocks, values, options = blocks or {}, values or {}, options or {}
    out: list[str] = []
    for index, item in enumerate(load(name).get("sections") or []):
        if not isinstance(item, dict):
            raise TypeError(f"{name}.yaml sections[{index}] 必须是映射")

        if "text" in item:
            body = fill(str(item["text"] or ""), values)
        elif "key" in item:
            body = fill(text(name, str(item["key"])), values)
        elif "inject" in item:
            slot = str(item["inject"])
            if slot not in blocks:
                raise KeyError(f"{name}.yaml sections[{index}] 注入了未知的 `{slot}`")
            body = blocks[slot].strip()
        elif "choice" in item:
            group = str(item["choice"])
            if group not in options:
                raise KeyError(f"{name}.yaml sections[{index}] 的选项组 `{group}` 没人给选项")
            body = choice(name, group, options[group])
        else:
            raise KeyError(f"{name}.yaml sections[{index}] 需要 text / key / inject / choice 之一")

        if not body:
            continue
        title = item.get("title")
        out.append(f"## {title}\n{body}" if title else body)
    return "\n\n".join(out)
