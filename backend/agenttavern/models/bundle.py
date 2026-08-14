"""worldbook 的交换格式。格式与三条约定见 docs/design/WorldbookFormat.md。

**一份 worldbook 开启一个 session**（D90），所以它是这个项目里唯一值得单独定
格式的东西——别的（节点树、trace、state）都是跑出来的产物，不是人写的输入。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .worldbook import WorldbookEntry

FORMAT = "agenttavern.worldbook"
VERSION = 1


class WorldbookBundle(BaseModel):
    """一份可以拷走的 worldbook。"""

    format: str = FORMAT
    version: int = VERSION

    name: str = ""
    note: str = ""

    entries: list[WorldbookEntry] = Field(default_factory=list)

    @classmethod
    def of(
        cls, entries: list[WorldbookEntry], *, name: str = "", note: str = ""
    ) -> WorldbookBundle:
        """打包。**去掉 `id` 与 `summary`**（见文档 §2）。

        - `id` 是这份存储里的地址，不是条目的身份。带着它导入要么撞上已有条目
          静默覆盖，要么两份文件里的同名 id 指向完全不同的东西。
        - `summary` 是 `summarize` 懒惰生成的缓存（D69），跟着 `content` 走；
          `content` 一旦被人改过旧摘要就是错的，而导入方无从判断改没改。
          **缓存不该跨机器传播。**
        """
        return cls(
            name=name,
            note=note,
            entries=[entry.model_copy(update={"id": "", "summary": None}) for entry in entries],
        )

    def check(self) -> str | None:
        """能不能导。不能就给一句人能读的理由。

        **不做尽力而为的解析**：读一份半懂的格式会把坏数据导进来，用户以为成了，
        几轮之后才发现不对，而那时已经分不清是导入丢的还是模型写坏的。
        """
        if self.format != FORMAT:
            return f"这不是 worldbook 文件（format={self.format!r}）。"
        if self.version != VERSION:
            return f"版本不认识：{self.version}，本程序只认 {VERSION}。"
        if not self.entries:
            return "文件里一条设定都没有。"
        return None


__all__ = ["FORMAT", "VERSION", "WorldbookBundle"]
