"""summarizer：摘要生成（D69）。

**摘要由系统维护，不由 director 顺手做。**

`summarize` 曾只是一个模型可调的工具，实测 12 轮下来**一次都没被调用**——
原因不是它不好用，是**它的收益全在未来的轮次**：这一轮花 token，好处归下一轮。
一个只对后续有利的动作，优化当前轮的模型永远不会主动去做。

后果比"没摘要"更糟：索引回退到"前 N 字"，而标签仍写着「总结」。这带来
**位置偏置**——细纲开头的东西原样保留，结尾的东西完全消失，保留什么取决于
它碰巧在哪个位置，与重要性无关。模型看着"总结"二字，自然不会想去展开全文。

因此改为：**节点滑出全文窗口时，由编排层补上摘要。** 它是系统维护，不是创作决策；
仍然是懒惰的（只在真的要用摘要时才生成）、仍然缓存在节点上（D69 的两条性质不变），
只是把触发点从"模型想起来"换成了"确实需要了"。
"""

from __future__ import annotations

from ..config import LIMITS
from ..llm import LLMGateway
from ..prompting import assemble

SUMMARY_MAX_CHARS = LIMITS.segment.summary_max_chars


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
    max_chars: int = SUMMARY_MAX_CHARS,
    user_id: str | None = None,
    *,
    enabled: bool = True,
) -> str:
    """把一段内容压到 ``max_chars`` 以内。

    **压缩判据是"可不可再生"，不是"重不重要"。** 后者是事后才知道的——
    写摘要的那一刻，无从知道台账上那行日期以后会成为关键（D106 的交集盲区）。
    而具体项占的字数很少、丢了不可再生；概括项占字数多、随时能从事实里重新读出来。

    判据本身写在 `summarizer.yaml` 里，这里只装配（D140）。``enabled=False`` 时
    那一档是空串，指示整条不发——留这个开关是为了能从外部关掉，不是推荐这么做。
    """
    prompt = assemble(
        "summarizer",
        contexts={"content": content},
        values={"max_chars": str(max_chars)},
        options={"compression": "on" if enabled else "off"},
    )
    response = await gateway.complete(prompt.messages, user_id=user_id)
    return response.content.strip()
