"""state：结构化的世界当前状态。

术语（S52）——三样东西按"谁改变它"区分：
  - **state**     剧情推进改变（director 每 run 提交增量）  ← 本模块
  - **worldinfo** 用户改变（静态设定）
  - **history**   只增不改（过往 run 的细纲）

核心立场（S30）：**有限建模，结构化存储，判断交给模型。** 建模是为了让检索准、
维护省，不做规则引擎。真正买到的是**前移**——生成时状态是准的，很多矛盾根本
不会产生。

**state 不是信息的存放处，是一条捷径**（S36）。事情本身记在 history 里；state 把
"每轮都要用、又不该每次回头翻"的那几条拎到眼前。三个推论：

- **判据是双条件**：既要约束后面能写什么，又要值得随时看见。"照片朝向"过不了
  前者，"本轮剧情摘要"过不了后者。
- **删键不等于丢信息**——那件事仍在 history 里。因此 state 该保持短。
- **结构化靠提示词鼓励，不靠 schema 强制**（S30 不变）：能用数字别用句子，
  能用是否别用描述，同类的事用同一套键名。既然是速查表，形状比文采重要。

**不要指望代码校验内容**（P11）：把 `关系[A][B] = 决裂` 存成结构化字段后再用
代码读取比对，不是独立验证——该字段的值本就是模型写进去的。语义判断发生在写入时，
代码检查发生在读取时。

---

## 为什么没有"固定核心"

曾按 present / knows / time / location 定义过一层固定核心 schema。**该设计已取消**，
原因不是字段选得不好，而是这类 schema 在原理上就定不下来：

- **时间是服务于写作的。** "下午一点""是晚上""过了一个月"在不同情境下影响完全不同，
  粒度与形态都不定。细化字段解决不了——它只是把猜测挪到更深一层。
- **地点同样。** 可以是某国家、某城市、某酒店、"婚礼进行中"、"山顶"。
- **知情不是每个故事都需要维护。** 而角色能挂的属性——性格、能力、与他人的关系——
  也不可能预先穷举。

因此存储层**不预设任何维度**，只提供命名空间与读写语义。"哪些维度存在"往上挪一层：
由 session 级声明（显式，符合 S82），或由模型动态决定。两条路对本层的要求是一致的
——给足自由度。

schema 因此只有两层：**自由 KV（即全部）+ 模板库（session 级声明，尚未做）**（S30）。
这不违反 S30：模型做的是"记一个键值对"（数据层），不是"设计一个数据结构"（元层）。

## 结构就是 JSON 的结构（S31）

从属关系**用嵌套表达**，不再用点分键名::

    {
      "时间": {"当前": "第三日 傍晚", "距上轮": "三小时后"},
      "地点": "康德酒店 302 房",
      "林": {"在场": true, "性格": "谨慎，话少", "知情": ["照片的存在"]}
    }

早先是一层平铺 + `林.在场` 这种键名，点号"仅用于渲染时分组，系统不赋予它语义"。
那个约定有三处不划算：

- **它是一套要教的私有语法。** 模型得学会点号在这里代表什么，而 JSON 自带的
  嵌套它本来就会。
- **分组只在渲染时发生，存储仍是平的**，于是"某个角色名下有哪些东西"这件事
  代码得现算，且键里出现真正的点号就会算错。
- **按层级控制展开做不了。** 平铺结构里没有"层"，只能按值的字符数藏——
  而字符数和"这一块要不要给"没有关系。嵌套之后深度是现成的（`render_state`
  的 `depth`）。

**寻址一律用路径数组**（``["林", "知情"]``），不用点分字符串：点号刚被取消了
结构含义，再拿它当寻址语法等于把刚拆掉的歧义装回去，而且键里本来就可能有点。

已知代价仍是键名漂移（"心情"/"情绪"/"mood" 各写一次）。缓解手段不变：把已有的
结构回给模型让它复用。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

JsonValue = Any

Tree = dict[str, JsonValue]
"""嵌套的状态树。叶子是任意 JSON 值，中间层一律是 ``dict``。"""

Path = list[str]
"""定位一处状态的路径，一段一个键（``["林", "知情"]``）。"""


def label_path(path: Path) -> str:
    """路径的人类可读写法（``林 › 知情``）。

    **只用于给人看**——trace 标签、采样标记、错误提示。寻址一律用数组（S31）：
    这里用的分隔符不是语法，换成别的字符也不影响任何行为。
    """
    return " › ".join(path)


def walk(tree: Tree, path: Path) -> JsonValue:
    """按路径取值。路上任何一段不存在或不是字典就返回 ``None``。"""
    node: JsonValue = tree
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node


def paths_of(tree: Tree, prefix: Path | None = None) -> list[Path]:
    """全部**叶子**路径，深度优先、字典序。

    回给模型让它复用已有的写法——防键名漂移靠的一直是这个（S30）。
    """
    out: list[Path] = []
    for key in sorted(tree):
        here = [*(prefix or []), key]
        value = tree[key]
        # 空字典也算叶子：它是"这里有个东西但暂时没内容"，不该消失。
        if isinstance(value, dict) and value:
            out.extend(paths_of(value, here))
        else:
            out.append(here)
    return out


def _merge_into(target: Tree, patch: Tree) -> None:
    """把 ``patch`` 深合并进 ``target``。**就地修改。**

    - 两边都是字典 → 递归下去，只动提到的那几个键。
    - 叶子 → 覆盖。
    - ``None`` → **删掉这个键**（连同它下面的整棵子树）。

    深合并是这套写法的关键：改一个角色的一项属性不必重发整个角色，
    而整体重发正是清单被拍平的老路（S32）。
    """
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        elif isinstance(value, dict):
            target[key] = _clone(value)
        else:
            target[key] = list(value) if isinstance(value, list) else value


def _clone(tree: Tree) -> Tree:
    out: Tree = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            out[key] = _clone(value)
        elif isinstance(value, list):
            out[key] = list(value)
        else:
            out[key] = value
    return out


def _leaf_paths(patch: Tree, prefix: Path | None = None) -> list[tuple[Path, JsonValue]]:
    """``append`` / ``remove`` 的补丁里，每个**非字典叶子**及其路径。

    它们的叶子必须是列表——那两个动作只对清单成立。
    """
    out: list[tuple[Path, JsonValue]] = []
    for key in sorted(patch):
        here = [*(prefix or []), key]
        value = patch[key]
        if isinstance(value, dict):
            out.extend(_leaf_paths(value, here))
        else:
            out.append((here, value))
    return out


def _as_list(items: JsonValue) -> list[JsonValue]:
    """``append`` / ``remove`` 的叶子。写成单个值也认——模型时常这么写，
    为此退回一个错误不值得。
    """
    return list(items) if isinstance(items, list) else [items]


def _ensure(tree: Tree, path: Path) -> tuple[Tree, str] | None:
    """走到 ``path`` 的父节点，沿途缺的字典补上。最后一段不是键就返回 ``None``。"""
    if not path:
        return None
    node = tree
    for segment in path[:-1]:
        nxt = node.get(segment)
        if not isinstance(nxt, dict):
            # 中间挡着一个非字典值：不静默覆盖它，这一笔跳过。
            if nxt is not None:
                return None
            nxt = {}
            node[segment] = nxt
        node = nxt
    return node, path[-1]


class StateDelta(BaseModel):
    """一个 run 的状态变化。**权威数据**——当前状态是沿路径重放增量的结果（S42）。

    每 run 必须提交，**哪怕为空**（S43）。空增量极便宜；漏提交的代价极高——
    分叉在几百个 run 后才爆，且爆时查不出源头，还会让分支重建出错。
    """

    set: Tree = Field(default_factory=dict)
    """覆盖写，**深合并**（S31）：只动补丁里提到的那几个键，别的原样留着。
    叶子为 ``None`` 表示删掉那个键（连同它下面的整棵子树）。

    深合并是这套写法的关键——改一个角色的一项属性不必重发整个角色，
    而整体重发正是清单被拍平的老路（S32）。
    """

    append: Tree = Field(default_factory=dict)
    """追加到清单型的值（如知情、经历过的事）。去重。**叶子必须是列表。**

    与 ``set`` 分开是因为几种写法都真实存在：关系、位置这类是覆盖，
    清单是增删。这不是对内容的假设，只是不同的写入语义。
    """

    remove: Tree = Field(default_factory=dict)
    """从列表型的值里拿掉指定项。不存在的项静默忽略。

    **``append`` 的逆运算，补于 S32。** 原先只有增没有减，清单就只能只增不减；
    要去掉一项就得 ``set`` 整个列表，而整体重写既贵又容易丢项——实测模型
    干脆把列表拍成了一个字符串（`["青锋剑", "止血符 ×2"]` → `"青锋剑、止血符 ×4"`），
    结构一次性塌掉且不可逆。

    这个缺口在纯文学场景里看不见：那里的清单（知情、经历）天然只增不减。
    **数据化的世界才会暴露它**——消耗品会用完，随从会离队，伏笔会被揭开。
    """

    def is_empty(self) -> bool:
        return not self.set and not self.append and not self.remove

    def merge(self, other: StateDelta) -> StateDelta:
        """把 ``other`` 叠加到自身之上，返回新增量。

        ``state_write`` 可在 loop 中多次调用（S20），中途的多次写入需要累积成
        一份增量落到节点上——节点与 run 一一对应（S1），一个 run 只有一份增量。
        """
        merged_set = _clone(self.set)
        _merge_into(merged_set, other.set)

        def union(mine: Tree, theirs: Tree) -> Tree:
            out = _clone(mine)
            for path, items in _leaf_paths(theirs):
                spot = _ensure(out, path)
                if spot is None:
                    continue
                parent, leaf = spot
                current = parent.get(leaf)
                target = list(current) if isinstance(current, list) else []
                target.extend(i for i in _as_list(items) if i not in target)
                parent[leaf] = target
            return out

        return StateDelta(
            set=merged_set,
            append=union(self.append, other.append),
            remove=union(self.remove, other.remove),
        )


class WorldState(BaseModel):
    """某个节点处的完整状态。**派生数据，不落盘**——落盘会导致用户编辑了快照
    却被重建覆盖，是最糟的体验。用户要改当前状态，实质是改最新节点的增量。
    """

    values: Tree = Field(default_factory=dict)

    def apply(self, delta: StateDelta) -> WorldState:
        """套用一个增量，返回新状态。不修改自身。

        顺序是 set → remove → append：先覆盖，再对清单增删。
        remove 在 append 之前，因此"换掉一项"可以在同一次写入里表达
        （remove 旧的 + append 新的），不必整体重写。
        """
        values = _clone(self.values)
        _merge_into(values, delta.set)

        for path, items in _leaf_paths(delta.remove):
            spot = _ensure(values, path)
            if spot is None:
                continue
            parent, leaf = spot
            current = parent.get(leaf)
            drop = _as_list(items)
            if isinstance(current, list):
                parent[leaf] = [i for i in current if i not in drop]
            elif current is not None and current in drop:
                # 单值也允许被 remove 掉——否则模型得记住这个键当初是不是清单。
                parent.pop(leaf, None)

        for path, items in _leaf_paths(delta.append):
            spot = _ensure(values, path)
            if spot is None:
                continue
            parent, leaf = spot
            current = parent.get(leaf)
            target = (
                list(current)
                if isinstance(current, list)
                else ([] if current is None else [current])
            )
            target.extend(i for i in _as_list(items) if i not in target)
            parent[leaf] = target

        return WorldState(values=values)

    def paths(self) -> list[Path]:
        """已用过的全部叶子路径。回给模型复用，防止键名漂移。"""
        return paths_of(self.values)

    def at(self, path: Path) -> JsonValue:
        """按路径取一处状态；路上断了就是 ``None``。"""
        return walk(self.values, path)


def _diff_tree(before: Tree, after: Tree) -> Tree:
    out: Tree = {}
    for key, value in after.items():
        if key not in before:
            # **新键整份写下去，空字典也算**——"这里有个东西但暂时没内容"是
            # 一种真实的状态（`paths_of` 也把它当叶子），递归下去会得到空补丁，
            # 于是这个键根本不会被建出来。
            out[key] = _clone(value) if isinstance(value, dict) else value
            continue
        old = before[key]
        if isinstance(value, dict) and isinstance(old, dict):
            inner = _diff_tree(old, value)
            if inner:
                out[key] = inner
        elif value != old:
            out[key] = list(value) if isinstance(value, list) else value
    for key in before:
        if key not in after:
            out[key] = None  # `set` 里的 `None` 就是删键（连同整棵子树）
    return out


def diff(before: Tree, after: Tree) -> StateDelta:
    """由「想要的结果」反推出增量。**「改结果」那个视图的落地方式**（R22）。

    用户改状态有两种操作：改这一轮的**指令**（直接改增量），或者改这一轮之后的
    **结果**（改状态树本身）。后者要能落到数据上，就得和这一轮之前的状态求差。

    差分放在这里而不是前端，是为了不破坏「**权威是增量**」（S42）：接口收到
    结果的那一刻就把它换算成增量了，落盘的仍然只有增量，没有第二份真相。

    只产出 ``set``：它配合深合并已经表达得了一切改动（改值、删键、换整个列表），
    而 ``append`` / ``remove`` 是**写入语义**，不是差分结果——从两棵树看不出
    用户心里想的是"追加一项"还是"换成这个列表"，猜错了下游重放就会不一样。
    要表达追加走「改指令」那个视图。

    保证 ``WorldState(values=before).apply(diff(before, after)).values == after``。
    """
    return StateDelta(set=_diff_tree(before, after))


def replay(deltas: list[StateDelta]) -> WorldState:
    """沿一条路径重放增量，得到该点的状态。

    **状态是路径的函数，不是全局单例**（S42）。不存在"当前状态"这个全局对象，
    只存在"沿某条路径重放到某点的状态"——否则切换分支后会出现
    "文本对、状态悄悄错"，且要很多 run 后才以角色行为不合理的形式暴露。
    """
    state = WorldState()
    for delta in deltas:
        state = state.apply(delta)
    return state
