"""state：结构化的世界当前状态。

术语（D61）——三样东西按"谁改变它"区分：
  - **state**     剧情推进改变（director 每 run 提交增量）  ← 本模块
  - **worldinfo** 用户改变（静态设定）
  - **history**   只增不改（过往 run 的细纲）

核心立场（D18）：**有限建模，结构化存储，判断交给模型。** 建模是为了让检索准、
维护省，不做规则引擎。真正买到的是**前移**——生成时状态是准的，很多矛盾根本
不会产生。

**state 不是信息的存放处，是一条捷径**（D99）。事情本身记在 history 里；state 把
"每轮都要用、又不该每次回头翻"的那几条拎到眼前。三个推论：

- **判据是双条件**：既要约束后面能写什么，又要值得随时看见。"照片朝向"过不了
  前者，"本轮剧情摘要"过不了后者。
- **删键不等于丢信息**——那件事仍在 history 里。因此 state 该保持短。
- **结构化靠提示词鼓励，不靠 schema 强制**（D20/D74 不变）：能用数字别用句子，
  能用是否别用描述，同类的事用同一套键名。既然是速查表，形状比文采重要。

**不要指望代码校验内容**（D30）：把 `关系[A][B] = 决裂` 存成结构化字段后再用
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
由 session 级声明（显式，符合 D15），或由模型动态决定。两条路对本层的要求是一致的
——给足自由度。

D21 的三层结构因此塌成两层：**自由 KV（即全部）+ 模板库（session 级声明，尚未做）**。
这不违反 D20：模型做的是"记一个键值对"（数据层），不是"设计一个数据结构"（元层）。

## 键的约定

键是自由字符串。以 `.` 分隔可表达从属，仅用于**渲染时分组**，系统不赋予它语义::

    时间                 "第三日 傍晚"
    时间.距上轮          "三小时后"
    地点                 "康德酒店 302 房"
    林.在场              true
    林.性格              "谨慎，话少"
    林.知情              ["照片的存在"]

已知代价是键名漂移（"心情"/"情绪"/"mood" 各写一次）。缓解手段是把已有键列表
回给模型让它复用——这才是 schema 自描述机制的主要价值。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

JsonValue = Any


class StateDelta(BaseModel):
    """一个 run 的状态变化。**权威数据**——当前状态是沿路径重放增量的结果（D48）。

    每 run 必须提交，**哪怕为空**（D19）。空增量极便宜；漏提交的代价极高——
    分叉在几百个 run 后才爆，且爆时查不出源头，还会让分支重建出错。
    """

    set: dict[str, JsonValue] = Field(default_factory=dict)
    """覆盖写。值为 ``None`` 表示删除该键。"""

    append: dict[str, list[JsonValue]] = Field(default_factory=dict)
    """追加到列表型的值（如知情、经历过的事）。去重。

    与 ``set`` 分开是因为几种写法都真实存在：关系、位置这类是覆盖，
    清单是增删。这不是对内容的假设，只是不同的写入语义。
    """

    remove: dict[str, list[JsonValue]] = Field(default_factory=dict)
    """从列表型的值里拿掉指定项。不存在的项静默忽略。

    **``append`` 的逆运算，补于 D96。** 原先只有增没有减，清单就只能只增不减；
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

        ``state_write`` 可在 loop 中多次调用（D56），中途的多次写入需要累积成
        一份增量落到节点上——节点与 run 一一对应（D51），一个 run 只有一份增量。
        """
        merged_append = {k: list(v) for k, v in self.append.items()}
        for key, items in other.append.items():
            target = merged_append.setdefault(key, [])
            target.extend(i for i in items if i not in target)

        merged_remove = {k: list(v) for k, v in self.remove.items()}
        for key, items in other.remove.items():
            target = merged_remove.setdefault(key, [])
            target.extend(i for i in items if i not in target)

        return StateDelta(
            set={**self.set, **other.set},
            append=merged_append,
            remove=merged_remove,
        )


class WorldState(BaseModel):
    """某个节点处的完整状态。**派生数据，不落盘**——落盘会导致用户编辑了快照
    却被重建覆盖，是最糟的体验。用户要改当前状态，实质是改最新节点的增量。
    """

    values: dict[str, JsonValue] = Field(default_factory=dict)

    def apply(self, delta: StateDelta) -> WorldState:
        """套用一个增量，返回新状态。不修改自身。

        顺序是 set → remove → append：先整体覆盖，再对清单增删。
        remove 在 append 之前，因此"换掉一项"可以在同一次写入里表达
        （remove 旧的 + append 新的），不必整体重写。
        """
        values = {k: (list(v) if isinstance(v, list) else v) for k, v in self.values.items()}

        for key, value in delta.set.items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value

        for key, items in delta.remove.items():
            current = values.get(key)
            if isinstance(current, list):
                values[key] = [i for i in current if i not in items]
            elif current is not None and current in items:
                # 单值也允许被 remove 掉——否则模型得记住这个键当初是不是列表。
                values.pop(key, None)

        for key, items in delta.append.items():
            current = values.get(key)
            target = (
                list(current)
                if isinstance(current, list)
                else ([] if current is None else [current])
            )
            target.extend(i for i in items if i not in target)
            values[key] = target

        return WorldState(values=values)

    def keys(self) -> list[str]:
        """已用过的全部键。回给模型复用，防止键名漂移。"""
        return sorted(self.values)

    def grouped(self) -> dict[str, dict[str, JsonValue]]:
        """按第一段前缀分组，仅用于渲染。系统不赋予 ``.`` 任何语义。

        无前缀的键归到 ``""`` 组。
        """
        groups: dict[str, dict[str, JsonValue]] = {}
        for key in sorted(self.values):
            head, _, tail = key.partition(".")
            if tail:
                groups.setdefault(head, {})[tail] = self.values[key]
            else:
                groups.setdefault("", {})[head] = self.values[key]
        return groups


def replay(deltas: list[StateDelta]) -> WorldState:
    """沿一条路径重放增量，得到该点的状态。

    **状态是路径的函数，不是全局单例**（D48）。不存在"当前状态"这个全局对象，
    只存在"沿某条路径重放到某点的状态"——否则切换分支后会出现
    "文本对、状态悄悄错"，且要很多 run 后才以角色行为不合理的形式暴露。
    """
    state = WorldState()
    for delta in deltas:
        state = state.apply(delta)
    return state
