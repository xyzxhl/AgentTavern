"""writer 单独调：**细纲固定，只动提示词。**

## 为什么必须固定细纲

writer 是一次输入一次输出、没有工具的纯渲染（S3）。它的输入只有三样：
文风提示词、`## 上文`、`## 本轮细纲`。

但此前每次"调 writer"都是跑整条 `bench --scenario pulp`，**那会把细纲一起重新
生成**。于是前后两次比较的是「不同的提示词 + 不同的细纲」写出的正文，
**writer 的变化和 director 的变化混在一起，归不了因**。连着三轮都是这么比的。

这里把细纲钉死：从一个已跑完的 session 里取出**落盘的细纲和上文原文**，
反复只让 writer 重写。这样唯一的变量就是提示词（或采样参数）。

## 为什么要重复多次

思考模式默认开启，而**开启时 `temperature` 被静默忽略**（见 `LLMGateway.complete`），
所以同一份输入每次产出都不同。单次对照读到的差异可能纯粹是抖动。
`--repeat` 跑多次，看的是**分布**而不是某一次。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from ..agents.writer import DEVIATION_MARKER
from ..config import Settings, load_settings
from ..llm import LLMGateway, Message
from ..models import WorldbookEntry
from ..prompting import call_of, upstream_runs, writer_prompt
from ..storage import Store
from ..tracing import outline_from_trace


@dataclass(frozen=True)
class Take:
    """一份固定的 writer 输入。正文取自节点，临时细纲取自开发 trace。"""

    index: int
    outline: str
    upstream: str
    original: str
    """原来那一次写出的正文，作为对照留着。"""


@dataclass
class Rendering:
    take: int
    attempt: int
    prose: str
    deviation: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class LabResult:
    session_id: str
    takes: list[Take] = field(default_factory=list)
    renderings: list[Rendering] = field(default_factory=list)

    def prose_of(self, take: int) -> list[str]:
        return [r.prose for r in self.renderings if r.take == take]


def load_takes(
    settings: Settings, session_id: str, limit: int | None = None
) -> tuple[list[WorldbookEntry], list[Take]]:
    """把一个 session 的每一轮拆成一份固定输入。

    `上文` 用**当时落盘的正文**，不是这次重写出来的——否则第 n 轮的输入会随
    第 n-1 轮的重写而变，细纲固定了上文却没固定，等于没固定。
    """
    repo = Store(settings.data_dir).session(session_id)
    session = repo.load_session()
    path = repo.path_to(session.active_leaf_id) if session.active_leaf_id else []

    takes: list[Take] = []
    for i, node in enumerate(path):
        outline = outline_from_trace(repo, node.id)
        if not outline:
            continue
        window = upstream_runs()
        upstream = "\n\n".join(n.prose for n in path[max(0, i - window) : i] if n.prose)
        takes.append(Take(index=i + 1, outline=outline, upstream=upstream, original=node.prose))
    # 文风实验要的是角色语言习惯与用户扮演谁——两样都在**条目**上（S65），
    # worldbook 已经没有字段了（S64）。文风与视角也不在 session 上了（R31），
    # 它们是 `writer.yaml` 里那两个 `choice` 的 `selected`。
    return repo.worldbook(), takes[:limit] if limit else takes


TERSE_CRAFT = """用网文的笔法写，像讲给人听，别端着。
台词由你写，细纲只给了意思；话里的信息不能改。"""
"""**收束版**：把「台词 / 落笔 / 避开定式」三节（约 1400 字细则）换成两句话。

问的是一个很实在的问题：**那 1400 字在挣它占的位置吗。** 细则越多，
提示词越贵、越难改、越容易内部打架（`_CRAFT` 压过 `prose_style` 就是一例）。
如果两句话能到同样的水平，那些细则就该删。

保留的两点是**不能靠"文风"二字推出来的契约**：台词归 writer（S89），
信息不能改（S8 的前提）。其余全是手艺建议，而手艺建议正是模型自己有的。
"""


_CRAFT_HEAD = "# 人物思绪描写"
_DIALOGUE_HEAD = "细纲中的对白不是台词草稿。"
"""认出那两节的头一句。**装配器不再自动包标签**（R31），所以不能再按
`<创作指导>` 这样的前缀去认——那对标签现在根本不存在了。

