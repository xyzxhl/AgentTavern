"""递归字符切分（S142）。

依次试一组分隔符，**第一个能把这块切到限长以内的就用它**；一路退到按单个字符。
切完再贪心合并回接近限长的块。

**为什么不是定长硬切**：那会从句子中间下刀，切出来的两半各自都不成话。
BM25 那一路尤其吃亏——一个专名被劈成两半，两边都命中不了，
而这条链路要解决的恰恰是专名与术语。

**为什么不用模型做语义切分**：那是又一次调用、又一层不确定性，
而这条链路的检索质量还一次都没量过（P27：先量方差，再改东西）。
递归字符切分是"不花模型的钱就能拿到的那部分语义切分"。

**全程按 (start, end) 区间操作，不传字符串。** 每块必须记得它在原文里的位置——
检索结果要指得回原文，用户才判得出准不准（R42）。拿字符串一路传下去，
到最后就只能靠 `find()` 回推位置，而重复的段落会回推到错的地方。
"""

from __future__ import annotations

from ..models.corpus import Chunk

SEPARATORS: tuple[str, ...] = (
    "\n\n",  # 段落——最想保住的边界
    "\n",
    "。",
    "！",
    "？",
    "；",
    "，",
    " ",
    "",  # 最后一档：按单个字符。**它保证限长一定守得住。**
)
"""从大到小的自然边界。中文标点在前，因为这个项目的资料以中文为主；
`""` 那一档不能省——一整段没有任何分隔符的文本只能靠它切开。
"""

Span = tuple[int, int]


def chunk(text: str, *, size: int, overlap: int) -> list[Chunk]:
    """切块。`text[c.start:c.end] == c.text` 是硬约束。"""
    if size <= 0:
        raise ValueError("块长必须为正")
    if overlap < 0:
        raise ValueError("重叠不能为负")
    if overlap >= size:
        # **不静默死循环**：重叠 ≥ 块长时切块永远推不动，症状是导入一份资料
        # 之后进程卡住，而错误离原因很远。
        raise ValueError(f"重叠（{overlap}）必须小于块长（{size}），否则切块推不动")
    if not text:
        return []

    # **重叠算在限长之内。** 先按 `size - overlap` 切合并，再往前扩 `overlap`，
    # 合起来正好不超 `size`——否则"一次检索回 5 段、每段 400 字"这笔账会虚，
    # 而那笔账决定 director 的上下文涨多少。
    budget = size - overlap
    pieces = _split((0, len(text)), text, budget, SEPARATORS)
    merged = _merge(pieces, budget)

    out: list[Chunk] = []
    for i, (start, end) in enumerate(merged):
        # **重叠靠往前扩边界，不靠拼接字符串**——这样 text[start:end] 仍然成立。
        widened = max(0, start - overlap) if i else start
        out.append(Chunk(index=i, start=widened, end=end, text=text[widened:end]))
    return out


def _split(span: Span, text: str, size: int, seps: tuple[str, ...]) -> list[Span]:
    """把一段递归切到限长以内。返回**不重叠**的区间，顺序即原文顺序。"""
    start, end = span
    if end - start <= size:
        return [span]
    if not seps:
        return _by_length(span, size)

    sep, rest = seps[0], seps[1:]
    if sep == "":
        return _by_length(span, size)

    parts = _cut_on(span, text, sep)
    if len(parts) == 1:
        # 这一档切不动，换更小的分隔符。
        return _split(span, text, size, rest)

    out: list[Span] = []
    for part in parts:
        out.extend(_split(part, text, size, rest) if part[1] - part[0] > size else [part])
    return out


def _cut_on(span: Span, text: str, sep: str) -> list[Span]:
    """按分隔符切。**分隔符留给前一片**——句子要以句号结尾，
    不能把句号甩到下一块开头。
    """
    start, end = span
    out: list[Span] = []
    cursor = start
    while cursor < end:
        hit = text.find(sep, cursor, end)
        if hit < 0:
            out.append((cursor, end))
            break
        out.append((cursor, hit + len(sep)))
        cursor = hit + len(sep)
    return [s for s in out if s[1] > s[0]]


def _by_length(span: Span, size: int) -> list[Span]:
    start, end = span
    return [(i, min(i + size, end)) for i in range(start, end, size)]


def _merge(pieces: list[Span], size: int) -> list[Span]:
    """把碎片贪心合并回接近限长的块。

    不合并的话，一句话就是一块——**块太碎会让每次检索回来的都是断句**，
    模型拿不到足够的上下文。
    """
    out: list[Span] = []
    for piece in pieces:
        if out and piece[1] - out[-1][0] <= size:
            out[-1] = (out[-1][0], piece[1])
        else:
            out.append(piece)
    return out
