"""折叠与展开：**同一件事的两半，一份实现**（S157）。

这份文件钉的是那一份实现本身。三个域各自怎么摊成一棵树、地址长什么样，
在 `test_tools.py`；上下文里折出来的字长什么样，在 `test_retrieval_off.py`
和这里的最后两条。
"""

from __future__ import annotations

import pytest

from agenttavern.disclosure import FoldText, expand, fold, keys_of
from agenttavern.models import WorldbookEntry, WorldbookMode, WorldState
from agenttavern.prompting import fold_depth, render_state, render_worldinfo

TEXTS = FoldText(
    long=lambda chars: f"（{chars} 字）",
    deeper=lambda count: f"（{count} 项）",
)

NESTED = {
    "角色": {"王五": {"在场": True, "知情": ["匿名信"]}},
    "时间": "第一日 傍晚",
}


# -- 折叠 ---------------------------------------------------------------------


def test_it_folds_below_the_given_level() -> None:
    """**深过 `depth` 的子树只给项数**——「角色 › 王五 › ...」在第二层折起来。"""
    folded = fold(NESTED, depth=2, texts=TEXTS, inline_limit=200)
    assert folded == {"角色": {"王五": "（2 项）"}, "时间": "第一日 傍晚"}


def test_depth_zero_means_no_folding() -> None:
    """`depth <= 0` 整棵给出去——关掉按需展开时走的就是这条（S55）。"""
    assert fold(NESTED, depth=0, texts=TEXTS, inline_limit=200) == NESTED


def test_a_list_is_a_value_and_never_folded() -> None:
    """**清单折不得**：折它等于把形状顶替成一个字符串（S32）。

    模型照着写回，清单就塌了。长清单该做的是拆一层嵌套，不是藏起来。
    """
    tree = {"清单": ["甲", "乙", "丙"] * 50}
    assert fold(tree, depth=1, texts=TEXTS, inline_limit=1) == tree


def test_a_long_string_leaf_gives_its_size() -> None:
    long = "很长的一段。" * 100
    folded = fold({"背景": long, "短的": "在场"}, depth=0, texts=TEXTS, inline_limit=200)
    assert folded == {"背景": f"（{len(long)} 字）", "短的": "在场"}


def test_inline_limit_zero_folds_every_string() -> None:
    """`inline_limit <= 0` = **一律折起来**，不看它有多长。

    worldinfo 就是这样：条目正文一概按需展开，一条三个字的设定也不例外——
    在那棵树上「给不给」由 `mode` 决定，不由长度决定。
    """
    folded = fold({"教团": "短"}, depth=0, texts=TEXTS, inline_limit=0)
    assert folded == {"教团": "（1 字）"}


def test_a_flat_tree_may_omit_the_deeper_text() -> None:
    """平的树不必给「折起来的子树」那句话——给了也是死字符串。

    真遇到嵌套时**当场报错**：那是编程错误（这棵树不该有嵌套），
    不是模型能读到的东西，所以它抛异常而不是拼一句话出来。
    """
    flat = FoldText(long=lambda chars: f"（{chars} 字）")
    assert fold({"教团": "内容"}, depth=0, texts=flat, inline_limit=0) == {"教团": "（2 字）"}

    with pytest.raises(ValueError, match="嵌套"):
        fold({"教团": {"里面": "还有"}}, depth=1, texts=flat, inline_limit=0)


# -- 展开 ---------------------------------------------------------------------


def test_it_takes_a_whole_subtree() -> None:
    """路径可以停在中间层——**取一整棵子树**（S31）。

    折叠把深处折成了「N 项」，模型看到的就是那一层的位置，
    所以它必须能照着那个位置直接要下去。
    """
    got = expand(NESTED, [["角色", "王五"]])
    assert got.found == {"角色 › 王五": {"在场": True, "知情": ["匿名信"]}}
    assert got.missing == []


def test_a_partial_miss_keeps_both_halves() -> None:
    """**部分命中和一条都没命中是两件事**，所以两边都留着。

    部分命中时照样把找到的给出去——为了几条不存在的地址把整次调用判成失败，
    等于让模型多花一轮重来。
    """
    got = expand(NESTED, [["时间"], ["没有这个"]])
    assert got.found == {"时间": "第一日 傍晚"}
    assert got.missing == ["没有这个"]
    assert not got.empty


def test_keys_are_human_readable_leaf_paths() -> None:
    """没命中时回给模型的就是这份清单——顺带压键名漂移（S30）。"""
    # 字典序，深度优先（`paths_of`）——「时间」排在「角色」前面。
    assert keys_of(NESTED) == ["时间", "角色 › 王五 › 在场", "角色 › 王五 › 知情"]


# -- 两半必须对得上 -----------------------------------------------------------


def test_the_fold_depth_comes_from_the_yaml_and_really_folds() -> None:
    """**折叠层级是一个值，写在那一节 context 上**（S67）。

    这条测试是补的，因为**这条路径从来没生效过**：`render_state` 的 `depth`
    两个调用点都没传，恒为 0，折叠整个是死代码——而界面上那个「展开层级」
    输入框用户填了、存了盘、对模型毫无影响。
    """
    assert fold_depth("director") == 2
    assert fold_depth("state_manager") == 2

    rendered = render_state(WorldState(values=NESTED), agent="director")
    assert "王五" in rendered, "第二层还看得见"
    assert "在场" not in rendered, "第三层要折起来"
    assert "state_query" in rendered, "折起来的地方要说怎么取回去"


def test_what_is_folded_is_addressable() -> None:
    """**折叠留下的 key，展开必须认得**——两半对不上的时候没有任何地方会报错。

    模型照着占位符里的名字去调工具，工具说"没有这个"，而那一段就此取不回来了。
    """
    entries = [
        WorldbookEntry(name="教团", content="总部在旧城区。" * 50, mode=WorldbookMode.ON_DEMAND)
    ]
    index = render_worldinfo(entries)
    assert "教团" in index

    tree = {e.name: e.content for e in entries}
    assert not expand(tree, [["教团"]]).empty
