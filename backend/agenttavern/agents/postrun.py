"""生成后追问：本轮产出已定，再问 director 几件**与产出无关**的事。

**工具分两类，按"影不影响本轮产出"划**（S22）：

- **生成中**：`state_query`、`worldinfo_read`、`history_read`、`ask_question`
  ——它们改变这一轮写出什么。
- **生成后**：归段（`close_segment`）——它只影响**以后**怎么回看这一轮。

生成后的动作绝不能放进 tool loop：它对本轮产出毫无帮助，却要占掉往返、
分散注意力；director 输出细纲之后，本轮产出已经确定（S174）。

但它**必须问 director 本人**，而不是另起一个冷调用。这一段收没收是语义判断，
判据是"这场戏完了没有"——刚写完的 director 手里有 state、有前情、知道故事
要往哪走；一个只看得见几段正文的调用，判据全靠猜。

追问接在 director 自己的对话后面，因此几乎不花额外的输入 token。

## 答案走工具调用，不走自由文本

以前是「回一个数字」，代码从回复里抓第一串数字。**那是在自由文本里找结构**：
模型多写一句「我认为断在第 2 条」，抓到的就是 2 之外的某个数；写一句
「这 3 轮还没完」，抓到的是 3。而抓错的后果**不可逆且不出声**——
标记打在没人指的那一条上，之后那几轮被压成一段，没有任何地方会报错。

`close_segment(after)` 收一个 typed int，**不调用就是「先别断」**。
"""

from __future__ import annotations

from collections.abc import Sequence

from ..llm import LLMGateway, Message, ToolCall
from ..models import Actor, TraceEventType
from ..prompting import call_of, special_messages
from ..tools import close_segment_tool
from ..tracing import Tracer

CLOSE_SEGMENT_TOOL = "close_segment"


def parse_cut(calls: Sequence[ToolCall], open_runs: int) -> int:
    """从这次回复里读出分界位置。**没调工具就是 0**（当作"还没完"）。

    段落一压成摘要就不可逆，宁可晚一轮，也不要断在模型没真正给出的位置上。
    所以这里一律从严：不是那个工具、参数缺了、不是个整数——**全都算 0**。
    上界照旧夹住：它指到第 12 条而只有 3 条待归段时，取 3。
    """
    for call in calls:
        if call.name != CLOSE_SEGMENT_TOOL:
            continue
        raw = (call.arguments or {}).get("after")
        if isinstance(raw, bool) or not isinstance(raw, int):
            # 有些模型把整数参数写成字符串。**只认干净的十进制**，
            # 认不出就当没调——同上面那条，宁可晚一轮。
            if not (isinstance(raw, str) and raw.strip().isdigit()):
                return 0
            raw = int(raw.strip())
        return max(0, min(raw, open_runs))
    return 0


async def ask_segment_cut(
    gateway: LLMGateway,
    messages: list[Message],
    open_prose: list[str],
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

    **问题的正文在 `director.yaml` 的 `specials.segment` 里**（R31）。它以前
    在这个文件里另存了一份，于是 YAML 那一份是死字——用户改完什么都不会发生，
    而两份必然对不上（同 S27）。
    """
    open_runs = len(open_prose)
    if open_runs == 0:
        return 0

    # 归段是语义判断，结尾恰恰是最重要的证据。以前每轮只给前 120 字，模型会把
    # 正常预览当成“句中截断”，也永远看不见场景在末尾已经收束。正文必须完整给出；
    # 超出模型上下文时让调用明确失败，不能靠静默截断换一个必然偏向“不分段”的答案。
    listing = "\n\n".join(f"{i + 1}. {p}" for i, p in enumerate(open_prose))
    asked = special_messages(
        "director",
        "segment",
        values={"open_runs": str(open_runs), "listing": listing},
    )
    if not asked:
        # 这一节关掉了。**判断这件事的地方只有一个**——run.py 那边已经拦过，
        # 这里是第二道，因为"关掉了却仍然发了一次调用"是要花钱的那种错。
        return 0

    followup = [*messages, *asked]
    # 追问接在 director 自己的对话后面，**必须用同一个 user_id**，
    # 否则它落在另一个缓存空间里，整份前缀白付一次。
    # **追问用 director 自己那份旋钮**：它接在 director 的对话后面，
    # 换个模型等于换了个人来答这道题。
    response = await gateway.complete(
        followup,
        tools=[close_segment_tool().spec()],
        call=call_of("director"),
        user_id=user_id,
    )
    tracer.exchange(Actor.ORCHESTRATOR, followup, response)
    cut = parse_cut(response.tool_calls, open_runs)

    # **用 ORCHESTRATOR 而不是 DIRECTOR**：这是系统维护性的追问，不是创作轮次。
    # 记成 director 的 model_response 会被指标当成"只回文本没调工具"的 nudge。
    tracer.emit(
        TraceEventType.CROSS_CHECK,
        Actor.ORCHESTRATOR,
        f"追问归段：{'断在第 ' + str(cut) + ' 条之后' if cut else '先不断'}（共 {open_runs} 条）",
        {"cut": cut, "open_runs": open_runs, "called": [c.name for c in response.tool_calls]},
        usage=response.usage,
    )
    return cut
