"""前后端契约的 schema 导出。

**这一层唯一值得防的错误是两边定义漂移**：协议消息有二十几种，手写一份 TS
对照物只能保证写下来那一刻是对的。所以 TS 由 schema 生成，而这里守住 schema
本身——生成器只是转换，转换不出来的东西它不会替你补。
"""

from __future__ import annotations

import json
from pathlib import Path

from agenttavern.server.protocol import ClientMessage, ServerMessage
from agenttavern.server.schema import export_schema, message_types


def test_every_message_type_reaches_the_schema() -> None:
    """**每一种消息都要能被生成器看见。**

    漏掉一种的症状很隐蔽：前端拿不到那个类型，于是那条分支要么被写成 `any`、
    要么根本没人写，而消息照样在飞——运行时表现是"某个功能就是不响应"。
    """
    schema = export_schema()
    defs = schema["$defs"]

    for union in (ClientMessage, ServerMessage):
        for model in union.__args__:  # type: ignore[attr-defined]
            assert model.__name__ in defs, f"{model.__name__} 没进 $defs"

    kinds = message_types()
    assert "submit" in kinds["client"] and "goal_control" in kinds["client"]
    assert "question" in kinds["server"] and "trace" in kinds["server"]
    # 两个方向不该有同名的 type——同名意味着前端分不清一条消息是谁发的。
    assert not (kinds["client"] & kinds["server"])


def test_shared_models_are_defined_once() -> None:
    """`Node` / `TraceEvent` 这些**两边都用**，必须只有一份定义。

    两个方向各导一份 schema 的话会生成两套同名不同源的 TS 类型，赋值时报
    "类型不兼容"，而它们本来就是同一个东西。
    """
    defs = export_schema()["$defs"]
    for shared in ("Node", "TraceEvent", "ControlOverride"):
        assert shared in defs, f"{shared} 应当作为共享定义出现"

    # 顶层只有两个入口，别的都靠 $ref 指过去。
    assert set(export_schema()["properties"]) == {
        "ClientMessage",
        "ServerMessage",
        "Session",
        "WorldbookEntry",
    }


def test_the_two_unions_are_named_definitions_not_inline() -> None:
    """**两个联合必须是 `$defs` 里的具名条目**，不能内联在根属性上。

    生成器按 `$defs` 的键命名类型。内联的话它们会变成 `Protocol["ClientMessage"]`
    这种匿名成员，前端就拿不到 `ClientMessage` 这个名字——而那正是收发两处
    最需要标注的类型。
    """
    schema = export_schema()
    for name in ("ClientMessage", "ServerMessage", "Session", "WorldbookEntry"):
        assert name in schema["$defs"], f"{name} 应当是具名定义"
        assert schema["properties"][name] == {"$ref": f"#/$defs/{name}"}


def test_the_schema_is_json_serialisable(tmp_path: Path) -> None:
    """**必须能落成文件**——生成器读的是文件，不是 Python 对象。

    枚举、`Literal`、嵌套模型里任何不可序列化的东西都会在这里当场暴露，
    而不是等到前端 `npm run gen:types` 时才报一句语焉不详的错。
    """
    target = tmp_path / "protocol.schema.json"
    target.write_text(json.dumps(export_schema(), ensure_ascii=False, indent=2), encoding="utf-8")

    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert reloaded["title"]
    assert reloaded["$defs"]["Submit"]["properties"]["type"]["const"] == "submit"
