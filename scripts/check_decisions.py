"""决策台账的体检。**这是 spec 与 code 之间那条链路的守卫**（见 docs/Workflow.md）。

代码、测试与文档里有八百多处 `D<n>` 引用。它们是**手写的**，所以会烂：改设计时
只改了台账没改注释、只改了注释没改台账、引了一条早就被推翻的决策当活理由。
人读不出来——一处引用长得跟另一处一模一样。

三条硬规则，违反就退出码非零：

1. **悬空引用**——引了台账里不存在的编号。多半是打错字或删条目时漏了引用。
2. **重号**——同一个编号出现两次。编号是引用的唯一键，重了就没法确定指谁。
3. **引用已划线的决策**——划线表示"整条都不再做"，而代码正拿它当理由。
   修法有两种：改成引取代它的那条；或者这条其实记录的是一条活着的
   "不做什么"规则，那就去掉划线、把标题改写成直接的否定句。

**"无人引用的决策"只是提示，不算失败**：不少决策是方法论或产品定位，本来就
落不到某一行代码上。

有一种合法的例外：**把某条决策当反面标本举例**（`docs/Workflow.md` 的"不要/要"
对照表就是），那不是拿它当理由。这种地方用一对注释把范围圈掉——必须显式写，
不做启发式猜测：

    <!-- check-decisions: off -->
    ... 这几行里的 D 编号不算引用 ...
    <!-- check-decisions: on -->

Python 与 TS 里同样识别（`# check-decisions: off` / `// check-decisions: off`）。

    python scripts/check_decisions.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "docs" / "Decisions.md"

# **扫全部被 git 跟踪的文件**，只按后缀过滤。曾经这里列的是几棵子树
# （`backend/agenttavern`、`frontend/src`、`docs` …），结果 `backend/pyproject.toml`
# 和仓库根的 `dev.ps1` 都在名单外——它们里面的 D 引用一处也检查不到，而
# **一个有盲区的检查器比没有检查器更坏**：它会给出"全都查过了"的错觉。
SUFFIXES = {".py", ".ts", ".tsx", ".mjs", ".yaml", ".yml", ".toml", ".ps1", ".md"}

# 第三方素材，不是我们的字；里面的 `D<数字>` 是巧合。
SKIP_PREFIXES = ("docs/sillytavern_preset_example/",)


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [ROOT / rel for rel in out if not rel.startswith(SKIP_PREFIXES)]


OFF = re.compile(r"check-decisions:\s*off")
ON = re.compile(r"check-decisions:\s*on")


def cited(body: str) -> set[str]:
    """文件里**当作理由引用**的 D 编号。

    `check-decisions: off` / `on` 之间的行不算——那是把某条决策当反面标本举例。
    落单的 `off`（忘了写 `on`）就一直关到文件末尾，这是刻意的：宁可漏报，也
    不要因为一对标记没配平而把整个 CI 判红。
    """
    found: set[str] = set()
    live = True
    for line in body.splitlines():
        if OFF.search(line):
            live = False
        elif ON.search(line):
            live = True
        elif live:
            found.update(re.findall(r"\bD\d{1,3}\b", line))
    return found


def main() -> int:
    text = LEDGER.read_text(encoding="utf-8")

    # 台账里"被定义"的条目 = 表格首列，允许两侧的删除线。
    defined = Counter(re.findall(r"^\|\s*~*(D\d+)~*\s*\|", text, re.M))
    struck = set(re.findall(r"^\|\s*~~(D\d+)~~\s*\|", text, re.M))

    refs: dict[str, set[str]] = {}
    for path in tracked_files():
        if path.suffix.lower() not in SUFFIXES:
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel == "docs/Decisions.md":  # 台账引用自己是正常的
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        for d in cited(body):
            refs.setdefault(d, set()).add(rel)

    problems: list[str] = []

    dupes = sorted(d for d, n in defined.items() if n > 1)
    if dupes:
        problems.append(f"重号：{', '.join(dupes)}——编号是引用的唯一键，不能重复")

    dangling = sorted((d for d in refs if d not in defined), key=lambda d: int(d[1:]))
    for d in dangling:
        problems.append(f"悬空引用 {d}：台账里没有这一条 ← {', '.join(sorted(refs[d]))}")

    zombie = sorted((d for d in refs if d in struck), key=lambda d: int(d[1:]))
    for d in zombie:
        problems.append(
            f"引用了已划线的 {d} ← {', '.join(sorted(refs[d]))}\n"
            f"      改成引取代它的那条；若这条记的其实是一条活着的"
            f"「不做什么」，就去掉划线、标题改写成否定句"
        )

    print(f"台账 {len(defined)} 条（划线 {len(struck)}），被引用 {len(refs)} 个编号")

    orphan = sorted((d for d in defined if d not in refs), key=lambda d: int(d[1:]))
    if orphan:
        print(f"提示：{len(orphan)} 条无人引用（不算失败）：{' '.join(orphan)}")

    if problems:
        print(f"\n发现 {len(problems)} 个问题：", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1

    print("台账与引用一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
