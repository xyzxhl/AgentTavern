"""worldbook 仓储：**一级资产，不属于任何 session**（D170）。

目录布局::

    data/worldbooks/<worldbook_id>/
      worldbook.json           名字、题材与基调、尺度
      entries/<entry_id>.json  一条设定一个文件

以前它躺在 `sessions/<id>/worldinfo/` 里，因为 D92 定的是「一份 worldbook 开启
一个 session」。那条一对一现在不成立了：**同一个世界底下可以有好几个故事**，
而把设定钉在某一局故事里，等于每开一个新故事都要把整份设定复制一遍——复制之后
「改一次、所有故事都跟着」就没了，而那正是把它独立出来要买的东西。

## 跑着的故事被改了设定怎么办

分两半答，这是 D158 已经定下的那条线，独立出来之后**恰好还是它**：

- **已经发生过的剧情所依赖的事实**（初始状态）在第一轮就冻结进
  `Session.initial_state`。之后再改 `state` 条目只影响新会话——第五轮写着
  "她掏出钥匙"，钥匙来自初始状态，把它删掉那一轮就凭空多了一把不存在的钥匙。
- **静态设定**（`world` / `character` 条目、题材、尺度）跟着改，立刻生效。
  它们是"这个世界是什么"，改它就等于改角色卡——那本来就该对所有故事生效。
"""

from __future__ import annotations

from pathlib import Path

from ..models import EntryKind, StateDelta, Worldbook, WorldbookEntry, WorldState
from .jsonfile import read_model, read_model_or_none, write_model


class WorldbookRepository:
    def __init__(self, root: Path, worldbook_id: str) -> None:
        self.root = root
        self.worldbook_id = worldbook_id
        self.dir = root / "worldbooks" / worldbook_id

    # -- 整份设定 -----------------------------------------------------------

    @property
    def book_path(self) -> Path:
        return self.dir / "worldbook.json"

    def load(self) -> Worldbook:
        """**文件缺了不炸**，给一份空的（带着正确的 id）。

        它会缺：旧数据还没迁完、或者 session 指着一份已经被删掉的设定。那两种
        情况下"这个世界什么都没有"是个能跑的答案，而在读配置的路上抛异常
        会让整个会话打不开——症状离原因很远。
        """
        book = read_model_or_none(self.book_path, Worldbook)
        return book or Worldbook(id=self.worldbook_id)

    def save(self, book: Worldbook) -> None:
        write_model(self.book_path, book)

    def exists(self) -> bool:
        return self.book_path.exists()

    # -- 条目 ---------------------------------------------------------------

    @property
    def entries_dir(self) -> Path:
        return self.dir / "entries"

    def entry_path(self, entry_id: str) -> Path:
        return self.entries_dir / f"{entry_id}.json"

    def entries(self) -> list[WorldbookEntry]:
        """**全部**条目，不做预筛选（D62/D63）。索引覆盖全集是准入标准。

        **读不出来的条目在这里照抛**（D179）。这条路径喂模型——静默跳过等于让
        一条设定悄悄消失，而模型据此写出矛盾的剧情，没有任何地方会报错。
        要「能列出来、别整个瘫掉」那种宽容，走 `survey()`。
        """
        if not self.entries_dir.exists():
            return []
        entries = [read_model(p, WorldbookEntry) for p in self.entries_dir.glob("*.json")]
        return sorted(entries, key=lambda e: (e.name, e.id))

    def survey(self) -> tuple[int, list[str]]:
        """``(读得出来的条数, 读不出来的文件名)``。**列表用它，模型不用**（D179）。

        实测踩过：盘上一条 `kind: "lore"` 的旧条目（那个值早改叫 `world` 了），
        于是 `GET /worldbooks` **整个 500**——设定库列不出来，新建之后的刷新也炸，
        用户看到的症状是「新建没反应」。**一条八百年前的脏数据让整个功能瘫痪，
        而报错离原因十万八千里。**

        所以列表这一层要宽容：它只是为了显示"几条"，不喂模型。坏的那几条**报出来
        而不是跳过**——悄悄少一条比 500 更糟，用户根本不知道自己丢了什么（D62）。
        """
        if not self.entries_dir.exists():
            return 0, []
        good, bad = 0, []
        for path in sorted(self.entries_dir.glob("*.json")):
            try:
                read_model(path, WorldbookEntry)
            except Exception:  # noqa: BLE001 — 坏成什么样都算坏，这里只负责数
                bad.append(path.name)
            else:
                good += 1
        return good, bad

    def save_entry(self, entry: WorldbookEntry) -> None:
        write_model(self.entry_path(entry.id), entry)

    def load_entry(self, entry_id: str) -> WorldbookEntry | None:
        return read_model_or_none(self.entry_path(entry_id), WorldbookEntry)

    def delete_entry(self, entry_id: str) -> bool:
        """删掉一条设定。不存在返回 ``False``。

        **故事开始之后删初始状态条目不影响既有的每一轮**（D158）：起点那时
        已经冻结进 `Session.initial_state` 了，这里删的只是"以后新建会话时的模板"。
        开始之前删则立刻生效——那时故事还没有过去，没有东西依赖它。
        """
        path = self.entry_path(entry_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def clear(self) -> int:
        """删掉全部条目，返回删了几条。**导入时的「替换」用它。**"""
        entries = self.entries()
        for entry in entries:
            self.entry_path(entry.id).unlink(missing_ok=True)
        return len(entries)

    # -- 初始状态 -----------------------------------------------------------

    def compose_initial_state(self) -> WorldState:
        """把全部 `STATE` 条目合成一个初始状态。**故事开始之前**用它。

        它归 worldbook 而不是 session：合成的材料全在这里，session 那边只负责
        决定「什么时候把它定下来」（`freeze_initial_state`）。
        """
        state = WorldState()
        for entry in self.entries():
            if entry.kind is EntryKind.STATE and entry.values:
                state = state.apply(StateDelta(set=entry.values))
        return state


__all__ = ["WorldbookRepository"]
