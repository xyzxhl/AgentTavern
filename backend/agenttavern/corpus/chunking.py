"""定长滑窗切块（S142）。

**不做语义切分。** 那要模型参与——又一次调用、又一层不确定性，而这条链路的
检索质量还一次都没量过。P27：先量方差，再改东西。定长切块是那个基线。

**重叠不是可选的**：不重叠时，正好跨在边界上的那句话被切成两半，
两半各自都不足以命中，**于是那句谁也检索不到**——而且没有任何地方会报错。
"""

from __future__ import annotations

from ..models.corpus import Chunk


def chunk(text: str, *, size: int, overlap: int) -> list[Chunk]:
    """把一段文本切成带重叠的定长块。

    每块记住它在原文里的区间，`text[start:end]` 必须还原出该块——
    检索结果要指得回原文，用户才判得出准不准（R42）。
    """
    if size <= 0:
        raise ValueError("块长必须为正")
    if overlap < 0:
        raise ValueError("重叠不能为负")
    if overlap >= size:
        # **不静默死循环**：步长 = size - overlap，一旦 ≤ 0 就永远推不动，
        # 症状是导入一份资料之后进程卡住，而错误离原因很远。
        raise ValueError(f"重叠（{overlap}）必须小于块长（{size}），否则切块推不动")

    if not text:
        return []

    step = size - overlap
    out: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        out.append(Chunk(index=len(out), start=start, end=end, text=text[start:end]))
        if end == len(text):
            break
        start += step
    return out
