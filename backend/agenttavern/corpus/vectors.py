"""向量索引（S143）。

**存成 `.npy`，不是 JSON。** 几千块 × 上千维，JSON 又大又慢，
而且要自己发明一套浮点格式；`.npy` 是 numpy 自带的，读写都是一行。

这**不违反**「文件要人类可读」那条：可读说的是**用户写的东西**——
原文与块表都是纯文本。向量是派生数据，改它没有意义，重建一遍就是了。

**用余弦不用点积。** 点积会让长向量无脑胜出，而向量长度和相关性无关；
余弦归一化到 [-1, 1]，还顺带让阈值跨资料可比（S148）。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class VectorIndex:
    """一份资料的向量。行序号就是块序号。"""

    def __init__(self, matrix: np.ndarray) -> None:
        self._m = matrix

    @property
    def count(self) -> int:
        return int(self._m.shape[0]) if self._m.size else 0

    @classmethod
    def build(cls, vectors: list[list[float]]) -> VectorIndex:
        if not vectors:
            return cls(np.zeros((0, 0), dtype=np.float32))
        return cls(np.asarray(vectors, dtype=np.float32))

    def search(self, query: list[float], *, limit: int) -> list[tuple[int, float]]:
        """按余弦相似度返回 `(块序号, 相似度)`，高的在前。

        **没建向量的资料返回空而不是抛**——那种资料要能走降级路径查，
        在这里炸掉会把整条链路带走。
        """
        if not self.count or not query:
            return []

        q = np.asarray(query, dtype=np.float32)
        if q.shape[0] != self._m.shape[1]:
            # 维度对不上说明换过 embedding 模型而索引没重建。
            # **当场报错**：静默返回空会被读成"资料里没有"，那是最难查的一类。
            raise ValueError(
                f"向量维度对不上（索引 {self._m.shape[1]}，query {q.shape[0]}）"
                "——多半是换了 embedding 模型，需要重建索引"
            )

        qn = float(np.linalg.norm(q))
        rn = np.linalg.norm(self._m, axis=1)
        # 零向量的模是 0，除法会得到 nan。把它压成 0：**和谁都不像**，
        # 而不是"和谁都像"——nan 在排序里的行为不可预测。
        denom = rn * qn
        sims = np.where(denom > 0, self._m @ q / np.where(denom > 0, denom, 1.0), 0.0)

        order = np.argsort(-sims)[:limit]
        return [(int(i), float(sims[i])) for i in order]

    def dump(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, self._m)

    @classmethod
    def load(cls, path: Path) -> VectorIndex:
        return cls(np.load(path))
