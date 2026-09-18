"""Session 元指令与模式。

**「配置」这一层已经没了**（R31/S80）。曾经这里有三类东西：元指令（改变目标）、
控制维度（改变处理方式）、能力开关（系统能做什么）。后两类**整体上调到了提示词
层**——它们全都在改「模型收到什么」，那就该和提示词住在同一份文件里，而不是散在
session / worldbook / 服务三个层级：

| 原来在这里 | 现在在哪 |
|---|---|
| `ControlDimensions`（输入处理、抢话、文风、视角、铺陈） | agent YAML 里 `choice` 的 `selected` |
| `ControlOverride`（单轮覆盖） | **删掉**：没有被覆盖的对象了 |
| `Capabilities.nudge` / `summarize` | director 的 `specials.nudge` / `.segment` 的 `enabled` |
| `Capabilities.state` | `state_manager.yaml` 顶上的 `enabled`（这个 agent 跑不跑） |
| `Capabilities.state_query` / `worldinfo_read` / `history_read` 等 | 各自那个工具的 `enabled` |
| `Capabilities.upstream_runs` / `state_recent_runs` | 对应那条 `context` 上的 `runs` |
| `Capabilities.stream` / `trace` | **`Settings`**（服务级，关于交付与运维，不关于能力） |

规律是**几乎每一项都落成「某个具体条目上的开关或参数」**——这本身就是它们该
归 agent 管理的证据：它们从来不是独立的配置，是某条提示词、某个工具、某段
context 的属性。

于是 session 上只剩**内容**：标题、目标、节点树。尚未形成需求的触发模式不在
schema 里占位；`time_driven` 已退回 Ideas。

详见 docs/design/Configuration.md。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from ..llm.types import Message

# --------------------------------------------------------------------------
# Session 元指令
# --------------------------------------------------------------------------


class Goal(BaseModel):
    """长任务终止条件（R24）。

    机制：正常一轮生成结束后检查是否达标，未达标则开启下一 run。

    **goal 不是模式**——有无 goal 系统跑法一样，只是 run 末多一次 goal_manager 的
    调用。达标判定一律交给模型，不降级为代码：那些结构化字段的值本就是模型写的，
    代码读取比对不是独立验证（P11）。
    """

    description: str
    """用户设定的可判定标准。表述越具体，模型判定越准。"""

    max_runs: int | None = None
    """硬上限保底——模型可能永远判不达标，或目标本身不可达。

    **`None` = 不设上限**（S97）。以前留空会被填成一个默认值（50），那等于替
    用户做了决定：他没填，不是"随便给个数"，是"别拦我"。想要止损的人本来就会
    填一个数；不填的人被一个自己没见过的数字在第 50 轮拦下来，只会以为出了故障。
    """

    max_total_tokens: int | None = None
    """硬上限保底（同上，`None` = 不设上限）。

    **两条预算都可以在 goal 跑着的时候调大**——那就是"加预算继续"（S96）。
    `description` 不行：对话是围着它建起来的，改了等于换了个目标。
    """

    # `max_revisions` **已删除**（S93）。它当初的理由是「重新规划这条路没有出口，
    # 模型永远可以再拆一层」。但重拆现在不是「又失败了一次」，是**脚手架跟着剧情
    # 走**——subgoal 只是让模型先想清楚路径，不是要逐项打勾的执行契约。
    # 留着它反而会在剧情正常波动几次之后误判 failed。止损交给上面两条预算：
    # 重拆本身不花钱，跑 run 才花钱。


class SubGoal(BaseModel):
    """一个**中间状态**。goal_manager 给出来的，不是用户写的。

    **它描述状态，不是步骤**，而且彼此之间**没有先后依赖**（S93）：可以被跳过、
    以不同顺序实现、同时实现，也可能因剧情变化而不再必要。所以这个列表是一组，
    不是一条链——序号只用来指代，不表示顺序。
    """

    description: str
    done: bool = False


class GoalOutcome(StrEnum):
    """goal 的**终态，只有两种**（S96）。

    `REPLAN` 不再是一种去向——**它是一个动作，不是结局**：任何一轮发现剩下的
    阶段失效了，就地重拆即可。`DERAILED` 并进了 `FAILED`。

    预算耗尽**不是终态**，是暂停（`GoalRun.paused`）：用户可以加预算接着跑。
    """

    DONE = "done"
    """完成。交回控制权。"""

    FAILED = "failed"
    """失败：**当前局面下拆不出任何能到达目标的路径**。

    判据要比「这一版计划没走通」重得多——那种情况该重拆。只有重拆也拆不出路时
    才是 failed。同样交回控制权，但用户需要知道是哪一种：一个是达成了，
    一个是这个目标已经不可能了，后续动作完全不同。
    """


class GoalPlan(BaseModel):
    """把 goal 拆成的一串阶段（S93）。

    **它们是脚手架，不是执行契约。** 存在的目的是逼模型在动手之前先想清楚差距
    在哪；某一条最终有没有原样实现并不要紧——剧情变了就重给一版。因此没有
    「第几版」这种计数，也没有次数上限。

    但 `finish(done)` **要求全部标为成立**。这两条不矛盾，合起来才是要点：
    **重给随时可以，而宣布完成之前必须用「当前的」那一版重核一遍**，不是照着
    三轮前那份打勾。

    **它挂在 session 上**（`GoalRun.plan`），不再挂节点。S104 让它跟着节点走是
    为了分支隔离；goal 跑着的时候不允许切分支（S104），那个理由不成立了。
    """

    subgoals: list[SubGoal] = Field(default_factory=list)

    # `current`（第一个未达成的那个）**已删除**（S93）：中间状态之间没有先后
    # 依赖，挑出一个说"当前这个"等于凭空造了个顺序。要看还剩什么就看全部未成立的。

    @property
    def all_done(self) -> bool:
        return bool(self.subgoals) and all(s.done for s in self.subgoals)

    def render(self) -> str:
        lines = [
            f"{i + 1}. [{'已成立' if s.done else '未成立'}] {s.description}"
            for i, s in enumerate(self.subgoals)
        ]
        return "\n".join(lines)


class GoalRun(BaseModel):
    """一次 goal 从创建到结束期间的**运行时状态**（S92）。

    和 `SessionMeta.goal` 分开：那个是**用户配置**（目标是什么、给多少预算），
    这个是**跑起来之后的东西**（对话、进度、花销）。混在一起的话，"用户改了什么"
    和"系统跑出了什么"就分不开，而前者要禁改、后者每轮都在变。

    **它挂在 session 上，不挂节点。** goal 跑着的时候不允许切分支，所以 S104 让
    `GoalPlan` 跟着节点走的那个理由（分支隔离）不再成立。
    """

    conversation: list[Message] = Field(default_factory=list)
    """goal_manager 的产品记录，**跨 run 持续到这个 goal 结束**。

    静态前缀（任务说明 + 目标 + 节奏）在整个 goal 生命周期里不变，之后每轮只
    追加一条「第 N 轮发生了什么」。模型的 reasoning 与 tool call/result 不写回；
    当前生效计划在每次调用前由 ``plan`` 投影注入。
    """

    plan: GoalPlan | None = None
    """当前计划与进度。**权威在这里，不在对话里。**

    对话是过程，这份是状态（同 `staged_delta` 的性质）。预算判断、UI 展示、
    恢复时的重建都读它——从对话里反解析既慢又脆。
    """

    spent_runs: int = 0
    spent_tokens: int = 0
    """已经花掉的。**落盘**——它们以前是 `SessionRunner` 的实例字段，
    进程重启就归零，于是 R24 那个用来兜住自主运行成本的上限实际上不生效。
    """

    paused: bool = False
    paused_reason: str = ""
    """停下了，但 goal 还在。两个来源：用户主动暂停，或预算耗尽。

    **预算耗尽不是失败**（S96）：用户可以把上限调大接着跑，所以它是暂停而不是
    终态。合成同一个状态是因为对用户而言它们是同一件事——停下了，要不要继续。
    """

    started_at_leaf_id: str | None = None
    """goal 是在哪个节点之后设的。**它把路径切成两段**（S93）：

    - **它之前**——goal 还不存在时写出来的剧情，进静态前缀，给**细纲**
      （摘要策略和 director 一样：归过段就用摘要，否则头部截断）。
    - **它之后**——goal 运行期间的每一轮，逐条追加，给**正文**。

    分开是因为两段要回答的问题不同：前面那段是"故事到这儿了"，细纲够用而且便宜；
    goal 期间要判的是"这一步到底有没有发生"，而**细纲只说定了什么，正文才是
    最终写出来的东西**——留白处落成了什么、话说到什么份上，只有正文看得见。

    ``None`` 表示这个 goal 从会话一开始就在，整条路径都算 goal 期间。
    """

    seen_leaf_id: str | None = None
    """对话已经追加到哪个节点为止。运行期间靠它做增量追加。

    恢复时**不靠它续，而是整份重建**：暂停期间用户可能自己跑了几轮、还可能切了
    分支，而「以最终选择的分支为准」——增量追加撤不回来，重建才做得到。
    """

    def exhausted(self, goal: Goal) -> str | None:
        """预算到顶了没有。到了返回一句人能读的理由，没到返回 ``None``。

        **没设的那条不参与判断**（S97）：`None` 是"不设上限"，不是"上限为零"。
        两条都没设时它永远返回 ``None``——那正是用户要的，止损交给暂停和放弃。
        """
        if goal.max_runs is not None and self.spent_runs >= goal.max_runs:
            return f"已跑满 {goal.max_runs} 轮"
        if goal.max_total_tokens is not None and self.spent_tokens >= goal.max_total_tokens:
            return f"已用掉 {self.spent_tokens} token（上限 {goal.max_total_tokens}）"
        return None


class SessionMeta(BaseModel):
    """**这局故事**自己的元指令。定义目标，不定义过程。

    这里以前还装着题材与基调、尺度、角色语言习惯、用户扮演谁——**那些是世界
    自己的属性，已经搬进 `WorldMeta`**（S63）。搬家的依据是这个类原本就写着的
    判别式「换个人来读还成不成立」：它同样答得出「换局故事还成不成立」，而答案
    也是成立。当时没搬，是因为 worldbook 还不是独立的东西（S68：一份 worldbook
    开启一个 session），"世界的属性"和"故事的属性"没有区别的余地。

    文风和视角两样也不在这儿：**它们上调到了 writer.yaml**（R31），
    和铺陈程度一样是一个 `choice` 的 `selected`。

    **没有「否定项」字段**（S91）。不能提前揭示的东西属于世界设定，写进
    worldinfo 或 state；「不许写什么」这类写作纪律属于提示词，写进 YAML。
    单立一个字段等于给同一件事开第三个入口，而它两侧都没人注入。
    """

    title: str = "未命名"
    """这局故事叫什么。**世界的名字在 `WorldMeta.name`**——同一个世界底下
    可以有好几个故事，两者必须分得开。"""

    goal: Goal | None = None
