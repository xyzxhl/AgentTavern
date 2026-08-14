import { useState } from "react";

import { useSession } from "../store";

/**
 * 输入框。
 *
 * **发送与停止是同一个按钮**，按「有没有话要说」变形：跑着的时候没输入，
 * 你唯一想做的事就是叫停；有输入就还是发送。两个常驻按钮里总有一个是灰的，
 * 而灰按钮只是占位。
 *
 * 跑着的时候发送**默认排队**（D26：每条输入独立成一个 run，不打包）。
 * 想插进正在跑的这一轮走「引导」（D119）——两者结果差很远，所以并排给出口，
 * 不做成一个需要猜的默认值。
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
  // 有话要说就是发送，没话可说时那一下的意思只能是「别再往下跑了」。
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

  function primary(): void {
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
              else primary();
            }}
            rows={2}
            placeholder={
              inFlight ? "排队等下一轮，或 Ctrl+Enter 插进正在跑的这一轮" : "推进剧情，或用（括号）写元指令"
            }
            className="max-h-48 min-h-[3rem] flex-1 resize-none rounded border border-line bg-ink-800 px-3 py-2 text-sm text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
          />

          <div className="flex flex-col items-stretch gap-1.5">
            <button
              className={`btn ${stopping ? "border-dim text-fg" : ""}`}
              onClick={primary}
              disabled={!stopping && !said}
              title={stopping ? "停止这一轮" : inFlight ? "排队，作为新的一轮" : "发送"}
            >
              {stopping ? "停止" : inFlight ? "排队" : "发送"}
            </button>

            {/* 只在真有得插的时候出现——没有 run 在跑时它和「发送」是同一件事。 */}
            {inFlight && (
              <button className="btn" onClick={guide} disabled={!said} title="Ctrl+Enter">
                引导
              </button>
            )}
          </div>
        </div>

        {inFlight && said && (
          <p className="mt-1.5 text-[11px] text-dim">
            <b className="font-medium text-fg">排队</b> 等这一轮写完，作为新的一轮 ·{" "}
            <b className="font-medium text-fg">引导</b> 插进正在跑的这一轮，当场改方向
          </p>
        )}
      </div>
    </div>
  );
}
