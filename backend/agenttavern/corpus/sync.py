"""源文件夹和知识库的对账（S150 / S151）。

**这一层只回答"哪儿对不上"，一个字节都不写。** 动手由用户按下才发生——
处理一份大资料要花时间也要花钱，而编辑器的每一次保存都会改 mtime，
自动同步等于让保存动作触发重建。

## 认领规则：先按路径，认不到再按内容

| 用户干了什么 | 路径 | 哈希 | 判成 |
|---|---|---|---|
| 什么都没干 | 认得到 | 一致 | 不出现在待办里 |
| 改内容 | 认得到 | 不一致 | **已修改** |
| 改文件名 | 认不到 | 认得到 | **改名**（只更新记录，不重建） |
| 放了个新文件 | 认不到 | 认不到 | **新文件** |
| 删了文件 | — | — | **源已删除** |
| **又改名又改内容** | 认不到 | 认不到 | 删一个 + 加一个 |

最后一行是这套规则**唯一的失真**，写在这里而不是藏着。

## 为什么每次都算哈希，不拿 mtime 抄近路

因为**结论一律由 hash 下**（S150）。mtime 在某些同步盘与容器里不可靠，
而同一秒内的两次同长度修改会让 mtime 与 size 都不变——那时抄近路就是漏报，
而漏报的后果是索引永久地对应着一份已经不存在的文本。

代价是每次对账把源文件读一遍：一本一兆的小说约 5 毫秒。
**真慢了再按 (size, mtime) 加一层进程内缓存**（P27：先量再改）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..models.corpus import Corpus
from .sources import SUPPORTED, list_sources, source_hash


class ChangeKind(StrEnum):
    NEW = "new"
    MODIFIED = "modified"
    RENAMED = "renamed"
    DELETED = "deleted"
    UNSUPPORTED = "unsupported"
    """认不得的后缀。**它也是一条待办**——不列出来的话，用户放了个 `.doc`
    会一直等一个永远不出现的条目，而界面上什么都不说。
    """


@dataclass(frozen=True)
class Pending:
    """一条对不上的地方。**给界面看的，不是给系统自动执行的。**"""

    kind: ChangeKind
    source_name: str
    corpus_id: str = ""
    """`NEW` 与 `UNSUPPORTED` 时为空——那时还没有对应的资料。"""

    reason: str = ""
    """`UNSUPPORTED` 时说清为什么。"""


def reconcile(folder: Path, corpora: list[Corpus]) -> list[Pending]:
    """对一次账。**只读，不写。**

    `source_name` 为空的资料（用接口直接导入的，没有源文件）**不参与对账**——
    它压根不属于文件夹那一侧，标成"源已删除"是错的。
    """
    tracked = [c for c in corpora if c.source_name]
    by_name = {c.source_name: c for c in tracked}
    by_hash = {c.source_hash: c for c in tracked if c.source_hash}

    files = list_sources(folder)
    out: list[Pending] = []
    claimed: set[str] = set()

    for item in files:
        if not item.supported:
            out.append(
                Pending(
                    kind=ChangeKind.UNSUPPORTED,
                    source_name=item.name,
                    reason=f"不支持的文件类型。可以放：{'、'.join(sorted(SUPPORTED))}",
                )
            )
            continue

        digest = source_hash(folder / item.name)

        # 1. 先按路径认。
        if (corpus := by_name.get(item.name)) is not None:
            claimed.add(corpus.corpus_id)
            if corpus.source_hash != digest:
                out.append(
                    Pending(
                        kind=ChangeKind.MODIFIED,
                        source_name=item.name,
                        corpus_id=corpus.corpus_id,
                    )
                )
            continue

        # 2. 路径认不到，按内容认——**这一步是"改名不算改内容"的全部实现**。
        if (corpus := by_hash.get(digest)) is not None and corpus.corpus_id not in claimed:
            claimed.add(corpus.corpus_id)
            out.append(
                Pending(
                    kind=ChangeKind.RENAMED,
                    source_name=item.name,
                    corpus_id=corpus.corpus_id,
                )
            )
            continue

        # 3. 两个都认不到 = 新文件。
        out.append(Pending(kind=ChangeKind.NEW, source_name=item.name))

    # 4. 没被任何文件认领的资料 = 源文件没了。
    out.extend(
        Pending(
            kind=ChangeKind.DELETED,
            source_name=corpus.source_name,
            corpus_id=corpus.corpus_id,
        )
        for corpus in tracked
        if corpus.corpus_id not in claimed
    )
    return out


__all__ = ["ChangeKind", "Pending", "reconcile"]
