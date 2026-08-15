import { useState } from "react";

import type { TraceEvent } from "../protocol";
import type { Thought } from "../store";

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

/** **writer 之后才跑的 agent**：状态维护后置（D143），目标判定在 run 末（D28/D148）。 */
const AFTER_WRITER = new Set(["state_manager", "goal_manager"]);

/**
 * 按**运行顺序**把一轮切成两半：正文之前发生的，和正文之后发生的。
 *
 * 切点**按位置定，不按 actor 定**——`writer` 最后那条记录之后就是下半。这一点
 * 在 goal 开场时才见分晓（D168）：那时 goal_manager 跑在 director **之前**，
 * 而它照 actor 认的话会被扔到正文下面去，时间线正好反了。位置不会说谎。
 *
 * 没有 writer 记录时退回按 actor 认——两种情形都走这条：从 state 阶段续跑的
 * 那一轮（这一轮压根没有 writer），以及思考段那份（writer 未必有思考段）。
 *
 * 之后的编排层事件（`state_committed`、`run_finished`）跟着落到下半，
 * 它们本来就发生在那之后。trace 事件和思考段都按时间排着，一个函数切两样。
 */
export function splitAtProse<T extends { actor: string }>(items: T[]): [T[], T[]] {
  let cut = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i]?.actor === "writer") {
      cut = i + 1;
      break;
    }
  }
  if (cut < 0) {
    const at = items.findIndex((x) => AFTER_WRITER.has(x.actor));
    cut = at < 0 ? items.length : at;
  }
  return [items.slice(0, cut), items.slice(cut)];
}

/**
 * 一轮的过程：**摆在正文流里，不在侧栏**。
 *
 * 顺序就是它真实发生的顺序——用户输入 → 思考 → 工具调用 → 思考 → … → 最终输出。
 * 把它放到右边等于要求读者一边读正文一边扭头，而这两样东西本来就是一件事的
 * 两个阶段。
 *
 * **同理，正文之后跑的东西要摆在正文下面**（D167）：状态维护后置到 writer 之后
 * （D143），goal 判定更在 run 末（D28）——把它们塞进正文上方那一块，等于让
 * 时间线在正文这里倒着走一段。所以 `Turn` 渲染两块，中间夹着正文。
 *
 * **正文一落地就自动收起**：过程是"这一轮为什么写成这样"的答案，只在你问的
 * 时候才需要；跑的时候你盯着它看进度，跑完了它就该让位给正文。
 */
export function Process({
  events,
  running,
  thinking,
  label = "过程",
  tail,
}: {
  events: TraceEvent[];
  running: boolean;
  /** 流式攒出来的思考段，**按 agent 分段**（D160/D166）。
   *
   * 它是过程，不是产出，所以摆在这里；分段是因为一轮里最多四个 agent 说过话，
   * 拼成一坨就分不出哪句是谁的了。 */
  thinking?: Thought[];
  /** 收起时那一行怎么称呼这一块。默认「过程」；正文下面那块叫「写完之后」——
   * **说清它是什么时候发生的**，那正是分成两块要传达的东西。 */
  label?: string;
  /** 摆在正文下方（留上边距而不是下边距）。 */
  tail?: boolean;
}): JSX.Element | null {
  // 跑着的时候摊开，跑完自动收起——但用户手动开过就听用户的。
  const [opened, setOpened] = useState<boolean | null>(null);
  const open = opened ?? running;

  if (events.length === 0 && !thinking?.length) return null;

  const steps = events.filter((e) => !PLUMBING.has(e.type));
  const latest = steps.at(-1) ?? events.at(-1);
  const gap = tail ? "mt-4" : "mb-3";

  if (!open) {
    return (
      <button
        onClick={() => setOpened(true)}
        className={`${gap} flex items-center gap-1.5 text-xs text-dim transition-colors hover:text-fg`}
      >
        <span className="select-none">▸</span>
        <span>
          {label} {steps.length} 步
        </span>
      </button>
    );
  }

  return (
    <div className={gap}>
      <button
        onClick={() => setOpened(false)}
        className="mb-1.5 flex items-center gap-1.5 text-xs text-dim transition-colors hover:text-fg"
      >
        <span className="select-none">▾</span>
        <span>{running ? (latest?.label ?? "正在开始") : `${label} ${steps.length} 步`}</span>
        {running && <span className="inline-block h-1 w-1 animate-pulse rounded-full bg-accent" />}
      </button>

      <ol className="border-l border-line pl-3">
        {events.map((event, i) => (
          <Step key={`${event.run_id}-${event.seq}-${i}`} event={event} />
        ))}
      </ol>

      {thinking && thinking.length > 0 && (
        <div className="mt-1.5 space-y-1 border-l border-line pl-3">
          {thinking.map((one, i) => (
            <Thinking key={`${one.actor}-${i}`} thought={one} running={running} />
          ))}
        </div>
      )}
    </div>
  );
}

const FOLD_AT = 300;

/**
 * 某个 agent 的一段思考。**长了就自己折起来**（同 D163 的理由）。
 *
 * 跑着的时候默认摊开——那时它就是进度条，你盯着它看到哪儿了；跑完之后默认
 * 折起，因为那时它只在"为什么写成这样"被问起时才需要，而 director 一轮的
 * 思考段实测有两万多字，摊着会把整条正文流顶到屏幕外面去。
 */
function Thinking({ thought, running }: { thought: Thought; running: boolean }): JSX.Element {
  const [opened, setOpened] = useState<boolean | null>(null);
  const long = thought.text.length > FOLD_AT;
  const open = opened ?? (running || !long);
  const who = ACTOR_LABEL[thought.actor] ?? thought.actor;

  return (
    <div>
      {long && (
        <button
          onClick={() => setOpened(!open)}
          className="flex items-baseline gap-1.5 text-[10px] text-dim/70 transition-colors hover:text-fg"
        >
          <span className="select-none">{open ? "▾" : "▸"}</span>
          <span>
            {who}的思考 {thought.text.length} 字
          </span>
        </button>
      )}
      {open && (
        <p className="whitespace-pre-wrap text-xs leading-relaxed text-dim/70">
          {!long && <span className="mr-1.5 font-mono text-[10px] text-dim/50">{who}</span>}
          {thought.text}
        </p>
      )}
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
