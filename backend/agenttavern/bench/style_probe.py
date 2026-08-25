"""文风诊断探针。**只产数，不判分。**

这些数的用途是**指路**：告诉我该去读哪一段文本。它们不进 `checks.py`，
不进 grader，不参与任何通过/不通过的判定——一旦某个数变成必须满足的指标，
模型就会为了满足它而牺牲别的东西，而检查器永远不会告警（P19）。

参考不是标准答案：六本参考横跨六个题材，同一个数在它们之间的差异往往
比"我们 vs 参考"的差异还大。**只有六本一致、而我们明显在外面的那些数才值得看。**
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

SENT_END = "。！？…"

DEMONSTRATIVE = ["这些", "那些", "这里", "那里", "这个", "那个", "这样", "那样", "这", "那"]
DEGREE = ["非常", "特别", "极其", "十分", "相当", "比较", "有些", "有点", "很", "太", "挺", "更"]
NEGATION = ["不", "没", "无", "未", "非", "别", "莫"]
CONNECTIVE = [
    "因为",
    "所以",
    "但是",
    "然而",
    "而且",
    "如果",
    "虽然",
    "不过",
    "于是",
    "因此",
    "并且",
    "或者",
    "既然",
    "即使",
    "只要",
    "除非",
    "反而",
    "何况",
]
PRONOUN = ["我们", "你们", "他们", "她们", "咱们", "我", "你", "他", "她", "它", "自己"]
PARTICLE = list("啊吧呢嘛呀啦哦喔哈嗯诶哎唉咦唔么嘞")


def paragraphs(text: str) -> list[str]:
    stripped = (ln.strip().strip("　").strip() for ln in text.splitlines())
    return [ln for ln in stripped if ln]


def sentences(text: str) -> list[str]:
    out: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in SENT_END:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _count_longest_first(text: str, words: list[str]) -> int:
    """长词先扣掉，避免「这些」被当成两次「这」。"""
    rest, total = text, 0
    for w in sorted(words, key=len, reverse=True):
        total += rest.count(w)
        rest = rest.replace(w, "\x00" * len(w))
    return total


def tail_particles(text: str) -> int:
    """**只数句末的**：句中的「啊」可能是名字或拟声，句末才确定是语气助词。"""
    return sum(
        1 for i, ch in enumerate(text) if i and ch in SENT_END + "”」" and text[i - 1] in PARTICLE
    )


def measure(text: str) -> dict[str, float]:
    n = len(text)
    paras = paragraphs(text)
    sents: list[str] = [s for p in paras for s in sentences(p)]

    def per_k(count: int) -> float:
        return count / n * 1000

    return {
        "段均字": sum(len(p) for p in paras) / len(paras),
        "句均字": sum(len(s) for s in sents) / len(sents),
        "单句成段%": sum(1 for p in paras if len(sentences(p)) == 1) / len(paras) * 100,
        "对白段%": sum(1 for p in paras if re.search(r"[“「]", p)) / len(paras) * 100,
        "疑问句%": sum(1 for s in sents if "？" in s) / len(sents) * 100,
        "语气词‰": per_k(tail_particles(text)),
        "人称代词‰": per_k(_count_longest_first(text, PRONOUN)),
        "指示代词‰": per_k(_count_longest_first(text, DEMONSTRATIVE)),
        "程度副词‰": per_k(_count_longest_first(text, DEGREE)),
        "否定词‰": per_k(_count_longest_first(text, NEGATION)),
        "连接词‰": per_k(_count_longest_first(text, CONNECTIVE)),
        "把字‰": per_k(text.count("把")),
        "被字‰": per_k(text.count("被")),
        "的‰": per_k(text.count("的")),
        "破折号‰": per_k(text.count("——")),
        "省略号‰": per_k(text.count("…")),
        "叹号‰": per_k(text.count("！")),
    }


def table(named: list[tuple[str, str]]) -> str:
    rows = [(name, measure(text)) for name, text in named]
    keys = list(rows[0][1])
    width = max(len(n) for n, _ in rows) + 2
    head = "指标".ljust(12) + "".join(n.rjust(width) for n, _ in rows)
    out = [head, "-" * len(head)]
    for k in keys:
        out.append(k.ljust(12) + "".join(f"{m[k]:.1f}".rjust(width) for _, m in rows))
    return "\n".join(out)


def _texts(args: argparse.Namespace) -> list[tuple[str, str]]:
    from ..config import load_settings
    from ..storage import Store

    out: list[tuple[str, str]] = []
    for path in sorted(Path(args.refs).glob("*.txt")) if args.refs else []:
        out.append((path.stem, path.read_text(encoding="utf-8")))
    if args.sessions:
        store = Store(load_settings().data_dir)
        for sid in args.sessions:
            repo = store.session(sid)
            session = repo.load_session()
            path_nodes = repo.path_to(session.active_leaf_id) if session.active_leaf_id else []
            joined = "\n".join(n.prose for n in path_nodes if n.prose)
            out.append((sid[-8:], joined))
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        prog="agenttavern.bench.style_probe",
        description="文风诊断：只产数，不判分。参考不是标准答案，见模块说明。",
    )
    p.add_argument("--refs", help="参考文本目录（读其中的 *.txt）")
    p.add_argument("--sessions", nargs="*", default=[], help="要一起量的 session id")
    args = p.parse_args()

    items = _texts(args)
    if not items:
        print("没有可量的文本。用 --refs 指目录，或 --sessions 给 id。")
        return 2
    print(table(items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
