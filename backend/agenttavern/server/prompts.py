"""提示词管理的读写接口（R31）。

**路径是 `/prompts`，不是 `/agents`**（S147）：这里管的是每一份 YAML，而它们
分两类——agent（名词，一个角色）和单次调用（动词，一次动作）。挂在 `/agents`
底下会让 `summarize` 看起来是 agent 的附属，而它是并列的另一类。

判据是：**所有被输入模型的内容，用户都能审查和修改。** 那不只是 `sections`——
它同时管**工具描述**、**运行时另外追加的整条消息**、以及嵌在渲染里的那些短句。
所以这里递出去的就是那份 YAML 的全部，只多一样：**工具的参数 schema**。

参数 schema 由 Python 生成、只读。这是 S10 那条担忧的落地方式：「一个工具怎么
描述自己，跟它的参数是同一件事，拆开写两处必然对不上」——**那个风险是真的**，
所以不把参数也搬进 YAML，而是让它和描述**并排显示**，漂移摆在人眼前。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..llm import Message, Role
from ..prompting import AGENTS, PROMPT_FILES, assemble, load, save
from ..tools import build_state_tools, build_tools, goal_tools


class PromptDoc(BaseModel):
    """一份提示词配置的完整内容。**原样进原样出**——它就是那份 YAML。"""

    data: dict[str, Any]


def _params() -> dict[str, dict[str, Any]]:
    """每个工具的参数 schema，**由 Python 生成**。

    这里把三套工具面都建一遍是有意的：页面上要显示的是「这个工具长什么样」，
    而那跟它此刻注不注册无关——一个被关掉的工具，用户照样要能读它的描述、
    看它的参数，才决定要不要打开。
    """
    out: dict[str, dict[str, Any]] = {}
    for tool in [*build_tools(), *build_state_tools(), *goal_tools()]:
        out[tool.name] = tool.params_type.model_json_schema()
    return out


# 装配一遍要用到的**假数据**。名字对不上、缺值的变量、`selected` 不在 options 里
# ——装配器一律报错（S75 刻意如此：静默跳过会让一整节悄悄消失）。所以写盘前拿
# 这份假 session 跑一遍，装不出来就拒绝并报出是哪一节坏了。
#
# **不跑这一遍的后果**：用户改坏一个字，下一轮生成直接 500，而错误信息离原因很远。
_FAKE_CONTEXTS: dict[str, Any] = {
    "worldinfo": "（试装配）",
    "history": [Message(role=Role.USER, content="（试装配）")],
    "state": "（试装配）",
    "user_input": "（试装配）",
    "character_voices": "（试装配）",
    "upstream": "（试装配）",
    "outline": "（试装配）",
    "prose": "（试装配）",
    "recent": "（试装配）",
    "user_character": "（试装配）",
    "goal": "（试装配）",
    "history_outline": "（试装配）",
    "content": "（试装配）",
}

_FAKE_VALUES = {"goal": "（试装配）", "user_character": "（试装配）", "said": "（试装配）"}


def _dry_run(name: str, data: dict[str, Any]) -> None:
    """把这份配置**装配一遍**，装不出来就抛。

    `sections` 与每一条 `specials` 都要跑到：附属提示词和正文段走的是同一个
    装配器，一条写坏了同样会在运行时炸，而它炸在「模型只回了文本、要推它一句」
    那种偶发路径上——比正文段难查得多。
    """
    assemble(
        name, contexts=_FAKE_CONTEXTS, values=_FAKE_VALUES, sections=data.get("sections") or []
    )
    for key, spec in (data.get("specials") or {}).items():
        if not isinstance(spec, dict):
            raise TypeError(f"specials.{key} 必须是映射")
        assemble(
            name,
            contexts=_FAKE_CONTEXTS,
            values=_FAKE_VALUES | {"open_runs": "3", "listing": "（试装配）"},
            sections=spec.get("sections") or [],
        )


def _render_samples(name: str, data: dict[str, Any]) -> None:
    """`rendering` 里的每一条也过一遍：**缺一条就是运行时 KeyError**。

    它们量小但每轮都进上下文，而且是在渲染函数深处取的——删掉一条不会有任何
    静态检查拦住，直到某一轮真的走到那一句。
    """
    table = data.get("rendering")
    if table is not None and not isinstance(table, dict):
        raise TypeError("rendering 必须是映射")
    for key in load(name).get("rendering") or {}:
        if key not in (table or {}):
            raise KeyError(f"rendering 少了 `{key}`——它在代码里有调用点，删掉会在运行时报错")


def router() -> APIRouter:
    api = APIRouter()

    @api.get("/prompts")
    def list_prompts() -> list[dict[str, Any]]:
        """有哪几份提示词。**列表来自后端，不在前端写死**——加一份就是加一个名字
        加一个文件。

        **分两类，并列，不是主从**（S147）：`agent` 会推演、会产出剧情、有自己的
        工具面，名字是名词；`call` 是一次单次调用（`summarize`、`rerank`），
        输入固定、输出固定、不参与剧情，名字是动词。
        **两类都可编辑**——R31 管的是「模型会读到的字」，不分是谁在读。
        """
        out: list[dict[str, Any]] = []
        for name in PROMPT_FILES:
            data = load(name)
            out.append(
                {
                    "name": name,
                    "group": "agent" if name in AGENTS else "call",
                    "enabled": bool(data.get("enabled", True)),
                    "sections": len(data.get("sections") or []),
                    "tools": len(data.get("tools") or {}),
                    "specials": len(data.get("specials") or {}),
                }
            )
        return out

    @api.get("/prompts/{name}")
    def read_prompt(name: str) -> dict[str, Any]:
        if name not in PROMPT_FILES:
            raise HTTPException(404, f"没有名为 {name} 的提示词")
        # **参数 schema 只给这一份手上那几个**，不是全部——页面上它挨着
        # 描述显示，多给的那些没有位置可放。
        tools = load(name).get("tools") or {}
        schemas = _params()
        return {
            "name": name,
            "data": load(name),
            "params": {t: schemas[t] for t in tools if t in schemas},
        }

    @api.put("/prompts/{name}")
    def write_prompt(name: str, doc: PromptDoc) -> dict[str, Any]:
        """**先试装配再写盘**（R31）。

        装配器对未知的 context、缺值的 `{{var}}`、没写 `role`、`selected` 不在
        options 里一律报错。所以这里拿一份假 session 跑一遍，装不出来就拒绝，
        并把是哪一节坏了报回去——否则用户改坏一个字，下一轮生成直接 500，
        而错误信息离原因很远。
        """
        if name not in PROMPT_FILES:
            raise HTTPException(404, f"没有名为 {name} 的提示词")
        try:
            _render_samples(name, doc.data)
            _dry_run(name, doc.data)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, f"这份配置装配不出来：{exc}") from exc

        save(name, doc.data)
        return {"name": name, "data": load(name)}

    return api


__all__ = ["router"]
