"""state 重放的语义。

两条核心：D48 状态是路径的函数；**存储层不预设任何维度**——时间、地点、角色属性的
粒度与形态都取决于故事，定不了固定 schema。
"""

from agenttavern.models import StateDelta, WorldState, replay


def test_empty_delta_is_legal_and_noop() -> None:
    """空增量合法（D19）——每 run 必交，哪怕没有变化。"""
    d = StateDelta()
    assert d.is_empty()
    assert replay([d, d, d]) == WorldState()


# -- 自由度：任何粒度、任何形态 ------------------------------------------------


def test_any_shape_of_time_is_expressible() -> None:
    """时间服务于写作：钟点、时段、跨度在不同故事里各有意义，系统不挑。"""
    assert replay([StateDelta(set={"时间": "下午一点"})]).values["时间"] == "下午一点"
    assert replay([StateDelta(set={"时间": "深夜"})]).values["时间"] == "深夜"
    assert (
        replay([StateDelta(set={"时间.距上轮": "过了一个月"})]).values["时间.距上轮"]
        == "过了一个月"
    )


def test_any_granularity_of_place_is_expressible() -> None:
    state = replay(
        [
            StateDelta(set={"地点": "北欧", "地点.细": "康德酒店 302"}),
            StateDelta(set={"地点.场合": "婚礼进行中"}),
        ]
    )
    assert state.values["地点"] == "北欧"
    assert state.values["地点.场合"] == "婚礼进行中"


def test_arbitrary_character_attributes() -> None:
    """性格、能力、与他人的关系都不可能预先穷举，因此不预设。"""
    state = replay(
        [
            StateDelta(
                set={"林.性格": "谨慎，话少", "林.在场": True, "林与陈.关系": "疏远"},
                append={"林.知情": ["照片的存在"]},
            )
        ]
    )
    assert state.values["林.性格"] == "谨慎，话少"
    assert state.values["林与陈.关系"] == "疏远"
    assert state.values["林.知情"] == ["照片的存在"]


def test_story_without_knowledge_tracking_is_fine() -> None:
    """不是所有故事都要维护知情——不写就是没有，不需要空字段占位。"""
    state = replay([StateDelta(set={"气氛": "紧绷"})])
    assert state.paths() == [["气氛"]]


# -- 写入语义 ----------------------------------------------------------------


def test_set_overwrites_and_none_deletes() -> None:
    state = replay(
        [
            StateDelta(set={"地点": "厨房", "伏笔:照片": "已埋"}),
            StateDelta(set={"地点": "楼下", "伏笔:照片": None}),
        ]
    )
    assert state.values == {"地点": "楼下"}


def test_append_accumulates_and_dedupes() -> None:
    """追加用于单调累积的清单，如某角色的知情。"""
    state = replay(
        [
            StateDelta(append={"林.知情": ["照片的存在"]}),
            StateDelta(append={"林.知情": ["照片的存在", "陈撒谎了"]}),
        ]
    )
    assert state.values["林.知情"] == ["照片的存在", "陈撒谎了"]


def test_append_onto_missing_key_creates_a_list() -> None:
    assert replay([StateDelta(append={"新键": ["甲"]})]).values["新键"] == ["甲"]


def test_remove_takes_items_off_a_list() -> None:
    """``append`` 的逆运算（D96）。

    缺了它，清单就只增不减，去掉一项只能 ``set`` 整个列表——实测模型干脆把
    列表拍成了字符串，结构一次性塌掉。纯文学场景看不见这个缺口：那里的
    知情、经历天然只增不减。**数据化的世界才会暴露它。**
    """
    state = replay(
        [
            StateDelta(set={"沈砚.随身": ["青锋剑", "止血符 ×2", "定身符 ×1"]}),
            StateDelta(remove={"沈砚.随身": ["定身符 ×1"]}),
        ]
    )
    assert state.values["沈砚.随身"] == ["青锋剑", "止血符 ×2"]


def test_remove_then_append_swaps_one_item_in_a_single_write() -> None:
    """「用掉一张符」= 去掉旧计数 + 放上新计数，**一次写入内完成**。

    因此 apply 的顺序必须是 set → remove → append。
    """
    state = replay(
        [
            StateDelta(set={"沈砚.随身": ["青锋剑", "止血符 ×2"]}),
            StateDelta(remove={"沈砚.随身": ["止血符 ×2"]}, append={"沈砚.随身": ["止血符 ×1"]}),
        ]
    )
    assert state.values["沈砚.随身"] == ["青锋剑", "止血符 ×1"]


