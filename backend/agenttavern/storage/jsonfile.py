"""JSON 文件读写。

存本地 JSON 而非数据库，首要理由是**用户可直接编辑**——这是 R22（state 必须
用户可查看/编辑）的最彻底形式。数据库会把这层能力藏起来。

写入用临时文件 + ``os.replace`` 保证原子性（Windows 上同样原子）——用户可能
正好在读文件，不能让他读到半截。
"""

from __future__ import annotations

import os
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
