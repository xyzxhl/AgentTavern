"""生成后追问：本轮产出已定，再问 director 几件**与产出无关**的事。

**工具分两类，按"影不影响本轮产出"划**（S22）：

- **生成中**：`state_query`、`worldinfo_read`、`history_read`、`ask_question`
  ——它们改变这一轮写出什么。
- **生成后**：归段（`summarize`）——它只影响**以后**怎么回看这一轮。

生成后的动作绝不能放进 tool loop：它对本轮产出毫无帮助，却要占掉往返、
分散注意力，还会和 `submit_outline` 的终止语义打架（S4）。

但它**必须问 director 本人**，而不是另起一个冷调用。这一段收没收是语义判断，
判据是"这场戏完了没有"——刚写完的 director 手里有 state、有前情、知道故事
要往哪走；一个只看得见几段细纲的调用，判据全靠猜。

追问接在 director 自己的对话后面，因此几乎不花额外的输入 token。
"""

from __future__ import annotations

from ..llm import LLMGateway, Message, Role
from ..models import Actor, TraceEventType
from ..tracing import Tracer

_CLOSE_SEGMENT = """本轮已经提交，下面这个问题**与本轮内容无关**，只影响以后怎么回看。

下面这 {open_runs} 轮还没有归段（按顺序编号，最后一条就是你刚写完的）：

{listing}

**如果要把它们断成一段，断在第几条之后？**

一段是一个完整的场景、一件完整的事。**它通常覆盖好几轮**——一场对峙、
一次外出、一笔交易谈完，都要几轮才走得完。判断的依据：换了地方、隔了时间、
一件事有了结果、一场对话双方都没有下一句要说。

**"刚才有件事结束了"不算。** 每一轮都会有什么东西告一段落，那是情节的节奏，
不是段落的分界。

回一个数字：

- `0` —— 这几轮还是同一件事的中途，先别断。**拿不准就回 0。**
- `k`（1 ≤ k ≤ {open_runs}）—— 第 1 到第 k 条合成一段，第 k+1 条起属于下一段。
  **k 可以小于 {open_runs}**：分界常常要等到后面一轮才看得出来，
  如果真正的分界在前面几条，就断在那里。

只回这个数字。"""


def parse_cut(content: str, open_runs: int) -> int:
    """解析分界位置。**认不出来回 0**（当作"还没完"）。

    段落一压成摘要就不可逆，宁可晚一轮，也不要断在模型没真正给出的位置上。
    """
    digits = ""
    for ch in content.strip():
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    if not digits:
        return 0
    return min(int(digits), open_runs)


async def ask_segment_cut(
    gateway: LLMGateway,
    messages: list[Message],
    open_outlines: list[str],
    tracer: Tracer,
    *,
    model: str | None = None,
    user_id: str | None = None,
) -> int:
    """问刚写完的 director：这几轮该断在第几条之后。0 表示先别断。

    问法改过四版，**决定性的差别是问题的形式，不是它看得见什么**（S58）：

    - 批量冷调用问"断在第几轮之后"：查准 1.00、查全 0.67。
    - 逐轮问"这一轮是不是结尾"（无对照 / 有对照 / 持续对话）：
      查准 0.25 → 0.50 → 0.33，查全一直 0.33。

    **分界是相对判断，而二值问法装不下它**——它只能对当前轮独立表态，
    没法说"不是这里，是两轮前"。加多少对照信息都补不回来，因为答案的形状不对。

    位置问法同时保住了时机：在生成后问，揭示分界的那一轮就在手里；
    而回答可以**回溯**到前面几条，正是逐轮问法漏掉的那类分界。
    """
    open_runs = len(open_outlines)
    if open_runs == 0:
        return 0

    listing = "\n".join(f"{i + 1}. {o[:120]}" for i, o in enumerate(open_outlines))
    followup = [
        *messages,
        Message(
            role=Role.USER,
            content=_CLOSE_SEGMENT.format(open_runs=open_runs, listing=listing),
        ),
    ]
    # 追问接在 director 自己的对话后面，**必须用同一个 user_id**，
    # 否则它落在另一个缓存空间里，整份前缀白付一次。
    response = await gateway.complete(followup, model=model, user_id=user_id)
    tracer.exchange(Actor.ORCHESTRATOR, followup, response)
    cut = parse_cut(response.content, open_runs)

    # **用 ORCHESTRATOR 而不是 DIRECTOR**：这是系统维护性的追问，不是创作轮次。
    # 记成 director 的 model_response 会被指标当成"只回文本没调工具"的 nudge。
    tracer.emit(
        TraceEventType.CROSS_CHECK,
        Actor.ORCHESTRATOR,
        f"追问归段：{'断在第 ' + str(cut) + ' 条之后' if cut else '先不断'}（共 {open_runs} 条）",
        {"cut": cut, "open_runs": open_runs, "raw": response.content.strip()[:40]},
        usage=response.usage,
    )
    return cut