认头一句是脆的：改了 `writer.yaml` 里那两节的开头，这里就认不出来。所以
`_terse` 认不全时**直接报错**，不静默放过——一个消融跑出来和基线一模一样，
比报错难查得多（同 P29：没生效的东西不该悄悄没生效）。
"""


def _terse(messages: list[Message]) -> list[Message]:
    """把三节细则整段换掉，其余原样。

    一个条目一条消息之后（S9），这就是**按整条消息增删**，
    不再是在一大块文本里找下标切片。
    """
    out: list[Message] = []
    swapped = dropped = False
    for message in messages:
        body = (message.content or "").lstrip()
        if body.startswith(_CRAFT_HEAD):
            out.append(message.model_copy(update={"content": TERSE_CRAFT}))
            swapped = True
        elif body.startswith(_DIALOGUE_HEAD):
            dropped = True
        else:
            out.append(message)
    if not (swapped and dropped):
        raise ValueError(
            "writer.yaml 里认不出「创作指导」或「台词」那一节——"
            "改过开头的话，同步改 _CRAFT_HEAD / _DIALOGUE_HEAD"
        )
    return out


async def render_once(
    gateway: LLMGateway,
    worldinfo: list[WorldbookEntry],
    take: Take,
    *,
    thinking: bool | None = None,
    terse: bool = False,
) -> Rendering:
    messages = writer_prompt(
        outline=take.outline, upstream=take.upstream, worldinfo=worldinfo
    ).messages
    if terse:
        messages = _terse(messages)

    # **thinking 走 `extra` 透传**（S159）：各家拼法不同，这里发的是 DeepSeek
    # 那个形状——writer_lab 是对着一家端点做的实验工具，不必可移植。
    call = call_of("writer")
    if thinking is not None:
        mode = {"type": "enabled" if thinking else "disabled"}
        call = call.model_copy(update={"extra": {**call.extra, "thinking": mode}})
    response = await gateway.complete(messages, call=call)
    raw = response.content
    prose, _, report = raw.partition(DEVIATION_MARKER)
    return Rendering(
        take=take.index,
        attempt=0,
        prose=prose.strip(),
        deviation=report.strip() or None,
        tokens_in=response.usage.tokens_in,
        tokens_out=response.usage.tokens_out,
    )


async def run_lab(
    settings: Settings,
    session_id: str,
    *,
    repeat: int = 1,
    limit: int | None = None,
    thinking: bool | None = None,
    terse: bool = False,
) -> LabResult:
    worldinfo, takes = load_takes(settings, session_id, limit)
    result = LabResult(session_id=session_id, takes=takes)

    async with LLMGateway(settings) as gateway:
        for take in takes:
            for attempt in range(repeat):
                rendering = await render_once(
                    gateway, worldinfo, take, thinking=thinking, terse=terse
                )
                rendering.attempt = attempt + 1
                result.renderings.append(rendering)
                print(f"  第{take.index}轮 第{attempt + 1}次：{len(rendering.prose)} 字")
    return result


def main() -> int:
    p = argparse.ArgumentParser(prog="agenttavern.bench.writer_lab")
    p.add_argument("session", help="从哪个 session 取固定的细纲")
    p.add_argument("--repeat", type=int, default=1, help="每份细纲重写几次")
    p.add_argument("--limit", type=int, help="只取前 N 轮")
    p.add_argument(
        "--thinking",
        choices=["on", "off"],
        help="显式开关思考模式。不传则用服务端默认（DeepSeek 上是开启）",
    )
    p.add_argument(
        "--terse",
        action="store_true",
        help="收束版提示词：把台词/落笔/避开定式三节换成两句话",
    )
    p.add_argument("--out", help="把产出写到这个文件")
    args = p.parse_args()

    settings = load_settings()
    model = call_of("writer").model
    if not model:
        print("!! writer.yaml 顶上没写 model", file=sys.stderr)
        return 2

    thinking = None if args.thinking is None else args.thinking == "on"
    print(f"细纲取自 {args.session}　模型 {model}　thinking={args.thinking or '默认'}")
    result = asyncio.run(
        run_lab(
            settings,
            args.session,
            repeat=args.repeat,
            limit=args.limit,
            thinking=thinking,
            terse=args.terse,
        )
    )

    lines: list[str] = []
    for take in result.takes:
        lines += [f"===== 第{take.index}轮 细纲 =====", take.outline, ""]
        for i, prose in enumerate(result.prose_of(take.index), 1):
            lines += [f"----- 重写 {i} -----", prose, ""]
    text = "\n".join(lines)
    if args.out:
        from pathlib import Path

        Path(args.out).write_text(text, encoding="utf-8")
        print(f"产出：{args.out}")
    else:
        print(text)

    total_in = sum(r.tokens_in for r in result.renderings)
    total_out = sum(r.tokens_out for r in result.renderings)
    print(f"\n共 {len(result.renderings)} 次重写　tokens_in {total_in}　tokens_out {total_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
