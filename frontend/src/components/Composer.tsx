import { useState } from "react";

import { useSession } from "../store";

/**
 * 输入框。**只有一个按钮**，按两件事变形：模型在不在跑、框里有没有字。
 *
 * | | 空 | 有内容 |
 * |---|---|---|
 * | 空闲 | 发送（灰，按不动） | 发送 |
 * | 运行中 | **中断** | 发送 |
 *
 * 没话要说的时候，那一下唯一的意思就是「别再往下跑了」；有话要说就永远是发送——
 * 跑着的时候发送进队列，作为新的一轮（D26：每条输入独立成一个 run，不打包）。
 *
 * **引导走 Ctrl+Enter**（D119）：它插进正在跑的这一轮、落在操作边界上，和排队
 * 是两件事。不给它单独的按钮，是因为按钮一多，「发送」是干什么的就得先想一下；
 * 而引导本来就是少数情况。
 */
export function Composer(): JSX.Element {
  const [text, setText] = useState("");
  const phase = useSession((s) => s.phase);
  const queue = useSession((s) => s.queue);
  const resumable = useSession((s) => s.resumable);
  const send = useSession((s) => s.send);

  const said = text.trim();
  // `asking` 也算在飞：那一轮挂起在等回答，没有结束。
  const inFlight = phase !== "idle";
  const stopping = inFlight && !said;

  function submit(): void {
    if (!said) return;
    send({ type: "submit", text: said });
    setText("");
  }

  /** 插进**正在跑的这一轮**，落在下一个操作边界上，不掐掉在飞的那次操作。 */
  function guide(): void {
    if (!said) return;
    send({ type: "interject", text: said, interrupt: false });
    setText("");
  }

  function press(): void {
    if (stopping) send({ type: "stop" });
    else submit();
  }

  return (
    <div className="border-t border-line px-6 py-3">
      <div className="mx-auto max-w-3xl">
        {queue.length > 0 && (
          <p className="mb-2 text-xs text-dim">
            排队中 {queue.length} 条：{queue.join(" · ")}
          </p>
        )}
        {resumable && (
          <p className="mb-2 text-xs text-dim">上一轮挂起成了草稿，下一条输入会接着它跑。</p>
        )}

        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== "Enter" || e.nativeEvent.isComposing) return;
              // Shift+Enter 换行——正文输入常有多段。
              if (e.shiftKey) return;
              e.preventDefault();
              // Ctrl/Cmd+Enter 直接走引导，不排队。
              if (e.ctrlKey || e.metaKey) guide();
              else press();
            }}
            rows={2}
            placeholder={
              inFlight ? "排队等下一轮，或 Ctrl+Enter 插进正在跑的这一轮" : "推进剧情，或用（括号）写元指令"
            }
            className="max-h-48 min-h-[3rem] flex-1 resize-none rounded border border-line bg-ink-800 px-3 py-2 text-sm text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
          />

          <button
            onClick={press}
            disabled={!stopping && !said}
            title={stopping ? "中断这一轮" : inFlight ? "排队，作为新的一轮" : "发送"}
            aria-label={stopping ? "中断" : "发送"}
            className={`grid h-9 w-9 shrink-0 place-items-center rounded border transition-colors ${
              stopping
                ? "border-dim text-fg hover:border-fg"
                : said
                  ? "border-accent/60 text-accent hover:bg-accent/10"
                  : "cursor-not-allowed border-line text-dim/40"
            }`}
          >
            {stopping ? <StopIcon /> : <SendIcon />}
          </button>
        </div>

        {inFlight && said && (
          <p className="mt-1.5 text-[11px] text-dim">
            <b className="font-medium text-fg">发送</b> 排队，等这一轮写完再作为新的一轮 ·{" "}
            <b className="font-medium text-fg">Ctrl+Enter</b> 插进正在跑的这一轮，当场改方向
          </p>
        )}
      </div>
    </div>
  );
}

function SendIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.6">
      <path d="M8 13V3M8 3L4 7M8 3l4 4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function StopIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="currentColor">
      <rect x="3" y="3" width="10" height="10" rx="1.5" />
    </svg>
  );
}
