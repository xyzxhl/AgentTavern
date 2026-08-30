"""读源文件夹里的文件（S153 / S154）。

**这一层只回答一件事：这个文件里的字是什么。** 拿到字之后，txt、docx、pdf
走的是完全同一条路——所以解析那两个依赖碰不到检索的任何一层。

## 三种安静的坏法，全都要出声

| | 不拦住会怎样 |
|---|---|
| **乱码**（GBK 当 UTF-8 读） | 一串 `�` 被切块、被 embed、进索引，全程不报错 |
| **扫描件**（PDF 没有文字层） | 抽出来是空的，建成一份"查不到任何东西"的资料 |
| **空文件** | 同上 |

三种的症状都是**"这份资料怎么查都查不到"**，而那和「资料里确实没有」
看起来一模一样（同 S139 那条罪）。所以这一层宁可报错，也不产出可疑的文本。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..config import LIMITS

TEXT_SUFFIXES = frozenset({".txt", ".md", ".json", ".yaml", ".yml", ".csv"})
"""纯文本一族，直接读。"""

DOC_SUFFIXES = frozenset({".docx", ".pdf"})
"""要先解析成文字的。**只有这两种**——`.doc`（老格式）不认，`.png` 更不认。"""

SUPPORTED = TEXT_SUFFIXES | DOC_SUFFIXES

MIN_CHARS_PER_PAGE = LIMITS.corpus.min_chars_per_page

_ENCODINGS = ("utf-8", "gb18030")
"""按顺序试。**`gb18030` 不是 `gbk`**：前者是后者的超集，多认一批生僻字，
而代价为零。"""


class SourceError(RuntimeError):
    """这个文件读不成。**给人读的一句话**，会原样显示在界面上。"""


@dataclass(frozen=True)
class SourceFile:
    """源文件夹里的一个文件。**认不得的也在这里**，标成不支持。"""

    name: str
    size: int
    mtime: float
    supported: bool


@dataclass(frozen=True)
class Extracted:
    text: str
    encoding: str
    """**怎么把字节变成字的**：`utf-8` / `gb18030` / `docx` / `pdf`。

    后两个不是编码，但它们回答的是同一个问题，而用户要看到的也正是这个答案。
    """


def list_sources(folder: Path) -> list[SourceFile]:
    """列源文件夹。**认不得的后缀也列出来**，标成不支持。

    悄悄跳过的话，用户放了一个 `.doc`（不是 `.docx`）会一直等一个永远不出现的
    条目，而界面上什么都不说。

    文件夹不存在时返回空——**第一次起服务时它就是不存在的**，那不是错误。
    隐藏文件（`.` 开头）跳过：`.DS_Store`、`.gitkeep` 这些不是用户放的资料。
    """
    if not folder.exists():
        return []
    out: list[SourceFile] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        stat = path.stat()
        out.append(
            SourceFile(
                name=path.name,
                size=stat.st_size,
                mtime=stat.st_mtime,
                supported=path.suffix.lower() in SUPPORTED,
            )
        )
    return out


def source_hash(path: Path) -> str:
    """文件内容的指纹。**算的是字节**——改名不变，改内容才变（S150）。

    算字节而不是解码后的文本：解码本身可能失败，而"这个文件变没变"
    是个在解码之前就该回答得了的问题。
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_source(path: Path) -> Extracted:
    """把一个源文件读成文字。**读不成就抛 `SourceError`**，不返回可疑的东西。"""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise SourceError(
            f"不支持的文件类型 `{suffix or path.name}`。可以放：{'、'.join(sorted(SUPPORTED))}"
        )

    if suffix in TEXT_SUFFIXES:
        extracted = _read_text(path)
    elif suffix == ".docx":
        extracted = _read_docx(path)
    else:
        extracted = _read_pdf(path)

    if not extracted.text.strip():
        raise SourceError("文件是空的。")
    return extracted


def _read_text(path: Path) -> Extracted:
    """UTF-8 → GB18030。**两种都解不开就报错**（S153）。

    绝不用 `errors="replace"`：那会安静地产出一串 `�`，然后它被切块、
    被 embed、进索引——全程没有任何地方报错。
    """
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return Extracted(text=raw.decode(encoding), encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise SourceError(f"编码认不出来（试过 {'、'.join(_ENCODINGS)}）。把它另存为 UTF-8 再放进来。")


def _read_docx(path: Path) -> Extracted:
    import docx

    try:
        document = docx.Document(str(path))
    except Exception as exc:  # noqa: BLE001 —— 坏文件什么都可能抛
        raise SourceError(f"这个 .docx 打不开：{exc}") from exc
    # **段落之间留空行**：切块那一层第一个试的分隔符就是空行（S142），
    # 拼成一坨会让它退到按句号切，块的边界就跟着变差。
    return Extracted(
        text="\n\n".join(p.text for p in document.paragraphs if p.text.strip()),
        encoding="docx",
    )


def _read_pdf(path: Path) -> Extracted:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001
        raise SourceError(f"这个 PDF 打不开：{exc}") from exc

    # **扫描件的特征是「页数不少，却几乎抽不出字」**（S154），所以门槛按页算，
    # 不按总字数：一份一页五十字的 PDF 是合法的，一份两百页零字的不是。
    # 用绝对字数当门槛会把前者一起拒掉——那是这条规则第一版的错。
    body = "".join(pages).strip()
    if len(body) < len(pages) * MIN_CHARS_PER_PAGE:
        raise SourceError(
            f"{len(pages)} 页只抽出 {len(body)} 个字——**这看起来是扫描件**"
            "（没有文字层）。本项目不做 OCR，需要先用别的工具转成文字。"
        )

    # **不插页码标记**（S154）：它们会混进被检索的正文，被切块、被分词、
    # 被 embed，成为噪声——BM25 那一路对这种高频短串尤其敏感。
    # 代价是用户不能按页回查，补法是界面上直接给他看解析后的这份文字。
    return Extracted(text="\n\n".join(p for p in pages if p.strip()), encoding="pdf")


__all__ = [
    "DOC_SUFFIXES",
    "SUPPORTED",
    "TEXT_SUFFIXES",
    "Extracted",
    "SourceError",
    "SourceFile",
    "list_sources",
    "read_source",
    "source_hash",
]
