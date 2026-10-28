"""JSON 文件读写。

存本地 JSON 而非数据库，首要理由是**用户可直接编辑**——这是 R22（state 必须
用户可查看/编辑）的最彻底形式。数据库会把这层能力藏起来。

写入用临时文件 + ``os.replace`` 保证原子性（Windows 上同样原子）——用户可能
正好在读文件，不能让他读到半截。
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from threading import RLock
from typing import TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)
_JSON_IO_LOCK = RLock()
"""进程内 JSON I/O 串行化。

WebSocket runner 只有一个，但 REST 元操作仍可能从线程池同时读写同一份 session。
它们若共用 ``.tmp`` 并发替换，Windows 会短暂拒绝访问；读也可能撞在替换窗口上。
锁只覆盖本地文件操作，不覆盖模型调用或业务计算。
"""


def write_model(path: Path, model: BaseModel) -> None:
    """原子写入一个 Pydantic 模型。

    ``model_dump_json`` 输出 UTF-8 不转义非 ASCII——中文直接可读，
    这是"用户可直接编辑"的前提。
    """
    with _JSON_IO_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(model.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)


def read_model(path: Path, model_type: type[M]) -> M:
    with _JSON_IO_LOCK:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def read_model_or_none(path: Path, model_type: type[M]) -> M | None:
    with _JSON_IO_LOCK:
        if not path.exists():
            return None
        return read_model(path, model_type)


def append_jsonl(path: Path, model: BaseModel) -> None:
    """向 JSONL 追加一行。

    trace 是 append-only（S47），逐行追加天然匹配，且不需要读改写整个文件。
    """
    with _JSON_IO_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = model.model_dump_json()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def read_jsonl(path: Path, model_type: type[M]) -> Iterator[M]:
    with _JSON_IO_LOCK:
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
    for raw in lines:
        line = raw.strip()
        if line:
            yield model_type.model_validate_json(line)


def read_jsonl_page(
    path: Path,
    model_type: type[M],
    *,
    limit: int,
    before: int | None = None,
) -> tuple[list[M], int | None]:
    """从 append-only JSONL 的尾部向前取一页。

    ``before`` 是从文件开头数的**非空行位置**，并且不包含该位置。首次不传时
    取文件尾；返回的游标继续向前翻。它不用事件自己的 ``seq``，因为用户编辑等
    后追加事件可能复用既有 seq，而文件位置仍然严格单调（S115）。

    读取会顺序扫文件以确定尾部，但内存只保留一页原始行，且只解析这一页；不会
    因为一条超大 trace 在一次请求里构造全部 Pydantic 对象。
    """
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    if before is not None and before < 0:
        raise ValueError("before 不能小于 0")

    raw_page: deque[str] = deque(maxlen=limit)
    seen = 0
    with _JSON_IO_LOCK:
        if not path.exists():
            return [], None
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                if before is not None and seen >= before:
                    break
                raw_page.append(line)
                seen += 1

    page = [model_type.model_validate_json(raw) for raw in raw_page]
    start = seen - len(page)
    return page, start if start > 0 else None