def test_remove_ignores_absent_items() -> None:
    """不存在的条目静默忽略——模型记错一个字不该让整次写入失败。"""
    state = replay(
        [
            StateDelta(append={"林.知情": ["照片"]}),
            StateDelta(remove={"林.知情": ["根本没有这条"], "根本没有这个键": ["甲"]}),
        ]
    )
    assert state.values["林.知情"] == ["照片"]


def test_remove_on_a_scalar_deletes_the_key() -> None:
    """单值也能被 remove 掉，模型不必记住这个键当初是不是列表。"""
    state = replay([StateDelta(set={"天气": "雨"}), StateDelta(remove={"天气": ["雨"]})])
    assert "天气" not in state.values


def test_remove_merges_across_multiple_writes_in_one_run() -> None:
    """``state_write`` 可在一个 run 内多次调用，三种语义都要累积（D56）。"""
    delta = StateDelta(remove={"包裹": ["甲"]}).merge(
        StateDelta(remove={"包裹": ["乙"], "另一个": ["丙"]})
    )
    assert delta.remove == {"包裹": ["甲", "乙"], "另一个": ["丙"]}
    assert not delta.is_empty()


def test_unmentioned_keys_are_untouched() -> None:
    """离场再出场天然成立：只改提到的键，其余保持原状。"""
    state = replay(
        [
            StateDelta(set={"陈.在场": True}, append={"陈.知情": ["照片"]}),
            StateDelta(set={"陈.在场": False}),
            StateDelta(set={"陈.在场": True}),
        ]
    )
    assert state.values["陈.在场"] is True
    assert state.values["陈.知情"] == ["照片"]


# -- 无副作用 ----------------------------------------------------------------


def test_apply_does_not_mutate_source() -> None:
    """重放必须无副作用——同一状态会被多条分支路径共享。"""
    base = replay([StateDelta(set={"地点": "门口"}, append={"林.知情": ["a"]})])
    branched = base.apply(StateDelta(set={"地点": "客厅"}, append={"林.知情": ["b"]}))

    assert base.values["地点"] == "门口"
    assert base.values["林.知情"] == ["a"]
    assert branched.values["地点"] == "客厅"
    assert branched.values["林.知情"] == ["a", "b"]


def test_merge_accumulates_like_replay() -> None:
    """多次 state_write 累积成一份增量——一个 run 只有一份（D51/D56）。"""
    merged = StateDelta(set={"地点": "门口"}, append={"林.知情": ["a"]}).merge(
        StateDelta(set={"地点": "客厅", "时间": "傍晚"}, append={"林.知情": ["b"]})
    )
    assert merged.set == {"地点": "客厅", "时间": "傍晚"}
    assert merged.append["林.知情"] == ["a", "b"]


# -- 渲染分组 ----------------------------------------------------------------


def test_structure_is_the_json_structure() -> None:
    """从属关系**用嵌套表达**，不再用点分键名（D157）。

    早先是一层平铺 + `林.性格` 这种键，点号"仅用于渲染时分组"。换掉的理由里
    最硬的一条是：平铺结构里没有"层"，按层级控制展开根本做不了。
    """
    state = replay([StateDelta(set={"时间": "傍晚", "林": {"性格": "谨慎", "在场": True}})])

    assert state.values == {"时间": "傍晚", "林": {"性格": "谨慎", "在场": True}}
    assert state.paths() == [["时间"], ["林", "在场"], ["林", "性格"]]
    assert state.at(["林", "性格"]) == "谨慎"
    # 路上断了就是 None，不抛。
    assert state.at(["林", "没这个"]) is None
    assert state.at(["时间", "当前"]) is None


def test_a_set_merges_deeply_instead_of_replacing_the_whole_branch() -> None:
    """**深合并**（D157）：改一项不必重发整个角色。

    整体重发正是清单被拍平的老路（D96）——模型为了改一个属性重写整个对象，
    顺手把旁边那个清单写成了一句话。
    """
    state = replay([StateDelta(set={"林": {"在场": True, "知情": ["照片"]}})])
    state = state.apply(StateDelta(set={"林": {"性格": "谨慎"}}))

    assert state.values["林"] == {"在场": True, "知情": ["照片"], "性格": "谨慎"}


def test_a_null_leaf_deletes_that_subtree() -> None:
    state = replay([StateDelta(set={"林": {"在场": True, "性格": "谨慎"}, "地点": "客厅"})])
    state = state.apply(StateDelta(set={"林": {"性格": None}, "地点": None}))

    assert state.values == {"林": {"在场": True}}


def test_append_and_remove_address_nested_lists() -> None:
    state = replay([StateDelta(set={"林": {"知情": ["照片"]}})])
    state = state.apply(StateDelta(append={"林": {"知情": ["西山的坟"]}}))
    state = state.apply(StateDelta(remove={"林": {"知情": ["照片"]}}))

    assert state.values["林"]["知情"] == ["西山的坟"]
