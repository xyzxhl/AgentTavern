"""提示词正文从 YAML 加载，不写在代码里。

## 为什么搬出来

提示词是这个项目**改得最频繁**的东西，而它此前是一堆 Python 字符串常量：
改一句话要动源码、要过 ruff/format、大段替换还容易切坏文件（真发生过一次，
203 个测试全红）。搬进 YAML 之后，改提示词就只是改文本。

**装配逻辑仍在 Python 里**（`agents/prompts.py`）——哪一节给谁、按什么顺序、
哪些随控制维度切换，那是拓扑的一部分（D33），不是文案。

## 分文件的判据：按接收方分，不按主题分

`writer.yaml` / `director.yaml` 各自是一个 agent 收到的全部文字。
**这样才看得出隔离有没有被破坏**——文风混进 director、正文混进哪一侧，
翻开对应文件就知道。按主题分（比如「对白」一个文件）会把两侧的内容搅在一起，
而两侧的分工正是整个拓扑的地基。
"""

from __future__ import annotations

import functools
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
    """取一组「选一」里的某一项，比如铺陈密度、控制权档位。"""
    group = load(name)[key]
    if option not in group:
        raise KeyError(f"{name}.yaml 的 `{key}` 里没有 `{option}`")
    return str(group[option]).strip()


@functools.cache
def _optional() -> dict[str, Any]:
    """`optional.yaml`。**文件不存在就当全空**，不报错。

    这与 `text()` 缺键必须报错是**故意相反**的：主文件里少一个键说明装配和
    文案脱节了，那是 bug；而这份文件本来就可能被 `.gitignore` 掉、或者部署方
    根本没建过——**"没有"就是它的正常状态之一**。
    """
    path = _DIR / "optional.yaml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def optional(side: str, key: str) -> str:
    """取一段可选提示词。缺文件、缺分组、缺键、值为空，一律返回空串。

    调用方拿到空串后整节不注入——默认插一个空标题会白占体量，
    还会让模型以为这里本该有内容。
    """
    group = _optional().get(side) or {}
    return str(group.get(key) or "").strip()
