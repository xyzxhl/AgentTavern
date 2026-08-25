"""python -m agenttavern.bench --scenario noir
    python -m agenttavern.bench --scenario data --limit 3
    python -m agenttavern.bench --scenario noir --no-grade
    python -m agenttavern.bench --regrade sess_xxxx   # 只重判，不重新生成

跑完一整条输入序列，做归因，落一份可统计的报告。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from ..config import Settings, load_settings
from ..llm import LLMGateway
from .grader import Judgement, grade_turn
from .report import render
from .runner import BASELINE, BenchResult, load_result, run_scenario
from .scenarios import SCENARIOS


async def _grade_all(
    gateway: LLMGateway,
    result: BenchResult,
    model: str,
    repeat: int,
) -> list[list[Judgement]]:
    judgements: list[list[Judgement]] = []
    for turn in result.turns:
        if turn.node is None:
            judgements.append([])
            continue
        judgements.append(
            await grade_turn(
                gateway,
                user_text=turn.user_text,
                outline=turn.outline,
                prose=turn.prose,
                state_before=turn.state_before,
                delta=turn.node.state_delta,
                model=model,
                repeat=repeat,
            )
        )
        print(f"  [{turn.index + 1}] 归因完成")
    return judgements


def _write(
    args: argparse.Namespace,
    result: BenchResult,
    judgements: list[list[Judgement]],
    model: str,
    grader_model: str,
) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%m%d-%H%M%S")
    path = out_dir / f"{stamp}-{result.scenario}-{result.variant}.md"
    path.write_text(
        render(result, judgements, model=model, grader_model=grader_model),
        encoding="utf-8",
    )
    print(f"\n汇总：{result.totals()}")
    print(f"session：{result.session_id}")
    print(f"报告：{path}")
    return 0


def _progress(turn: object) -> None:
    n = getattr(turn, "index", 0) + 1
    if getattr(turn, "failure", None):
        print(f"  [{n}] 失败：{turn.failure}")  # type: ignore[attr-defined]
    else:
        print(
            f"  [{n}] 细纲 {len(turn.outline)} / 正文 {len(turn.prose)}"  # type: ignore[attr-defined]
            f"  标记 {len(turn.notes)}"  # type: ignore[attr-defined]
        )


async def _main(args: argparse.Namespace) -> int:
    settings: Settings = load_settings()
    if not settings.llm_model:
        print("!! .env 里没有 LLM_MODEL", file=sys.stderr)
        return 2

    grader_model = args.grader_model or settings.llm_model

    if args.regrade:
        # 只重判，不重新生成——grader 改动前后必须比在同一批素材上，
        # 否则 grader 的差异和模型本身的随机性会混在一起。
        result = load_result(settings, args.regrade)
        print(f"重判 {args.regrade}（{len(result.turns)} 轮）  grader {grader_model}")
        async with LLMGateway(settings) as gateway:
            judgements = await _grade_all(gateway, result, grader_model, args.repeat)
        return _write(args, result, judgements, settings.llm_model, grader_model)

    scenario = SCENARIOS[args.scenario]
    print(f"场景 {scenario.name}（{len(scenario.inputs)} 轮）  模型 {settings.llm_model}")

    async with LLMGateway(settings) as gateway:
        result = await run_scenario(
            scenario,
            gateway,
            settings,
            variant=BASELINE,
            limit=args.limit,
            on_turn=_progress,
        )
        judgements = []
        if not args.no_grade:
            print("归因中……")
            judgements = await _grade_all(gateway, result, grader_model, args.repeat)

    return _write(args, result, judgements, settings.llm_model, grader_model)


def main() -> int:
    p = argparse.ArgumentParser(prog="agenttavern.bench")
    p.add_argument("--scenario", default="noir", choices=sorted(SCENARIOS))
    p.add_argument("--limit", type=int, help="只跑前 N 轮")
    p.add_argument("--regrade", help="只重判已有 session 的素材，不重新生成")
    p.add_argument("--grader-model", help="归因用的模型，默认与生成同一个")
    p.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="每类判几次，只保留复现率——随机 grader 的单次判定是噪声（P19）",
    )
    p.add_argument("--no-grade", action="store_true", help="只采集不归因")
    p.add_argument("--out", default="bench", help="报告目录")
    return asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
