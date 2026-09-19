import { useEffect, useRef, useState } from "react";

import { useSession } from "../store";

/**
 * director 停下来问用户（S24 / S23）。
 *
 * **内联而不是弹窗**：要答这个问题得先读上面那几段正文，弹窗恰好把它挡住。
 *
 * **三个选项之外必须有自由输入**（S23）。用户随时可以都不选、自己写一条，
 * 服务端不校验回答在不在选项里——那第四条路正是最该传下去的一种回答，
 * 而模型没想到它。
 */
export function QuestionPrompt(): JSX.Element | null {
  const question = useSession((s) => s.question);
  const answerQuestion = useSession((s) => s.answerQuestion);
  const [own, setOwn] = useState("");
  const box = useRef<HTMLDivElement>(null);

  const questionId = question?.question_id;

  useEffect(() => {
    setOwn("");
    box.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [questionId]);

  // 1 / 2 / 3 直接选。输入框里打字时不拦。
  useEffect(() => {
    if (!question) return;
    function onKey(event: KeyboardEvent): void {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      const at = Number(event.key) - 1;
      const picked = question!.options[at];
      if (picked === undefined) return;
      event.preventDefault();
      answerQuestion(question!.question_id, picked);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [answerQuestion, question]);

  if (!question) return null;

  function answer(selected: string): void {
    const said = selected.trim();
    if (!said) return;
    answerQuestion(question!.question_id, said);
  }

  return (
    <div ref={box} className="border-t border-line bg-ink-800 px-6 py-4">
      <div className="mx-auto max-w-3xl">
        <p className="mb-3 text-sm text-fg">{question.question}</p>

        <div className="space-y-1.5">
          {question.options.map((option, i) => (
            <button
              key={i}
              onClick={() => answer(option)}
              className="flex w-full gap-2.5 rounded border border-line px-3 py-2 text-left text-xs leading-relaxed text-dim transition-colors hover:border-dim hover:bg-ink-700 hover:text-fg"
            >
              <span className="shrink-0 font-mono text-[11px] text-dim">{i + 1}</span>
              <span className="min-w-0 flex-1">{option}</span>
            </button>
          ))}
        </div>

        <div className="mt-2.5 flex items-center gap-2">
          <input
            value={own}
            onChange={(e) => setOwn(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) answer(own);
            }}
            placeholder="都不选，自己写一条"
            className="flex-1 rounded border border-line bg-ink-900 px-3 py-1.5 text-xs text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
          />
          <button className="btn" onClick={() => answer(own)} disabled={!own.trim()}>
            回答
          </button>
        </div>
      </div>
    </div>
  );
}
