"""冒烟：跑一个真实的 run，把输入 / 输出 / trace 全存下来。

    python -m agenttavern.smoke "我推开门"

这是优化流程阶段 0 的观测工具（docs/Optimization.md）。**它不判断质量**，只把
模型实际看到什么、实际吐出什么、卡在哪里如实记下来。

**在 gateway 层录制**，因此存的是模型真正收到的 messages 与真正返回的内容，
不是我们以为它收到的——这个区别在调 prompt 时至关重要。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .bench.metrics import Metrics, RecordingGateway, collect
from .bench.scenarios import SCENARIOS
from .config import Settings, load_settings
from .models import Session, TraceEvent, UserInput
from .orchestration import execute_run
from .prompting import call_of, selected
from .storage import Store

# --------------------------------------------------------------------------
# 场景——与回归集共用同一批素材，避免两处漂移
# --------------------------------------------------------------------------

WORLDS = {name: (s.make_session, s.worldbook, s.world_name) for name, s in SCENARIOS.items()}


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def _fence(text: str, lang: str = "") -> str:
    return f"```{lang}\n{text}\n```"


def render_report(
    *,
    settings: Settings,
    session: Session,
    user_text: str,
    gateway: RecordingGateway,
    metrics: Metrics,
    events: list[TraceEvent],
    node: Any,
    outline: str,
    notes: list[str],
    failure: str | None,
) -> str:
    out: list[str] = [
        f"# 冒烟 · {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        f"- 模型：`{call_of('director').model}` @ `{settings.llm_base_url}`",
        f"- 结果：{'**失败** — ' + failure if failure else '成功'}",
        f"- 指标：{json.dumps(metrics.as_dict(), ensure_ascii=False)}",
        # 配置整体上调到了提示词层（S80），session 上没有它们了。记下**当前
        # 选的哪几档**——冒烟报告要能回答"这份产出是在什么设定下跑出来的"。
        f"- 文风 / 视角 / 铺陈：{selected('writer', 'prose_style')} / "
        f"{selected('writer', 'pov')} / {selected('writer', 'density')}",
        "",
        "## 用户输入",
        "",
        _fence(user_text),
        "",
    ]

    for i, ex in enumerate(gateway.exchanges):
        who = "director" if ex.tools else "writer / 窄调用"
        out += [f"## 调用 {i + 1}（{who}）", ""]

        if ex.tools:
            names = "`, `".join(t.name for t in ex.tools)
            out += [f"工具面（{len(ex.tools)}）：`{names}`", ""]

        out += ["<details><summary>发出的 messages</summary>", ""]
        for m in ex.messages:
            head = f"**{m.role.value}**"
            if m.tool_call_id:
                head += f" (tool_call_id={m.tool_call_id})"
            out += [head, "", _fence(m.content or "（空）"), ""]
            for tc in m.tool_calls:
                out += [f"→ 调用 `{tc.name}`", _fence(tc.arguments_raw, "json"), ""]
        out += ["</details>", ""]

        if ex.error:
            out += ["**调用失败**", "", _fence(ex.error), ""]
            continue

        r = ex.response
        assert r is not None
        out += [
            f"**返回**（in={r.usage.tokens_in} out={r.usage.tokens_out} finish={r.finish_reason}）",
            "",
        ]
        if r.content:
            out += [_fence(r.content), ""]
        for tc in r.tool_calls:
            flag = f" ⚠️ 解析失败：{tc.parse_error}" if tc.parse_error else ""
            out += [f"→ `{tc.name}`{flag}", _fence(tc.arguments_raw, "json"), ""]
        if not r.content and not r.tool_calls:
            out += ["（既无文本也无工具调用）", ""]

    if node is not None:
        out += [
            "## 产出",
            "",
            f"### 细纲（{len(outline)} 字）",
            "",
            _fence(outline),
            "",
            f"### 正文（{len(node.prose)} 字）",
            "",
            _fence(node.prose),
            "",
            "### 偏离报告",
            "",
            _fence(node.deviation_report or "（无）"),
            "",
            "### state 增量",
            "",
            _fence(node.state_delta.model_dump_json(indent=2), "json"),
            "",
        ]

    out += ["## 采样标记", "", (("- " + "\n- ".join(notes)) if notes else "（无）"), ""]

    out += ["## trace", "", "| # | actor | type | label | tok |", "|---|---|---|---|---|"]
    for e in events:
        label = e.label.replace("|", "\\|")
        tok = f"{e.tokens_in or ''}/{e.tokens_out or ''}".strip("/")
        out.append(f"| {e.seq} | {e.actor.value} | {e.type.value} | {label} | {tok} |")

    out += ["", "<details><summary>trace payload</summary>", ""]
    for e in events:
        if e.payload:
            body = json.dumps(e.payload, ensure_ascii=False, indent=2)
            out += [f"**{e.seq} {e.label}**", _fence(body, "json"), ""]
    out += ["</details>", ""]

    return "\n".join(out)


# --------------------------------------------------------------------------


async def run_once(args: argparse.Namespace) -> int:
    settings = load_settings()
    if not call_of("director").model:
        print("!! director.yaml 顶上没写 model", file=sys.stderr)
        return 2

    store = Store(settings.data_dir)
    if args.session:
        repo = store.session(args.session)
        session = repo.load_session()
    else:
        make_session, worldbook, world_name = WORLDS[args.world]
        session = make_session()
        repo = store.create_session(session)
        # 设定是一级资产（S63），而它只有条目（S64）——名字是唯一的字段。
        book = repo.book()
        book.save(book.load().model_copy(update={"name": world_name}))
        for entry in worldbook:
            book.save_entry(entry)

    gateway = RecordingGateway(settings)
    events: list[TraceEvent] = []
    node, outline, notes, failure = None, "", [], None

    async def ask(question: str, options: list[str]) -> str:
        # 非交互：取第一个选项，报告里会如实记下来。
        print(f"   [ask_question] {question} -> 自动选「{options[0]}」")
        return options[0]

    print(f"session={session.id}  model={call_of('director').model}")
    print(f"输入：{args.text}")

    try:
        outcome = await execute_run(
            repo,
            session,
            gateway,
            user_input=UserInput(text=args.text),
            parent_id=session.active_leaf_id,
            ask_user=ask,
            on_trace=events.append,
        )
        node, outline, notes = outcome.node, outcome.outline, outcome.notes
    except Exception as exc:  # noqa: BLE001 —— 失败的 run 在阶段 0 最有信息量
        failure = f"{type(exc).__name__}: {exc}"
        print(f"!! {failure}", file=sys.stderr)
    finally:
        await gateway.aclose()

    metrics = collect(events)
    report = render_report(
        settings=settings,
        session=session,
        user_text=args.text,
        gateway=gateway,
        metrics=metrics,
        events=events,
        node=node,
        outline=outline,
        notes=notes,
        failure=failure,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%m%d-%H%M%S")
    path = out_dir / f"{stamp}-{session.id}.md"
    path.write_text(report, encoding="utf-8")

    print()
    print(f"指标：{json.dumps(metrics.as_dict(), ensure_ascii=False)}")
    if notes:
        print(f"采样标记：{notes}")
    if node is not None:
        print(f"细纲 {len(outline)} 字 / 正文 {len(node.prose)} 字")
    print(f"报告：{path}")
    return 1 if failure else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="agenttavern.smoke")
    p.add_argument("text", help="用户输入")
    p.add_argument("--session", help="续跑已有 session")
    p.add_argument(
        "--world",
        default="noir",
        choices=sorted(WORLDS),
        help="新建 session 用哪个世界；data 是数据化世界，用来压 state 判据",
    )
    p.add_argument("--out", default="smoke", help="报告目录")
    return asyncio.run(run_once(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
