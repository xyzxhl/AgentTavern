import { useState } from "react";

import { useSession } from "../store";

/**
 * 输入框。**跑着的时候不禁用**——输入进队列，每条独立成一个 run（D26）。
 * 禁用等于逼用户盯着屏幕等，而排队本来就是设计的一部分。
 */
export function Composer(): JSX.Element {
  const [text, setText] = useState("");
  const phase = useSession((s) => s.phase);
  const queue = useSession((s) => s.queue);
  const resumable = useSession((s) => s.resumable);
  const send = useSession((s) => s.send);

  function submit(): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    send({ type: "submit", text: trimmed });
    setText("");
  }

  return (
    <div className="border-t border-line px-6 py-3">
      <div className="mx-auto max-w-3xl">
        {queue.length > 0 && (
          <p className="mb-2 text-xs text-dim">排队中 {queue.length} 条：{queue.join(" · ")}</p>
        )}
        {resumable && (
          <p className="mb-2 text-xs text-dim">
            上一轮挂起成了草稿，下一条输入会接着它跑。
          </p>
        )}

        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              // Enter 发送，Shift+Enter 换行——正文输入常有多段。
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                submit();
              }
            }}
            rows={2}
            placeholder="推进剧情，或用（括号）写元指令"
            className="max-h-48 min-h-[3rem] flex-1 resize-none rounded border border-line bg-ink-800 px-3 py-2 text-sm text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
          />
          <div className="flex flex-col gap-1.5">
            <button className="btn" onClick={submit} disabled={!text.trim()}>
              发送
            </button>
            <button
              className="btn"
              onClick={() => send({ type: "stop" })}
              disabled={phase === "idle"}
            >
              停止
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
