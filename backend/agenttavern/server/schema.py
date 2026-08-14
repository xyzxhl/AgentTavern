"""导出前后端契约的 JSON Schema。

**两边定义漂移是这一层唯一值得防的错误**（TechStack.md「前后端契约」）：协议
消息有二十几种，手写一份 TS 对照物只能保证写下来那一刻是对的。所以 TS 类型由
这里导出的 schema 生成，不手写——改了 Pydantic 模型而忘了改前端，会在生成时
就变成编译错误，而不是运行时某个字段悄悄是 `undefined`。

用法：

    python -m agenttavern.server.schema            # 打到 stdout
    python -m agenttavern.server.schema <路径>      # 写文件

前端侧 `npm run gen:types` 会跑上面这条再过 `json-schema-to-typescript`。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from ..models import Session, WorldbookBundle, WorldbookEntry
from .protocol import ClientMessage, ServerMessage

TITLE = "AgentTavern 协议"


def export_schema() -> dict[str, Any]:
    """一份根 schema：两个消息方向 + `Session`，外加共享定义。

    **合成一份**而不是各导一份：`Node`、`TraceEvent`、`ControlOverride` 这些
    到处都用，分开导会生成两套同名不同源的 TS 类型，赋值时报"类型不兼容"，
    而它们本来就是同一个东西。

    `Session` 与 `WorldbookEntry` 走的是 REST 不是 WS，但它们同样是前后端契约的
    一部分——会话列表、元指令编辑、能力开关、设定管理都读它们。
    """
    client = TypeAdapter(ClientMessage).json_schema(ref_template="#/$defs/{model}")
    server = TypeAdapter(ServerMessage).json_schema(ref_template="#/$defs/{model}")
    session = TypeAdapter(Session).json_schema(ref_template="#/$defs/{model}")
    worldbook = TypeAdapter(WorldbookEntry).json_schema(ref_template="#/$defs/{model}")
    bundle = TypeAdapter(WorldbookBundle).json_schema(ref_template="#/$defs/{model}")

    defs: dict[str, Any] = {}
    defs.update(client.pop("$defs", {}))
    defs.update(server.pop("$defs", {}))
    defs.update(session.pop("$defs", {}))
    defs.update(worldbook.pop("$defs", {}))
    defs.update(bundle.pop("$defs", {}))
    defs["Session"] = session
    defs["WorldbookEntry"] = worldbook
    defs["WorldbookBundle"] = bundle

    # **两个联合本身也进 `$defs`**，根对象只 `$ref` 过去。生成器按 `$defs` 的键
    # 命名类型；写成根的内联属性的话，它们会变成 `Protocol["ClientMessage"]`
    # 这种匿名成员，前端就拿不到 `ClientMessage` 这个名字。
    defs["ClientMessage"] = {"title": "ClientMessage", **client}
    defs["ServerMessage"] = {"title": "ServerMessage", **server}

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": TITLE,
        "type": "object",
        "properties": {
            "ClientMessage": {"$ref": "#/$defs/ClientMessage"},
            "ServerMessage": {"$ref": "#/$defs/ServerMessage"},
            "Session": {"$ref": "#/$defs/Session"},
            "WorldbookEntry": {"$ref": "#/$defs/WorldbookEntry"},
            "WorldbookBundle": {"$ref": "#/$defs/WorldbookBundle"},
        },
        "required": [
            "ClientMessage",
            "ServerMessage",
            "Session",
            "WorldbookEntry",
            "WorldbookBundle",
        ],
        "additionalProperties": False,
        "$defs": defs,
    }


def message_types() -> dict[str, set[str]]:
    """两个方向各有哪些 ``type`` 字面量。测试用它守住覆盖面。"""
    out: dict[str, set[str]] = {}
    for name, union in (("client", ClientMessage), ("server", ServerMessage)):
        out[name] = {
            # 每个成员的 `type` 都是 `Literal[...]`，默认值就是那个字面量。
            model.model_fields["type"].default
            for model in union.__args__
        }
    return out


def main() -> None:
    text = json.dumps(export_schema(), ensure_ascii=False, indent=2) + "\n"
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"写入 {target}（{len(text)} 字）")
    else:
        # **不能直接 `sys.stdout.write`**：Windows 控制台默认 GBK，schema 里带
        # 中文描述和箭头符号，一写就 UnicodeEncodeError。
        sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    main()
