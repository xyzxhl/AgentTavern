"""开发期的 Vite 代理表必须覆盖后端所有的 REST 前缀。

**这是同一个失败模式的第三次**，所以这次用测试钉住而不是再写一条注释。

漏一条前缀不会报错：Vite 会把那个请求当成前端路由、**回一份 `index.html`
且状态码 200**，于是 `res.ok` 为真，异常一直拖到 `res.json()` 才发生，
而那时的报错（`Unexpected token '<'`）离原因很远。

前两次：
1. `/worldbooks` 整个失灵过（R17），`api.ts` 的 `json()` 因此加了 content-type
   校验——**让它炸得更早更响，但仍然要等到运行时**。
2. 路由从 `/agents` 改名成 `/prompts`（S147）时代理表没跟着改，
   **提示词管理页在开发环境坏了好几个提交都没人发现**——因为那一页
   在生产构建里是好的，而没人在开发环境点开过它。

第二次那个尤其说明问题：**加前缀会被想起来，改名不会。**
"""

from __future__ import annotations

import re
from pathlib import Path

import agenttavern
from agenttavern.config import Settings
from agenttavern.server import create_app

VITE_CONFIG = Path(agenttavern.__file__).parent.parent.parent / "frontend" / "vite.config.ts"

IGNORED = {"openapi.json", "docs", "redoc"}
"""FastAPI 自带的那几个。前端从不请求它们。"""


def backend_prefixes() -> set[str]:
    """后端注册了哪些顶层 REST 前缀。WebSocket 那条也算——它走同一张代理表。

    **必须递归，而且要认对那个包装。** `include_router` 进来的那几个在这一版
    FastAPI 里是 `_IncludedRouter`：它自己没有 `.path`，也**没有 `.routes`**，
    真正的路由挂在 `.original_router.routes` 上。

    只看顶层的话 `/corpora` 与 `/prompts` **一个都数不到，而这条检查看起来
    仍然在工作**——写这条测试时第一版就是这么错的，第二版按 `.routes` 递归，
    照样是错的。**一个覆盖不全却全绿的检查，比没有更糟。**
    """
    out: set[str] = set()

    def walk(routes: object) -> None:
        for route in routes or []:  # type: ignore[union-attr]
            path = getattr(route, "path", "")
            if path:
                head = path.strip("/").split("/")[0]
                if head and head not in IGNORED:
                    out.add(head)
            nested = getattr(route, "original_router", None) or route
            if nested is not route:
                walk(getattr(nested, "routes", None))

    walk(create_app(Settings(llm_model="m"), None).routes)
    assert out, "一个前缀都没数到——枚举方式又和这一版 FastAPI 对不上了"
    return out


def proxied_prefixes() -> set[str]:
    text = VITE_CONFIG.read_text(encoding="utf-8")
    block = text[text.index("proxy: {") :]
    return set(re.findall(r'"/([a-z_-]+)":\s*\{', block))


def test_every_backend_prefix_is_proxied_in_dev() -> None:
    """**加一条 REST 前缀、或给一条改名，两个文件都要动。**

    漏了的症状是那一页在开发环境整个失灵，而生产构建里一切正常——
    于是它可以坏很久没人发现。
    """
    missing = backend_prefixes() - proxied_prefixes()
    assert not missing, (
        f"这些前缀后端有、Vite 代理表里没有：{sorted(missing)}。"
        f"往 {VITE_CONFIG.name} 的 proxy 里各加一行，否则开发环境请求它们会拿到 index.html。"
    )


def test_the_proxy_table_has_no_leftovers() -> None:
    """**反过来也要查：代理表里不该有后端已经没有的前缀。**

    留着的那条不会报错，它只是永远不会被命中——而它看起来像"这条还在用"，
    于是改名之后的那次遗漏被它掩盖住了。这正是 `/agents` → `/prompts`
    那次的实际经过。
    """
    stale = proxied_prefixes() - backend_prefixes()
    assert not stale, f"这些前缀 Vite 代理表里有、后端已经没有了：{sorted(stale)}"
