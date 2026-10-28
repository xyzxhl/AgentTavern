"""渐进式披露：**折叠与展开是同一件事的两半**（S157）。

三个域——世界设定、前情、状态——各有一棵 `title → content` 的树。上下文里给的是
**折起来的那份**（key + 体量），要看正文就按 key 展开。这个模块装的是两半共用的
那部分：折叠成什么样、按 key 取回来、取不到时说什么。

## 为什么必须共用

折叠时留下的占位符，和展开工具接受的地址，**必须是同一套 key**。它们以前是三处
各写一遍：

| | 地址 | 展开 | 长值 |
|---|---|---|---|
| state | 键路径 | `state_query` | 「N 字，用 state_query 展开」 |
| worldinfo | 条目 id | `worldinfo_read(refs, start, max_chars)` | **字符分页** |
| history | run id | `history_read(run_ids)` | 头部截断 |

三套寻址、三套截断话术、三份"没找到"的写法。对不上的时候**没有任何地方会报错**
——模型照着占位符里的名字去调工具，工具说"没有这个"，而那一段就此取不回来了。

## key 是人读得懂的名字

state 的键名是模型自己起的，worldinfo 的条目名是用户起的，两者都既是地址也是提要。
**只有段落例外**：它没有名字，只有一段摘要，而摘要当不了地址（太长、会重、传回来
会被改写一两个字）。所以段落用 id，**id 旁边永远跟着那段摘要**——那是它的提要。

> 有名字的用名字，没名字的用 id，而 id 旁边跟着提要。

## 不分页、不按体量截断

展开就是**全给**。以前每个工具各有一套预算和分页（`start` / `max_chars` /
「没给全」），那是在这一层猜"多少算多"。真的超了上下文是**整轮失败**，
不是这一层能补救的事——而一个猜出来的预算会让模型在不知情的情况下少看几段。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models.state import JsonValue, Path, Tree, label_path, walk

__all__ = [
    "Expanded",
    "FoldText",
    "expand",
    "fold",
    "keys_of",
]


@dataclass(frozen=True)
class FoldText:
    """折起来的地方摆哪句话。**它们是模型读到的字**（R31）。

    所以这个模块不自己拼话：调用方从 YAML 的 `rendering` 里取好了传进来。
    改了工具名却忘了改这句话，模型会去调一个不存在的工具——那句话和工具名
    是同一件事的两处，而这里只负责放它。
    """

    leaf: Callable[[int], str] | None = None
    """一个明确要求折起来的文本叶子：参数是它有多少字。"""

    deeper: Callable[[int], str] | None = None
    """一棵折起来的子树：参数是它底下有几项。

    **平的树可以不给**（worldinfo 就是 `条目名 → 正文`，一层到底）：给一句它永远
    用不上的话，等于在用户的 YAML 里放一条死字符串。真遇到嵌套时下面当场报错，
    那是编程错误，不是模型能读到的东西。
    """


def fold(tree: Tree, *, tail_depth: int, texts: FoldText, fold_text_leaves: bool = False) -> Tree:
    """按**离叶端还有几层**折叠，不按根部深度或字符数折叠。

    ``tail_depth=2`` 表示把「最后一个对象键 + 它下面的叶子键值」作为一个折叠单元；
    外层路径全都保留。不同深度的分支因此各自在叶端收口，而不是一律在根下第二层
    被截成含糊的大父树。

    ``fold_text_leaves`` 是 worldinfo 的显式按需开关投影：它折叠每个文本叶子，和
    文本有多少字无关。状态不启用它，任何字符串都不会仅因为长而被折叠。

    清单（list）是一个值，不能单独按长度折叠；若它位于被折叠的末端对象中，会随
    整棵对象一起隐藏，展开后仍保持原形状。
    """
    out: Tree = {}
    for key, value in tree.items():
        if not (isinstance(value, dict) and value):
            if fold_text_leaves and isinstance(value, str):
                if texts.leaf is None:
                    raise ValueError("这棵树要求折叠文本叶子，却没有提供占位文字")
                out[key] = texts.leaf(len(value))
            else:
                out[key] = value
        elif tail_depth > 0 and 1 + _tree_depth(value) <= tail_depth:
            if texts.deeper is None:
                raise ValueError(f"这棵树声称是平的，`{key}` 底下却有嵌套")
            out[key] = texts.deeper(len(value))
        else:
            out[key] = fold(
                value,
                tail_depth=tail_depth,
                texts=texts,
                fold_text_leaves=fold_text_leaves,
            )
    return out


def _tree_depth(tree: Tree) -> int:
    """``tree`` 下最深的键层数；只用于决定离叶端还剩几层。"""
    return max(
        (1 + _tree_depth(value) if isinstance(value, dict) and value else 1)
        for value in tree.values()
    )


def keys_of(tree: Tree) -> list[str]:
    """这棵树上能用的地址，人类可读（``林 › 知情``）。

    **展开没命中时把它整个回给模型**：一是让它下一轮能改对，二是顺带压键名漂移
    ——它看见"心情"已经存在，就不会再造一个"情绪"。
    """
    from .models.state import paths_of

    return [label_path(p) for p in paths_of(tree)]


@dataclass(frozen=True)
class Expanded:
    """一次展开的结果。"""

    found: dict[str, JsonValue]
    """``{人类可读的地址: 取到的子树}``。"""

    missing: list[str]
    """没命中的那几条地址，同样人类可读。"""

    @property
    def empty(self) -> bool:
        return not self.found


def expand(tree: Tree, paths: list[Path]) -> Expanded:
    """按路径取回子树。**一条都没命中和部分没命中是两件事**，所以两边都留着。

    部分命中时照样把找到的给出去——为了几条不存在的地址把整次调用判成失败，
    等于让模型多花一轮重来。
    """
    found: dict[str, JsonValue] = {}
    missing: list[str] = []
    for path in paths:
        value = walk(tree, path)
        if value is None:
            missing.append(label_path(path))
        else:
            found[label_path(path)] = value
    return Expanded(found=found, missing=missing)
