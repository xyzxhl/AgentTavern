"""eval 采样标记。

**这里不做结构性检查。** 曾设想比对"时间单调""说话者 ⊆ 在场""地点可达"，
实现时发现当前数据模型都支撑不了：``time.absolute`` 是自然语言无法机械比较、
从正文抽说话者是语义工作（P11）、``location`` 只是字符串没有拓扑。而
"知情者是否在场"这类比对看似成立，实则也不可靠——信息可以通过转述、书信、
监听获得，不在场并不蕴含不知情。

**硬做只会得到假警报，而假警报比不检查更糟——它会让人不再信任系统。**

因此本模块只产出**采样标记**：正则不是判断器，是采样器（P12）——把可疑样本挑出来
定向送 human / AI eval，让覆盖率不必靠盲抽。标记从不影响运行时行为（P12：
重生成成本高，决定权在用户）。

这里的每一条都只看**形状**，不看内容：长度、类型、有没有提交。凡是需要判断
"这条记得对不对""这个细节该不该记"的，都不在代码这一侧。
"""

from __future__ import annotations

from ..config import LIMITS
from ..models import StateDelta
from ..models.state import label_path as _label
from ..models.state import paths_of, walk

_DENSITY_MIN_CHARS = {
    "sparse": 80,
    "normal": 150,
    "rich": 300,
}
"""每个事件至少几个字，按铺陈程度分档。**键是 `writer.yaml` 里 `density` 那组
的档位名**（R31：档位定义在 YAML，这里只按名字查一个数值阈值）。

**找不到就按 normal 算**：用户加一档文风或改个档位名，不该让整轮生成失败——
这是个采样标记的阈值，不是契约（P12/P12：采样标记从不影响运行时行为）。
"""


def _min_chars(density: str) -> int:
    return _DENSITY_MIN_CHARS.get(density, _DENSITY_MIN_CHARS["normal"])


STATE_VALUE_MAX_CHARS = LIMITS.sampling.state_value_max_chars
"""单个状态值的长度上限。超过通常说明这个键装了不止一件事。

state 的用处是**让模型在很多轮之后不写出矛盾**，因此每个键应当是一条可被违反的约束。
一个塞进小传的值（"已故男子，照片中的另一人；名字由陈屿说出；与陈屿相识；……"）
既不好改也不好读，等于把细纲复述了一遍。**这不判断内容对错，只报告形状。**
"""


def _bloated_keys(values: dict[str, object]) -> list[str]:
    """挑出形状可疑的位置：值过长，或值里串了多件事。**走叶子路径**（S31）。"""
    suspect = []
    for path in paths_of(values):
        value = walk(values, path)
        if not isinstance(value, str):
            continue
        if len(value) > STATE_VALUE_MAX_CHARS or value.count("；") >= 2:
            suspect.append(_label(path))
    return suspect


def _shadowed_keys(before: dict[str, object], written: dict[str, object]) -> list[str]:
    """挑出与已有位置**只差一层归属**的新位置。

    实测：``沈砚 › 冷却`` 已存在，模型另起了一个顶层的 ``冷却``——那一轮 state 里
    两个冷却并存且值不同。它下一轮自己发现并删掉了，但中间那一轮是脏的，
    而**没有任何检查报得出来**：值的形状没变、没有超长、也不算漏记。

    只报"一个顶层裸键 vs 一个挂在谁名下的同名键"这一种。``林昭 › 知情`` 与
    ``陈屿 › 知情`` 末段相同却都是对的——**两边都有归属就不是歧义**。

    嵌套之后这条判据比点分键名时更硬：以前"有没有前缀"要靠字符串里找点号，
    键名里真带个点就会误判；现在它就是路径长度。
    """
    paths = paths_of(before) + paths_of(written)
    suspect = []
    for path in paths_of(written):
        for other in paths:
            if other == path or other[-1] != path[-1]:
                continue
            if (len(path) == 1) != (len(other) == 1):
                suspect.append(f"{_label(path)} / {_label(other)}")
                break
    return suspect


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "真假"
    if isinstance(value, int | float):
        return "数字"
    if isinstance(value, list):
        return "清单"
    if isinstance(value, dict):
        return "字典"
    return "文字"


def _reshaped_keys(before: dict[str, object], after: dict[str, object]) -> list[str]:
    """挑出被写成另一种形状的键。

    实测：初始状态给的是 `沈砚 › 灵石 = 3` 和 `["青锋剑", "止血符 ×2", …]`，模型写回
    `"1"` 和 `"青锋剑、止血符 ×4、…"`——数字变文字、清单被拍平成一句话。
    清单一旦拍平就再也没法单独增删其中一项，**且不可逆**。

    只比对类型，不比对内容。形状变化有时是合理的（`受伤: false` → `受伤: "左肩"`），
    所以这是采样标记而非约束。
    """
    suspect = []
    for path in paths_of(after):
        was, now = walk(before, path), walk(after, path)
        # 路径在旧状态里根本不存在（`was is None`）就不算改形状，是新增。
        if was is not None and now is not None and _kind(was) != _kind(now):
            suspect.append(_label(path))
    return suspect


def sampling_flags(
    *,
    delta: StateDelta,
    prose: str,
    density: str,
    deviation: str | None,
    state_before: dict[str, object] | None = None,
) -> list[str]:
    """返回可疑之处。空列表表示这一 run 没有值得优先送审的特征。"""
    flags: list[str] = []

    if bloated := _bloated_keys(delta.set):
        flags.append(f"状态值偏长或串了多件事，可能该拆成几个键：{'、'.join(bloated)}")

    if state_before and (reshaped := _reshaped_keys(state_before, delta.set)):
        flags.append(f"状态值被写成了另一种形状（数字/清单/文字）：{'、'.join(reshaped)}")

    if shadowed := _shadowed_keys(state_before or {}, delta.set):
        flags.append(
            f"新位置与已有位置只差一层归属，可能是同一件事的两个写法：{'、'.join(shadowed)}"
        )

    if delta.is_empty() and len(prose) > _min_chars(density) * 2:
        flags.append("正文不短却没有任何状态变化，可能有遗漏的记录")

    if prose and len(prose) < _min_chars(density) // 2:
        flags.append(f"正文明显短于当前展开密度（{density}）的预期")

    if deviation:
        flags.append(f"writer 报告了偏离：{deviation}")

    return flags
