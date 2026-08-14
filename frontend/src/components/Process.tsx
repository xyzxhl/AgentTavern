import { useState } from "react";

import type { TraceEvent } from "../protocol";

const ACTOR_LABEL: Record<string, string> = {
  director: "推演",
  writer: "成文",
  state_manager: "状态",
  goal_manager: "目标",
  orchestrator: "编排",
  user: "用户",
};

/** 这些是编排层的脚手架，不是"它在想什么"。默认压暗，收起时也不计入步数。 */
const PLUMBING = new Set(["context_assembled", "run_started", "run_finished", "model_request"]);

/**
 * 一轮的过程：**摆在正文流里，不在侧栏**。
 *
 * 顺序就是它真实发生的顺序——用户输入 → 思考 → 工具调用 → 思考 → … → 最终输出。
 * 把它放到右边等于要求读者一边读正文一边扭头，而这两样东西本来就是一件事的
 * 两个阶段。
 *
 * **正文一落地就自动收起**（`collapsed`）：过程是"这一轮为什么写成这样"的答案，
 * 只在你问的时候才需要；跑的时候你盯着它看进度，跑完了它就该让位给正文。
 */
export function Process({
  events,
  running,
}: {
  events: TraceEvent[];
  running: boolean;
}): JSX.Element | null {
  // 跑着的时候摊开，跑完自动收起——但用户手动开过就听用户的。
  const [opened, setOpened] = useState<boolean | null>(null);
  const open = opened ?? running;

  if (events.length === 0) return null;

  const steps = events.filter((e) => !PLUMBING.has(e.type));
  const latest = steps.at(-1) ?? events.at(-1);

  if (!open) {
    return (
      <button
        onClick={() => setOpened(true)}
        className="mb-3 flex items-center gap-1.5 text-xs text-dim transition-colors hover:text-fg"
      >
        <span className="select-none">▸</span>
        <span>过程 {steps.length} 步</span>
      </button>
    );
  }

  return (
    <div className="mb-3">
      <button
        onClick={() => setOpened(false)}
        className="mb-1.5 flex items-center gap-1.5 text-xs text-dim transition-colors hover:text-fg"
      >
        <span className="select-none">▾</span>
        <span>{running ? (latest?.label ?? "正在开始") : `过程 ${steps.length} 步`}</span>
        {running && <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-accent" />}
      </button>

      <ol className="border-l border-line pl-3">
        {events.map((event, i) => (
          <Step key={`${event.run_id}-${event.seq}-${i}`} event={event} />
        ))}
      </ol>
    </div>
  );
}

function Step({ event }: { event: TraceEvent }): JSX.Element {
  const plumbing = PLUMBING.has(event.type);
  return (
    <li className="flex gap-2 py-0.5 text-xs">
      <span className="w-7 shrink-0 pt-px text-right font-mono text-[10px] text-dim">
        {ACTOR_LABEL[event.actor] ?? event.actor}
      </span>
      <span className={`min-w-0 flex-1 break-words ${plumbing ? "text-dim/50" : "text-dim"}`}>
        {event.label}
      </span>
    </li>
  );
}
