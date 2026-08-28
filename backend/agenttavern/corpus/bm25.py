"""BM25：混合检索里**不依赖任何外部模型**的那一路（S140）。

它同时是**降级路径**——embeddings 没配或调用失败时整条链路退回到它，
而不是拒绝启用：只配了 chat 端点的部署照样能用上这个功能，
只是有损，所以要在回执里说出来。

**中文必须分词。** 这条链路要解决的是同人黑话、朝代官职、专业术语这一类
**低频词**，而 BM25 的全部价值就在低频词得分高——切成单字之后那个价值就没了。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

import jieba
from pydantic import BaseModel

# 拉丁字母、数字、下划线连成一串算一个词；其余交给 jieba。
_LATIN = re.compile(r"[A-Za-z0-9_]+")

# 只留下"能承载信息"的字符。标点在每一块里都出现，留着等于给所有块加同一个
# 高频词，**稀释真正的信号**。
_MEANINGFUL = re.compile(r"[\w一-鿿]")

K1 = 1.5
B = 0.75
"""BM25 的两个常数，取业界通用值。

`k1` 管词频饱和得多快，`b` 管长度归一化的强度。**这两个值调过要重测**——
它们直接改变排序，而排序坏了的症状只是"结果变差"，不会报错。
"""


def tokenise(text: str) -> list[str]:
    """切成词。**专名与术语要整个留下来**（S140）。

    拉丁串先摘出去再交给 jieba：混排时 jieba 会把 `BM25` 拆成 `BM` + `25`，
    而中英混排在专业资料里是常态。
    """
    out: list[str] = []
    cursor = 0
    for m in _LATIN.finditer(text):
        out.extend(_cut(text[cursor : m.start()]))
        out.append(m.group(0).lower())
        cursor = m.end()
    out.extend(_cut(text[cursor:]))
    return out


def _cut(segment: str) -> list[str]:
    if not segment:
        return []
    return [w for w in jieba.cut(segment) if _MEANINGFUL.search(w)]


class BM25Index(BaseModel):
    """一份资料的 BM25 索引。**落盘成 JSON**——同"不用数据库"那条理由（S143）。"""

    doc_freq: dict[str, int]
    """每个词出现在多少块里。"""

    term_freq: list[dict[str, int]]
    """每块里每个词出现几次。按块序号排列。"""

    lengths: list[int]
    """每块多少个词。"""

    @property
    def total(self) -> int:
        return len(self.lengths)

    @property
    def avg_len(self) -> float:
        return sum(self.lengths) / self.total if self.total else 0.0

    @classmethod
    def build(cls, texts: list[str]) -> BM25Index:
        term_freq = [Counter(tokenise(t)) for t in texts]
        doc_freq: Counter[str] = Counter()
        for tf in term_freq:
            doc_freq.update(tf.keys())
        return cls(
            doc_freq=dict(doc_freq),
            term_freq=[dict(tf) for tf in term_freq],
            lengths=[sum(tf.values()) for tf in term_freq],
        )

    def search(self, query: str, *, limit: int) -> list[tuple[int, float]]:
        """按相关性返回 `(块序号, 分数)`，高分在前。

        **一个词都没命中就返回空**——由调用方去说「这次没命中」（S139）。
        这一层不负责措辞，但也绝不编造命中。
        """
        terms = tokenise(query)
        if not terms or not self.total:
            return []

        avg = self.avg_len or 1.0
        scored: list[tuple[int, float]] = []
        for i, tf in enumerate(self.term_freq):
            score = 0.0
            for term in terms:
                freq = tf.get(term)
                if not freq:
                    continue
                df = self.doc_freq.get(term, 0)
                # 加一平滑：df 等于总块数时若不平滑会得到 0，
                # 那意味着"每块都有的词"完全不计分——而它仍然是命中。
                idf = math.log(1 + (self.total - df + 0.5) / (df + 0.5))
                norm = 1 - B + B * self.lengths[i] / avg
                score += idf * freq * (K1 + 1) / (freq + K1 * norm)
            if score > 0:
                scored.append((i, score))

        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:limit]

    def coverage(self, query: str, chunk_index: int) -> float:
        """query 切出来的词，有多大比例出现在这一块里（S148）。

        **判定用它，不用 BM25 原始分。** 原始分依赖这份资料的词频分布：
        同一个 3.7 分在一本小说里是强命中、在一份术语表里可能是噪声。
        拿它设阈值，**换一份资料整条链路的松紧就变了，而没有任何地方会报错**。
        覆盖率是比例，到哪儿都一样。
        """
        terms = set(tokenise(query))
        if not terms or not 0 <= chunk_index < self.total:
            return 0.0
        present = self.term_freq[chunk_index].keys()
        return len(terms & present) / len(terms)

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> BM25Index:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
