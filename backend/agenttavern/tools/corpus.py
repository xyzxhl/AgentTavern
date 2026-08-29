"""`corpus_search`：在用户导入的外部资料里查一段原文（S139/S145/S146）。

**回执只有两种形状**，这是这个工具的全部契约：

| | 说什么 |
|---|---|
| **找到了** | 那几段原文 + 各自在哪份资料的第几字 |
| **这次没找到** | 一句「这次没找到」+ 用的 query |

**没有第三种。** 特别是**不附「这可能不全，仅供参考」**（S139）——那是免责声明，
不是机制：模型会忽略它、它每次都占上下文、而且它把判断推回给了模型，
正是 [P10] 说的反面。系统能算出这次匹配够不够强，就该算完再给结论。

**措辞上有一条硬线**：没找到时说的是「**这次检索没找到**」，不是「资料里没有」。
前者是系统对自己这次动作的陈述，后者是对资料的断言——而这条链路**没有资格下
那个断言**：能不能查到取决于 query 问得对不对，这是原理上保证不了的事。
所以那句话住在 `director.yaml` 的 `rendering` 里（R31，用户可改），
而**代码这一侧一个字都不拼**。

**降级不进回执**（S140）：「这次只用了关键词匹配」改变不了模型下一步做什么，
只会多占上下文。它进 trace 和界面——**用户看到它才知道该去配 embeddings**。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..corpus.lookup import Lookup, look_up
from ..prompting import rendering
from .base import Tool, ToolResult
from .builtin import _tool
from .context import RunContext


class CorpusSearchParams(BaseModel):
    query: str = Field(description="要查什么。用资料里可能出现的说法，不是问句。")


async def _corpus_search(ctx: RunContext, params: CorpusSearchParams) -> ToolResult:
    sources = ctx.repo.corpus_sources()
    # 挂着但还没建好的那几份**要说出来**（S144）：静默略过会被读成「查过了，
    # 没有」，而它其实压根没参加这次检索。
    ready = {s.corpus_id for s in sources}
    waiting = [c.name for c in ctx.repo.corpora() if c.corpus_id not in ready]

    lookup = await look_up(sources, query=params.query, gateway=ctx.gateway, embedder=ctx.embedder)
    return ToolResult(
        content=_receipt(lookup, waiting),
        label=_label(lookup),
        payload=_payload(lookup, waiting),
    )


def _receipt(lookup: Lookup, waiting: list[str]) -> str:
    """回给模型的字。**每一句都来自 `director.yaml` 的 `rendering`**（R31）。

    代码只决定"说哪一句"和"填什么值"，一个字都不自己拼——否则用户在界面上把
    这几句改完，模型收到的仍然有一半是写死在这里的。
    """
    parts: list[str] = []
    if lookup.passages:
        parts.append(rendering("director", "corpus_hit", count=len(lookup.passages)))
        parts += [
            rendering(
                "director",
                "corpus_passage",
                name=p.corpus_name,
                start=p.start,
                end=p.end,
                text=p.text,
            )
            for p in lookup.passages
        ]
    else:
        parts.append(rendering("director", "corpus_miss", query=lookup.query))

    if waiting:
        parts.append(rendering("director", "corpus_building", names="、".join(waiting)))
    return "\n\n".join(parts)


def _label(lookup: Lookup) -> str:
    """给界面看的一句话（S111）。**它不进模型的上下文**，所以留在代码里。"""
    if lookup.passages:
        return f"查资料「{lookup.query}」：{len(lookup.passages)} 段"
    return f"查资料「{lookup.query}」：这次没找到"


def _payload(lookup: Lookup, waiting: list[str]) -> dict[str, Any]:
    """进 trace 的全部细节（S146）：两路各自的候选、rerank 前后的次序、
    最终返回的几段及其位置，外加**降级**——回执里没有的那一样。
    """
    return {
        "corpus": {
            **lookup.stages,
            "returned": [
                {
                    "corpus_id": p.corpus_id,
                    "name": p.corpus_name,
                    "chunk_index": p.chunk_index,
                    "start": p.start,
                    "end": p.end,
                }
                for p in lookup.passages
            ],
            "degraded": lookup.degraded,
            "not_ready": waiting,
        }
    }


def corpus_tool(agent: str = "director") -> Tool[Any]:
    """**只给 director**（S137）。writer 一个工具都没有，state_manager 与
    goal_manager 都不碰资料（S138）——所以这个工厂只在 `build_tools` 里被叫到。
    """
    return _tool(agent, "corpus_search", CorpusSearchParams, _corpus_search)


__all__ = ["CorpusSearchParams", "corpus_tool"]
