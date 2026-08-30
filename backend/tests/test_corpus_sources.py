"""源文件夹：读文件、对账、更新（S149–S154）。

一条贯穿的原则和这一篇的其它部分一样：**读不出来要出声**。
乱码、扫描件、空文件——三种都会安静地产出一份"建好了但怎么查都查不到"的资料，
而那和「资料里确实没有」看起来一模一样（同 S139 那条罪）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttavern.corpus.sources import (
    SourceError,
    list_sources,
    read_source,
    source_hash,
)

TANG = "尚书省掌管六部政令，长官为尚书令。中书省负责起草诏令。"


def write(root: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(text.encode(encoding))
    return path


def write_pdf(root: Path, name: str, pages: list[str]) -> Path:
    """造一份**真的有文字层**的 PDF。空的 `pages` 就是一份扫描件的样子。

    手搓字节不行——pypdf 要求合法的 xref。所以用它自己的 writer 组：
    每页挂一个 Helvetica 字体资源和一段 `BT … Tj ET` 的内容流。
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    root.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for body in pages or [""]:
        page = writer.add_blank_page(width=300, height=300)
        if not body:
            continue
        font = DictionaryObject()
        font.update(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 14 Tf 20 150 Td ({body}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)

    path = root / name
    with path.open("wb") as fh:
        writer.write(fh)
    return path


# -- S153 编码 -----------------------------------------------------------------


def test_a_utf8_file_is_read_as_utf8(tmp_path: Path) -> None:
    got = read_source(write(tmp_path, "唐.txt", TANG))
    assert got.text == TANG
    assert got.encoding == "utf-8"


def test_a_gbk_file_is_read_and_the_encoding_is_recorded(tmp_path: Path) -> None:
    """**中文用户的旧 txt 有相当概率是 GBK**（S153）。

    用哪种编码读的要记下来并显示——猜了就得说出来。
    """
    got = read_source(write(tmp_path, "唐.txt", TANG, encoding="gb18030"))
    assert got.text == TANG
    assert got.encoding == "gb18030"


def test_no_replacement_characters_are_ever_indexed(tmp_path: Path) -> None:
    """**两种都解不开就报错，不拿 `errors="replace"` 糊过去**（S153）。

    替换字符是不抛异常的：一串 `�` 会被切块、被 embed、进索引，
    **全程没有任何地方报错**，症状只是"这份资料怎么查都查不到"。
    """
    path = tmp_path / "坏的.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 和 GB18030 都解不开的字节序列。
    path.write_bytes(b"\xff\xfe\x00\x01\xff\xff\xfe\xfe")

    with pytest.raises(SourceError) as caught:
        read_source(path)
    assert "编码" in str(caught.value)


def test_gb18030_not_gbk_so_rare_characters_survive(tmp_path: Path) -> None:
    """**用 gb18030 不用 gbk**：前者是后者的超集，多认一批生僻字，代价为零。"""
    rare = "钅字旁的字：镕鎏鏊。还有𠮷"
    got = read_source(write(tmp_path, "生僻.txt", rare, encoding="gb18030"))
    assert got.text == rare


# -- S154 认哪些后缀 -----------------------------------------------------------


@pytest.mark.parametrize("name", ["a.txt", "b.md", "c.json", "d.yaml", "e.yml", "f.csv"])
def test_plain_text_family_is_read_directly(tmp_path: Path, name: str) -> None:
    assert read_source(write(tmp_path, name, TANG)).text == TANG


def test_an_unsupported_extension_is_reported_not_indexed(tmp_path: Path) -> None:
    """认不得的后缀**标成「不支持」，不当二进制切块**（S154）。"""
    with pytest.raises(SourceError) as caught:
        read_source(write(tmp_path, "封面.png", "不是图片但后缀是"))
    assert "不支持" in str(caught.value)


def test_an_empty_file_fails_loudly(tmp_path: Path) -> None:
    """空文件**标出来**，不当成一份空资料（R46/S154）。

    建一份空索引出来，查它永远是"没找到"——而那和"资料里确实没有"读起来一样。
    """
    with pytest.raises(SourceError) as caught:
        read_source(write(tmp_path, "空的.txt", "   \n\n  "))
    assert "空" in str(caught.value)


# -- S154 二进制文档 -----------------------------------------------------------


def test_a_docx_becomes_plain_text(tmp_path: Path) -> None:
    """`.docx` **解析成纯文本再走同一条路**（S154）。"""
    import docx

    document = docx.Document()
    for line in ("尚书省掌管六部政令。", "中书省负责起草诏令。"):
        document.add_paragraph(line)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "官制.docx"
    document.save(path)

    got = read_source(path)
    assert "尚书省掌管六部政令" in got.text
    assert "中书省负责起草诏令" in got.text
    assert got.encoding == "docx", "怎么把字节变成文字的，要记下来"


def test_a_pdf_without_a_text_layer_fails_loudly(tmp_path: Path) -> None:
    """扫描件**当场失败并说清**（S154）。

    没有文字层的 PDF 抽出来是空的。不拦住的话它会变成一份"建好了但怎么查都
    查不到"的资料——**和乱码是同一种病**。
    """
    scanned = write_pdf(tmp_path, "扫描件.pdf", ["", "", ""])
    with pytest.raises(SourceError) as caught:
        read_source(scanned)
    assert "扫描" in str(caught.value)
    assert "3 页" in str(caught.value), "要说清是几页里抽不出字"


def test_a_short_but_real_pdf_is_not_mistaken_for_a_scan(tmp_path: Path) -> None:
    """**门槛按页算，不按总字数。**

    这条是第一版规则的修正：当时用绝对字数（100 字）当门槛，于是一份
    一页五十字的正常文档被判成扫描件。**扫描件的特征是「页数不少，
    却几乎抽不出字」**——那是个每页的比例，不是一个总量。
    """
    from agenttavern.corpus.sources import MIN_CHARS_PER_PAGE

    body = "Shangshu sheng zhang liu bu"
    assert len(body) < MIN_CHARS_PER_PAGE * 3, "样本要短到能证明绝对门槛是错的"

    got = read_source(write_pdf(tmp_path, "便签.pdf", [body]))
    assert body in got.text
    assert got.encoding == "pdf"


# -- S150 指纹 -----------------------------------------------------------------


def test_the_hash_follows_the_bytes_not_the_name(tmp_path: Path) -> None:
    """指纹算的是**字节**：改名不变，改内容才变。"""
    one = write(tmp_path, "甲.txt", TANG)
    two = write(tmp_path, "乙.txt", TANG)
    assert source_hash(one) == source_hash(two)

    three = write(tmp_path, "丙.txt", TANG + "。")
    assert source_hash(three) != source_hash(one)


def test_listing_skips_nothing_and_reports_what_it_saw(tmp_path: Path) -> None:
    """列源文件夹：**认不得的后缀也要列出来**，标成不支持而不是当没看见。

    悄悄跳过的话，用户放了一个 `.doc`（不是 `.docx`）会一直等一个永远不出现的
    条目，而界面上什么都不说。
    """
    write(tmp_path, "唐.txt", TANG)
    write(tmp_path, "海.md", TANG)
    write(tmp_path, "封面.png", "x")
    (tmp_path / ".DS_Store").write_bytes(b"x")

    seen = {f.name: f for f in list_sources(tmp_path)}
    assert set(seen) == {"唐.txt", "海.md", "封面.png"}, "隐藏文件不算，别的都要列"
    assert seen["唐.txt"].supported is True
    assert seen["封面.png"].supported is False


def test_listing_a_missing_folder_is_empty_not_an_error(tmp_path: Path) -> None:
    """文件夹还不存在时返回空——**第一次起服务时它就是不存在的**。"""
    assert list_sources(tmp_path / "还没建") == []


def test_no_page_markers_leak_into_the_indexed_text(tmp_path: Path) -> None:
    """**解析 PDF 时不插页码标记**（S154）。

    插了的话它们会混进被检索的正文，被切块、被分词、被 embed，成为噪声——
    BM25 那一路对这种高频短串尤其敏感。代价是用户不能按页回查，
    补法是界面上直接给他看解析后的这份文字。
    """
    pages = ["Shangshu sheng liu bu", "Zhongshu sheng zhaoling", "Menxia sheng fengbo"]
    got = read_source(write_pdf(tmp_path, "三页.pdf", pages))

    for body in pages:
        assert body in got.text
    for leaked in ("第 1 页", "Page 1", "[1]", "--- 1 ---"):
        assert leaked not in got.text


def test_no_filesystem_watcher_is_installed() -> None:
    """**不装文件监视器**（S150）——差异靠指纹对账算出来。

    事件会丢：进程没跑的时候用户改了文件，那个事件就永远没有了，而丢了事件
    的后果不是"晚一点更新"，是索引**永久地**对应着一份已经不存在的文本。

    这条钉的是那个决定本身：`watchdog` 一旦出现在依赖里，就说明有人在往
    回走，而那时该先读一遍 S150 的理由。
    """
    import tomllib
    from pathlib import Path as P

    import agenttavern

    # `agenttavern/__init__.py` → `agenttavern/` → `backend/`
    pyproject = P(agenttavern.__file__).parent.parent / "pyproject.toml"
    assert pyproject.is_file(), f"找不到 pyproject：{pyproject}"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    assert not [d for d in declared if "watch" in d.lower()]
