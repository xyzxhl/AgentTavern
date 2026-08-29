"""rerank：复用 chat 模型，但它是一次**独立调用**（S141）。

三条缺一不可——

1. **独立调用。** 只看 [query + 候选段落]，不带 director 那一轮的任何东西。
   接在它的对话后面的话，模型会读到整轮推演，于是「哪一段最相关」变成
   「哪一段最符合我已经想好的走向」——那是自我确认（P20）。
2. **独立的缓存空间。** 静态前缀跨会话完全相同，独占一个空间能最大化复用；
   混进 session 的空间只会**污染那边的命中率**（P29 栽过一次）。
3. **排序，不打分。** 打分是开放判断，排序是受约束的选择（P10）。

**它是锦上添花的一步。** 坏掉时整条链路退化成"没 rerank"，而不是让检索失败——
所以解析一律宽容，读不懂就退回原顺序。

**但它只能重排，不能丢。** 模型漏掉的候选接在后面：那一段本来通过了阈值，
是模型排序时忘了提，不是它不相关。丢掉是一次静默的降级（P34）。
"""

from __future__ import annotations

import re
from typing import Protocol

from ..llm import Message
from ..prompting import assemble, rendering
from .search import Hit

RERANK_CACHE_SPACE = "rerank"
"""它自己的缓存空间。**不是 session 的 id**（S141）。

跨会话共用一个空间是有意的：这一步的静态前缀处处相同，共用能最大化命中，
而它本来就不属于任何一次对话，没有什么可被污染的。
"""

_NUMBER = re.compile(r"\d+")


class GatewayLike(Protocol):
    async def complete(self, messages: list[Message], *, user_id: str | None = None): ...  # type: ignore[no-untyped-def]


async def rerank_passages(
    gateway: GatewayLike,
    *,
    query: str,
    candidates: list[Hit],
    limit: int,
) -> list[Hit]:
    """把候选按相关度重排，取前 `limit` 条。"""
    if len(candidates) < 2:
        # **一条候选不发这次调用**：排序一条东西没有意义，而它要花钱。
        return candidates[:limit]

    header = rendering("rerank", "request_header").format(query=query)
    body = "\n".join(
        rendering("rerank", "passage").format(index=i, text=h.text)
        for i, h in enumerate(candidates)
    )
    prompt = assemble("rerank", contexts={"rerank_request": f"{header}\n\n{body}"})

    response = await gateway.complete(prompt.messages, user_id=RERANK_CACHE_SPACE)
    order = _parse_order(str(response.content), len(candidates))
    return [candidates[i] for i in order][:limit]


def _parse_order(reply: str, total: int) -> list[int]:
    """从回复里抽出序号。**宽容解析，绝不丢候选。**

    - 越界、重复的序号忽略——模型时常多写一个数字。
    - 一个有效序号都没有 → 退回原顺序（rerank 坏掉不该让检索失败）。
    - 漏掉的候选按原顺序接在后面（丢掉是静默降级，P34）。
    """
    seen: list[int] = []
    for token in _NUMBER.findall(reply):
        i = int(token)
        if 0 <= i < total and i not in seen:
            seen.append(i)
    if not seen:
        return list(range(total))
    seen.extend(i for i in range(total) if i not in seen)
    return seen
