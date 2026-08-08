"""AI grader：归因五类里**有参照物的那四类**（D59 / D83）。

| 归因 | 参照物 | 这里 |
|---|---|---|
| director 检索不足 | 细纲 vs state | ✔ |
| director 推演错 | 细纲 vs 用户指令 | ✔ |
| writer 偏离细纲 | 正文 vs 细纲 | ✔ |
| 写入遗漏 | 增量 vs 正文 **+ 本轮之前的 state** | ✔ |
| **写入冗余** | 增量 vs **判据本身** | ✔ 补于首批实测 |
| **writer 文风失败** | **无** | **不判**——只能 human |

其中两条是首批实测逼出来的修正：

- **写入遗漏原本只给「增量 vs 正文」，这不够**——不给本轮之前的 state，
  它分不清"漏记"和"早就记过"，实测立刻误报。
- **D59 的五类里没有"记了不该记的"**。写入遗漏只查缺不查多，于是
  ``照片.状态: "柜台，正面朝上"`` 被四类一致判为没问题——而它正是 D95 点名不该记的。

三条纪律：

1. **干净上下文**（P20）。每一类**单独一次调用，只给它的参照物 + 待判对象**，
   不给创作过程、不给其他三类的材料。给全了它会往"整体看没问题"的方向读。
2. **输出归因标签，不输出评分。** 评分无法指导行动，归因可以——五类的修法完全不同。
3. **判不出来就说判不出来。** 解析失败记 ``unknown``，不猜。混进假数据的 grader
   比没有 grader 更糟，它的错误会直接误导后续所有决策。

**grader 自己需要校准**：定期人工抽查一批它的判断。不校准会悄悄漂移。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from ..llm import LLMGateway, Message, Role
from ..models import StateDelta


class Attribution(StrEnum):
    RETRIEVAL = "director 检索不足"
    REASONING = "director 推演错"
    DEVIATION = "writer 偏离细纲"
    WRITE_MISS = "写入遗漏"
    WRITE_NOISE = "写入冗余"
    """D59 的五类里没有这一条，**这是个洞**。

    写入遗漏只查缺不查多，于是"记了不该记的"无人负责——首批实测里
    ``照片.状态: "柜台，正面朝上"`` 被四类一致判为没问题，而它正是 D95 点名
    不该记的东西。冗余的代价是慢性的：state 越长，真正要紧的几条越容易被看漏。

    它的参照物是**写下来的判据本身**（D95/D99），因此仍属"有参照物"那一侧。
    """


class Verdict(StrEnum):
    OK = "ok"
    ISSUE = "issue"
    UNKNOWN = "unknown"


@dataclass
class Judgement:
    attribution: Attribution
    verdict: Verdict
    evidence: str = ""
    hits: int = 1
    """判为"有问题"的次数。"""

    runs: int = 1
    """实际判了几次。``hits / runs`` 是复现率——**随机 grader 的真正信号**（D101）。"""

    @property
    def stable(self) -> bool:
        """过半数才算数。单次命中在这个规模上和噪声分不开。"""
        return self.runs > 1 and self.hits * 2 > self.runs


_FORMAT = """只输出两行，不要任何别的内容：

判定：没问题 或 有问题
证据：一句话。判定为"没问题"时写"无"。"""


def _messages(system: str, body: str) -> list[Message]:
    return [
        Message(role=Role.SYSTEM, content=f"{system}\n\n{_FORMAT}"),
        Message(role=Role.USER, content=body),
    ]


def _render_state(values: dict[str, object]) -> str:
    if not values:
        return "（空）"
    return "\n".join(
        f"- {k}：{json.dumps(v, ensure_ascii=False)}" for k, v in sorted(values.items())
    )


# -- 四类各自的提问 ----------------------------------------------------------

_RETRIEVAL = """你在检查一份剧情细纲有没有和**已知状态**冲突。

已知状态是这一轮开始时世界的样子。判"有问题"的情形只有两种：

- 细纲写的内容与已知状态**直接矛盾**（说某人在场，而状态记着他不在；花掉的钱比他有的多；
  用了正在冷却的手段；让某人说出他并不知道的事）。
- 细纲**无视**了状态里明确写着、且本轮显然相关的约束。

**下面这些不是矛盾**，不要报：

- **知情的人故意装作不知道**——回避、反问、拿一个自己知道答案的问题去换别的东西，
  都是正常的写法。只有当他表现得像是**真的忘了**自己知道，才算矛盾。
