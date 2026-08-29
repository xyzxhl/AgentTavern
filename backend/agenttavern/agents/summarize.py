"""summarize：把一段剧情压缩（S25）。

**它不是 agent，是一次单次调用**（S147）——所以名字是个动词。输入固定、输出
固定、不参与剧情：做完就没了，没有"它是谁"这回事。它住在 `agents/` 下面只是
因为调用点在这一层，别把它当第五个 agent 看。

**摘要由系统维护，不由 director 顺手做。**

`summarize` 曾只是一个模型可调的工具，实测 12 轮下来**一次都没被调用**——
原因不是它不好用，是**它的收益全在未来的轮次**：这一轮花 token，好处归下一轮。
一个只对后续有利的动作，优化当前轮的模型永远不会主动去做。

后果比"没摘要"更糟：索引回退到"前 N 字"，而标签仍写着「总结」。这带来
**位置偏置**——细纲开头的东西原样保留，结尾的东西完全消失，保留什么取决于
它碰巧在哪个位置，与重要性无关。模型看着"总结"二字，自然不会想去展开全文。

因此改为：**节点滑出全文窗口时，由编排层补上摘要。** 它是系统维护，不是创作决策；
仍然是懒惰的（只在真的要用摘要时才生成）、仍然缓存在节点上（S25 的两条性质不变），
只是把触发点从"模型想起来"换成了"确实需要了"。
"""

from __future__ import annotations

from ..config import LIMITS
from ..llm import LLMGateway
from ..models import Actor
from ..prompting import assemble
from ..tracing import Tracer

MAX_SEGMENT_RUNS = LIMITS.segment.max_runs
"""一段最多覆盖几轮。到顶就强制断开——**不能让"还没结束"无限拖下去**。

它不是语义判断，是止损：真有一段剧情写了六轮还没完，那也该先压一半，
否则缓冲区里的东西永远等不到摘要，索引就一直退化在头部截断上。
"""

MIN_SEGMENT_RUNS = LIMITS.segment.min_runs
"""一段最少几轮。**允许单轮成段**——一次干脆的转场本来就是一段，没理由硬凑。

早先设成 2，理由是"不值得为一轮花一次调用"。那个理由在冷调用的方案下成立，
在生成后追问的方案下不成立：追问接在 director 自己的对话后面，几乎不花额外输入。
"""


async def summarize_text(
    gateway: LLMGateway,
    content: str,
    user_id: str | None = None,
    *,
    enabled: bool = True,
    tracer: Tracer | None = None,
) -> str:
    """把一段内容压缩。

    **没有字数上限**（S25）。曾经有一个（`segment.summary_max_chars: 200`，
    写进提示词的"压缩到 N 字以内"），实测**它基本不起作用，而且没人发现**：
    48 条落盘摘要里 16 条超标，最坏 779 字 / 3.9 倍，且**超出幅度随输入长度增长**
    ——细纲 800 字时超 20%，细纲 1250 字时超到近 4 倍。一个说了不算的数比没有
    更糟：它让"压缩到 200 字"看起来是既成事实，于是 `budget_chars` 那套账全是虚的。
    现在改成**只统计不约束**：长度照旧量、照旧进 trace 和采集报告，但不再假装它被限住。

    **压缩判据是"可不可再生"，不是"重不重要"。** 后者是事后才知道的——
    写摘要的那一刻，无从知道台账上那行日期以后会成为关键（S56 的交集盲区）。
    而具体项占的字数很少、丢了不可再生；概括项占字数多、随时能从事实里重新读出来。

    判据本身写在 `summarize.yaml` 里，这里只装配（S10）。**选哪一档也在那儿**
    （R31：`selected` 住在条目上）——`off` 那一档是空串，整条不发。留这个档位
    是为了能从外部关掉，不是推荐这么做。

    ``enabled=False`` 是**调用方这一侧的短路**（比如整条摘要链路都不想跑），
    和 YAML 里选了 `off` 是两回事：前者连这次调用都不发生。
    """
    if not enabled:
        return ""
    prompt = assemble("summarize", contexts={"content": content})
    response = await gateway.complete(prompt.messages, user_id=user_id)
    # 开发者模式下这一次往返也要留下原文（S113）。摘要是**不可逆**的——一段压完
    # 原细纲就不再进上下文了，压坏了事后只能从这里看出是压的时候丢的还是本来就没有。
    if tracer is not None:
        tracer.exchange(Actor.ORCHESTRATOR, prompt.messages, response)
    return response.content.strip()
