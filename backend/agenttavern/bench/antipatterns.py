"""八股探针：社区实测出来的中文 LLM 文风反模式。

来源是 `docs/sillytavern_preset_example/` 里几份 preset 的「杀八股」正则
（狐神、夏瑾、双人成行）。**这批模式比自造指标可信**，理由不在于它们更精确，
而在于**它们是被需求验证过的**：有人真的被这些句式烦到要写正则去删。

同样值得注意的是**他们选择的解法**：事后正则删除，而不是在提示词里禁止。
这与实测一致（P27：对模型原生语体的引导推不动）。所以这里也**只量不改**——
量出来是为了知道我们和它们有多近，不是为了加一条硬约束（P19）。

## 一条重要的保留

正则会误伤。`突然` 有时就是该用的词，`一丝` 在某些句子里没有问题。
所以这些数**只是密度，不是错误数**——它回答"我们的产出里这类句式有多密集"，
不回答"这一处该不该改"。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# 每一条都注明来自哪份 preset，便于回溯。
PATTERNS: dict[str, str] = {
    # 「不是X，而是Y」：被两份 preset 分别写了正则，是最被公认的一条
    "不是而是": r"(不是|并非)[\s\S]{0,40}?(?<!不)是",
    # 转折副词滥用
    "突然忽然": r"突然|忽然",
    # 量词八股
    "一丝一抹": r"一丝+|一抹|弧度",
    # 「不易察觉」家族
    "微不可察": r"(不容置疑|(不易|难以)(觉察|察觉)|(微|几)不可(查|察|闻))",
    # 指关节泛白
    "指节泛白": r"指(关节|节|尖)[^，,。]{0,10}?白|泛白",
    # 「哈基米八股」词表
    "书面腔词": r"由于|极其|生理性?的?|病态的|霸道地?|死死[的地]",
    # 语气/尾音/音调 + 比喻
    "语气比喻": r"(语气|语调|声音|尾音|音调)[一-龥]{0,6}[，,]?得?(如同|像|仿佛)",
    # 「他的话像……」
    "话像比喻": r"话(像|如同|仿佛)",
    # 「一个动作/一个反应/一个认知/一个笑容」
    "一个X": r"(个动作|个反应|个认知|个笑容)",
    # 破折号：狐神直接 /——/g 替换掉
    "破折号": r"——",
}

ENDING_FORMULAS: dict[str, str] = {
    # **只看结尾**：收尾定式的战场在最后一两句，全文扫会把正常的「等」也算进来。
    "在等收尾": r"等[^。，]{0,8}(接|回答|开口|反应|答|说话|表态|决定)|在等|等着",
}
"""结尾定式。**和上面那批分开，因为它只在结尾成立**。

这一项的价值由一次实测确立：把 writer 提示词的「台词 / 落笔 / 避开定式」三节
（1109 字）换成两句话，**上面所有指标与八股密度全都没有差别**，而结尾定式
从 **0/30 变成 5/10**。

也就是说：**17 项文风指标 + 10 项八股，没有一项看得出这个差别**，而它是
S90 花两轮才治好的东西。这条因此是「统计看不见但真实存在」的现成例子，
留在这里当回归探针——**只量不拦**（P19），它的用途是让下一次"精简提示词"
的念头有个地方撞一下。
"""


def ending_formulas(text: str, tail: int = 60) -> dict[str, int]:
    end = text[-tail:]
    return {name: len(re.findall(p, end)) for name, p in ENDING_FORMULAS.items()}


def density(text: str) -> dict[str, float]:
    n = max(len(text), 1)
    return {name: len(re.findall(p, text)) / n * 1000 for name, p in PATTERNS.items()}


def table(named: list[tuple[str, str]]) -> str:
    rows = [(name, density(text)) for name, text in named]
    width = max(len(n) for n, _ in rows) + 2
    head = "八股".ljust(10) + "".join(n.rjust(width) for n, _ in rows)
    out = [head, "-" * len(head)]
    for key in PATTERNS:
        out.append(key.ljust(10) + "".join(f"{m[key]:.2f}".rjust(width) for _, m in rows))
    totals = ["合计".ljust(10)]
    totals += [f"{sum(m.values()):.2f}".rjust(width) for _, m in rows]
    out.append("".join(totals))
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(prog="agenttavern.bench.antipatterns")
    p.add_argument("--refs", help="参考文本目录")
    p.add_argument("--files", nargs="*", default=[], help="要一起量的文本文件")
    args = p.parse_args()

    items: list[tuple[str, str]] = []
    if args.refs:
        for path in sorted(Path(args.refs).glob("*.txt")):
            items.append((path.stem, path.read_text(encoding="utf-8")))
    for f in args.files:
        items.append((Path(f).stem[-12:], Path(f).read_text(encoding="utf-8")))

    if not items:
        print("没有可量的文本。")
        return 2
    print(table(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