- 细纲没有提到状态里的某一项——大部分项本轮本来就用不上。

不要评价写得好不好。**只找矛盾。**"""

_REASONING = """你在检查一份剧情细纲有没有正确接住**用户这一步输入**。

判"有问题"的情形：

- 用户写出的动作、对象、力度、对白、先后关系被**省略**或**换成了别的含义**。
- 用户加引号的原话被改了词。
- 用户角色被**替写了新的动作、对白、决定、心理**（用户没写的）。
- 用户发的是元指令（括号或方括号里的内容，如"跳到第二天"），却被写成了
  用户角色说出口的话，而不是让它作用于世界。

**细纲可以在转述之后继续推进剧情，这是正常的**，不要因此判有问题。
只看用户已经写出的那部分有没有被如实接住。

**换用同一个人或同一个地方的另一种叫法不算换含义**——用户说"钟表铺"、
细纲写"旧货店"，指的是同一家店，这不是问题。只有指向**变了**才算。

**用户写出的是动作，不是结果。** 被世界规则挡下来不算换含义——法术还在冷却、
钱不够、东西已经用完、伤在牵扯、对方先动了手。只要细纲写清了他**确实试过**
以及**为什么没成**，就是对的。只有把动作**换成另一个动作**、或者当作他没做过，
才算问题。"""

_DEVIATION = """你在检查一段正文有没有忠实于它的细纲。

细纲只写**事实**，正文负责**描写**。因此下面这些**都不算偏离**，不要报：

- 增加了描写、感官、心理、环境、比喻、节奏变化。
- 把细纲里转述的对白写成了具体的台词——**措辞、语气、绕不绕全由正文决定**，
  只要话里的信息没变。
- 跳过了细纲末尾"某人在等/等着谁开口"这类句子——那不是事实。

判"有问题"的只有**实质内容对不上**：

- 细纲里的事件、人物、物件、话里的**信息**在正文里消失了。
- 正文发明了细纲里没有的事件、人物、物件或信息。
- **数字、价钱、名字、日期、期限对不上**——这类必须逐一核对。"""

_WRITE_MISS = """你在检查一份状态增量有没有漏记**正文里真的发生了的变化**。

状态是一张速查表，只装**会约束后面能写什么**的东西：谁死了、谁不在场、
谁知道了什么、受了什么伤、还剩多少钱/物、什么还在冷却、答应了谁什么。

判"有问题"：正文里发生了这类变化，而**这一轮之前的状态**里没有、增量里也没有。

**下面这些不算漏记**，不要报：

- 物件的朝向、谁站在哪一侧、气氛、天色——它们约束不了任何后续内容。
- 本轮情节的复述——那是正文的事，不该进状态。
- **这一轮之前的状态里已经有的东西**，哪怕正文又提了一遍。
- 状态里已经有、且本轮没有改变的项。"""

_WRITE_NOISE = """你在检查一份状态增量有没有记**不该记的东西**。

状态是一张速查表，不是第二份记录——事情本身已经记在别处了。一个键要同时满足两条：

1. **它约束后面能写什么。** 不记的话，后面可能写出"不可能发生"的事。
2. **需要随时看见它**，不值得为它去翻前情。

判"有问题"：增量里有过不了这两条的键。典型的几种——

- **约束不了任何东西**：物件的朝向、谁站在柜台哪一侧、灯开着没有、本轮的气氛。
- **本轮情节的摘要**：像"现场.本轮""最近发生"这种每轮被整个覆盖的键。
- **一个键塞了好几件事**：值里串着分号，或者键是个光秃秃的实体名（如"许平"）
  而不是"谁的什么"。
- **记了"未明""不确定""尚未"**——没记的东西本来就是未知的。

**注意存在时间长短不是判据。**"冷却还剩两回合""身上只剩一块灵石"活不过几轮，
但漏了立刻出错，是**该记**的，不要报。

**值为 `null` 是删除这个键，不是"记录未知"。** 删掉不再约束后续的键是**对的**，
是我们鼓励的动作——看到 `null` 一律不要报。

