"""折叠与展开：**同一件事的两半，一份实现**（S157）。

这份文件钉的是那一份实现本身。三个域各自怎么摊成一棵树、地址长什么样，
在 `test_tools.py`；上下文里折出来的字长什么样，在 `test_retrieval_off.py`
和这里的最后两条。
"""

from __future__ import annotations

import pytest

from agenttavern.disclosure import FoldText, expand, fold, keys_of
from agenttavern.models import WorldbookEntry, WorldbookMode, WorldState
from agenttavern.prompting import render_state, render_worldinfo

TEXTS = FoldText(
    leaf=lambda chars: f"（{chars} 字）",
    deeper=lambda count: f"（{count} 项）",
)

NESTED = {
    "角色": {"王五": {"在场": True, "知情": ["匿名信"]}},
    "时间": "第一日 傍晚",
}


# -- 折叠 ---------------------------------------------------------------------


def test_it_folds_the_last_two_levels_of_each_branch() -> None:
    """从叶端计算；「王五 + 两个叶子」作为最后两层一起折叠。"""
    folded = fold(NESTED, tail_depth=2, texts=TEXTS)
    assert folded == {"角色": {"王五": "（2 项）"}, "时间": "第一日 傍晚"}


def test_tail_depth_zero_means_no_structural_folding() -> None:
    assert fold(NESTED, tail_depth=0, texts=TEXTS) == NESTED


def test_uneven_branches_keep_outer_paths_and_fold_at_their_own_leaf_end() -> None:
    tree = {
        "案件": {
            "证物库": {
                "铜盒": {
                    "封存编号": "BX-4319",
                    "夹层": {"微刻原文": "第三声钟响后", "邮签": "黑羽三角"},
                }
            },
            "路线候选": {"钟楼": {"状态": "未调查", "风险": "入口程序未知"}},
        }
    }

    folded = fold(tree, tail_depth=2, texts=TEXTS)

    assert folded["案件"]["证物库"]["铜盒"]["封存编号"] == "BX-4319"
    assert folded["案件"]["证物库"]["铜盒"]["夹层"] == "（2 项）"
    assert folded["案件"]["路线候选"]["钟楼"] == "（2 项）"


def test_a_list_is_a_value_and_never_folded() -> None:
    """**清单折不得**：折它等于把形状顶替成一个字符串（S32）。

    模型照着写回，清单就塌了。长清单该做的是拆一层嵌套，不是藏起来。
    """
    tree = {"清单": ["甲", "乙", "丙"] * 50}
    assert fold(tree, tail_depth=2, texts=TEXTS) == tree


def test_a_long_string_is_not_folded_because_of_its_size() -> None:
    long = "很长的一段。" * 100
    folded = fold({"背景": long, "短的": "在场"}, tail_depth=2, texts=TEXTS)
    assert folded == {"背景": long, "短的": "在场"}


def test_explicit_leaf_folding_does_not_depend_on_string_size() -> None:
    """worldinfo 由条目的按需模式决定，三个字和三千字走同一条规则。"""
    folded = fold({"教团": "短"}, tail_depth=0, texts=TEXTS, fold_text_leaves=True)
    assert folded == {"教团": "（1 字）"}


def test_a_flat_tree_may_omit_the_deeper_text() -> None:
    """平的树不必给「折起来的子树」那句话——给了也是死字符串。

    真遇到嵌套时**当场报错**：那是编程错误（这棵树不该有嵌套），
    不是模型能读到的东西，所以它抛异常而不是拼一句话出来。
    """
    flat = FoldText(leaf=lambda chars: f"（{chars} 字）")
    assert fold({"教团": "内容"}, tail_depth=0, texts=flat, fold_text_leaves=True) == {
        "教团": "（2 字）"
    }

    with pytest.raises(ValueError, match="嵌套"):
        fold({"教团": {"里面": "还有"}}, tail_depth=2, texts=flat)


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


def test_state_folds_each_branch_from_its_leaf_end() -> None:
    rendered = render_state(WorldState(values=NESTED), agent="director")
    assert "王五" in rendered, "第二层还看得见"
    assert "在场" not in rendered, "叶端两层要折起来"
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