增量为空时判"没问题"——空增量是合法的。"""


_PROMPTS = {
    Attribution.RETRIEVAL: _RETRIEVAL,
    Attribution.REASONING: _REASONING,
    Attribution.DEVIATION: _DEVIATION,
    Attribution.WRITE_MISS: _WRITE_MISS,
    Attribution.WRITE_NOISE: _WRITE_NOISE,
}


def _body(
    attribution: Attribution,
    *,
    user_text: str,
    outline: str,
    prose: str,
    state_before: dict[str, object],
    delta: StateDelta,
) -> str:
    """**只给这一类的参照物 + 待判对象。** 多给一样都会污染判断。"""
    if attribution is Attribution.RETRIEVAL:
        return f"## 已知状态\n{_render_state(state_before)}\n\n## 待检查的细纲\n{outline}"
    if attribution is Attribution.REASONING:
        return f"## 用户这一步\n{user_text}\n\n## 待检查的细纲\n{outline}"
    if attribution is Attribution.DEVIATION:
        return f"## 细纲\n{outline}\n\n## 待检查的正文\n{prose}"

    dumped = delta.model_dump(exclude_defaults=True, mode="json")
    rendered = json.dumps(dumped, ensure_ascii=False, indent=2) if dumped else "（空）"
    if attribution is Attribution.WRITE_NOISE:
        # 只看增量本身对不对得上判据——给正文反而会诱它去论证"这条确实发生了"，
        # 而"确实发生了"和"值得记"是两回事。
        return f"## 待检查的状态增量\n{rendered}"

    # 写入遗漏**必须**看见本轮之前的状态：不给的话它分不清"漏记"和"早就记过"。
    # 首批实测就栽在这——种子里写着"与一个已死之人站在一起"，它仍报了"未记录此人已死"。
    return (
        f"## 这一轮之前的状态\n{_render_state(state_before)}\n\n"
        f"## 正文\n{prose}\n\n## 待检查的状态增量\n{rendered}"
    )


def parse(content: str) -> tuple[Verdict, str]:
    """解析两行输出。**认不出来就是 unknown**，不猜。"""
    verdict = Verdict.UNKNOWN
    evidence = ""
    for line in content.splitlines():
        line = line.strip().lstrip("-* ").strip()
        if line.startswith("判定"):
            body = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            if "没问题" in body or "无问题" in body:
                verdict = Verdict.OK
            elif "有问题" in body:
                verdict = Verdict.ISSUE
        elif line.startswith("证据"):
            evidence = line.split("：", 1)[-1].split(":", 1)[-1].strip()
    return verdict, ("" if evidence in {"无", "None", ""} else evidence)


async def grade_turn(
    gateway: LLMGateway,
    *,
    user_text: str,
    outline: str,
    prose: str,
    state_before: dict[str, object],
    delta: StateDelta,
    model: str | None = None,
    repeat: int = 1,
) -> list[Judgement]:
    """对一轮做归因。**逐类独立调用**，互不见面。

    ``repeat > 1`` 时每类判多次，只保留复现率——**这是随机 grader 唯一可靠的
    读法**（D101）。实测：同一批素材连判三次，跨次稳定复现的三条全是真问题，
    只出现一次的四条全是误报。温度已经设为 0，但它并不能消掉这种波动。
    """
    needs_prose = {Attribution.DEVIATION, Attribution.WRITE_MISS}
    results: list[Judgement] = []

    for attribution, system in _PROMPTS.items():
        if attribution in needs_prose and not prose:
            continue  # 正文缺失（run 失败或被打断）时这两类无从判起

        body = _body(
            attribution,
            user_text=user_text,
            outline=outline,
            prose=prose,
            state_before=state_before,
            delta=delta,
        )
        messages = _messages(system, body)

        hits = 0
        runs = 0
        unknowns = 0
        evidences: list[str] = []

        for _ in range(max(1, repeat)):
            try:
                response = await gateway.complete(messages, model=model, temperature=0.0)
            except Exception as exc:
                unknowns += 1
                evidences.append(f"grader 调用失败：{type(exc).__name__}")
                continue
            verdict, evidence = parse(response.content)
            if verdict is Verdict.UNKNOWN:
                unknowns += 1
                continue
            runs += 1
            if verdict is Verdict.ISSUE:
                hits += 1
                if evidence:
                    evidences.append(evidence)

        if runs == 0:
            results.append(
                Judgement(attribution, Verdict.UNKNOWN, evidences[0] if evidences else "", 0, 0)
            )
            continue

        # 单次判定用"判了就是有问题"；多次判定要过半数——否则噪声会灌进分布。
        is_issue = hits > 0 if runs == 1 else hits * 2 > runs
        results.append(
            Judgement(
                attribution,
                Verdict.ISSUE if is_issue else Verdict.OK,
                "；".join(dict.fromkeys(evidences)),
                hits,
                runs,
            )
        )
    return results
