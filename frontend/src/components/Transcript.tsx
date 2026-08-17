import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";

import { api } from "../api";
import type { Node } from "../protocol";
import { useSession } from "../store";
import { NodeEditor } from "./NodeEditor";
import { Process, splitAtProse } from "./Process";

/**
 * 正文流。**每一轮是「输入 → 过程 → 正文」三段**，过程摆在两者中间——
 * 它本来就发生在中间。
 *
 * **只显示正文，不显示细纲**：细纲是给 writer 看的中间产物，摆在正文旁边会把
 * 读者的注意力拽到"这一轮本来打算写什么"上去。要看细纲展开过程里那一条。
 */
export function Transcript(): JSX.Element {
  const path = useSession((s) => s.path);
  const traces = useSession((s) => s.traces);
  const live = useSession((s) => s.live);
  const streaming = useSession((s) => s.streaming);
  const phase = useSession((s) => s.phase);
  const bottom = useRef<HTMLDivElement>(null);

  const liveSteps = live ? (traces[live.runId] ?? []) : [];
  const flowing = live ? streaming[live.runId] : undefined;

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [path.length, liveSteps.length, flowing?.prose.length, phase]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {path.length === 0 && phase === "idle" && (
          <p className="text-sm text-dim">说点什么开始这个故事。</p>
        )}

        {path.map((node, at) => (
          <Turn
            key={node.id}
            said={node.user_input?.text}
            simulated={node.user_input?.source === "simulated"}
            events={traces[node.id ?? ""] ?? []}
            node={node}
            last={at === path.length - 1}
            // **落地之后思考段还留着**（D166）：它不在节点里，扔掉就再也看不到了。
            // goal_manager 更是在这一轮落地**之后**才跑的，它的思考只能挂在这儿。
            thinking={streaming[node.id ?? ""]?.process}
          />
        ))}

        {/* 正在跑的那一轮：还没有节点，但输入和过程都已经有了。 */}
        {live && (
          <Turn
            said={live.input}
            events={liveSteps}
            running
            streamed={flowing?.prose}
            thinking={flowing?.process}
          />
        )}

        <div ref={bottom} />
      </div>
    </div>
  );
}

function Turn({
  said,
  simulated,
  events,
  node,
  last,
  running,
  streamed,
  thinking,
}: {
  said?: string;
  simulated?: boolean;
  events: import("../protocol").TraceEvent[];
  node?: Node;
  /** 是不是路径末端。**只有它下面才给「在这里断段」**——手动归段收的是"到此
   * 为止还没归段的那几轮"，在中间某一轮点它，收的仍然是到末端为止的那一段。 */
  last?: boolean;
  running?: boolean;
  /** 流式攒出来的正文。**只在这一轮还没落地时用**——节点一到就让位（D160）。 */
  streamed?: string;
  /** 过程那半反过来：节点里没有它，落地之后照样留着（D166）。 */
  thinking?: import("../store").Thought[];
}): JSX.Element {
  const editing = useSession((s) => s.editing);
  const notes = useSession((s) => s.notes[node?.id ?? ""] ?? []);

  // **时间线不能在正文这里倒着走**（D167）：状态维护跑在 writer 之后（D143），
  // goal 判定更在 run 末（D28），它们的思考与工具调用因此归到正文**下面**那一块。
  const [beforeEvents, afterEvents] = splitAtProse(events);
  const [beforeThinking, afterThinking] = splitAtProse(thinking ?? []);
  const post = afterEvents.length > 0 || afterThinking.length > 0;

  return (
    // `group` 是为了那排操作：**悬停才出现**，读正文的时候它们全是噪音。
    <article className="group mb-8">
      {(said || node) && (
        <div className="mb-3 flex items-baseline gap-2">
          {said && <span className="select-none text-dim">›</span>}
          <p className="min-w-0 flex-1 whitespace-pre-wrap text-sm text-dim">{said}</p>
          {/* **模拟输入要标出来**（D148）：那一条不是用户打的，是 goal_manager
              写的。不标的话用户会以为自己说过这句话。 */}
          {simulated && (
            <span className="shrink-0 rounded border border-line px-1 py-0.5 text-[10px] text-dim">
              目标推进
            </span>
          )}
          {node && <TurnControls node={node} />}
        </div>
      )}

      {/* 上面这块在下面那块开始有动静的那一刻就不再"跑着"了——**进度指示应该
          跟着当前阶段走**，否则两块会同时闪，看不出在哪一段。 */}
      <Process events={beforeEvents} running={!!running && !post} thinking={beforeThinking} />

      {/* 还在写：把攒出来的字直接摆出来。**不做 Markdown**——半截语法
          （没闭合的 `**`、写了一半的列表）渲染出来会跳来跳去。 */}
      {!node && streamed && (
        <p className="prose-body whitespace-pre-wrap">
          {streamed}
          <span className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-accent align-text-bottom" />
        </p>
      )}

      {node &&
        (node.prose ? (
          <div className="prose-body">
            <Markdown>{node.prose}</Markdown>
          </div>
        ) : (
          <p className="text-sm text-dim">（这一轮没有写出正文。）</p>
        ))}

      {/* 偏离报告是 writer 交出来的，跟着正文走，排在"写完之后"前面。 */}
      {node?.deviation_report && (
        <p className="mt-3 border-l-2 border-line pl-3 text-xs text-dim">
          偏离报告：{node.deviation_report}
        </p>
      )}

      {/* **写完之后**：状态维护（D143）与目标判定（D28）。goal_manager 尤其晚——
          它跑在这一轮落地之后，那时 `running` 已经是 false 了，所以这块是收着的。 */}
      {post && (
        <Process
          events={afterEvents}
          running={!!running}
          thinking={afterThinking}
          label="写完之后"
          tail
        />
      )}

      {/* **采样标记**（D43/D44）：服务端每轮都在算，而它从不影响运行时行为。
          放在这一轮的末尾、收着摆——它是"这一轮有什么值得看一眼"，不是警告。 */}
      {notes.length > 0 && (
        <ul className="mt-3 space-y-0.5">
          {notes.map((note, i) => (
            <li key={i} className="flex gap-1.5 text-[11px] text-dim/70">
              <span className="select-none">·</span>
              <span className="min-w-0 flex-1">{note}</span>
            </li>
          ))}
        </ul>
      )}

      {node && editing === node.id && <NodeEditor node={node} />}

      {/* 已归段的分界画出来，没归段的给一个「在这里断段」（D112 的第三条支路）。 */}
      {node?.closes_segment && <SegmentEnd span={node.segment_span ?? 1} />}
      {node && last && !node.closes_segment && <CloseSegment />}
    </article>
  );
}

/** 一段剧情收在这里。**它是索引的单位**——摘要存在段落的最后一个节点上（D110）。 */
function SegmentEnd({ span }: { span: number }): JSX.Element {
  return (
    <div className="my-6 flex items-center gap-3 text-[11px] text-dim">
      <span className="h-px flex-1 bg-line" />
      <span className="shrink-0">一段结束（{span} 轮）</span>
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}

/**
 * 手动归段（D112 的第三条支路）。**用户说这一段收了，就是收了**——不再追问模型。
 *
 * 自动追问判早了或判晚了，用户随时可以自己断；归段追问关掉时它更是唯一的路。
 * 后端一直有这个接口，而界面此前连方法都没有。
 */
function CloseSegment(): JSX.Element {
  const sessionId = useSession((s) => s.sessionId);
  const refreshState = useSession((s) => s.refreshState);
  const [busy, setBusy] = useState(false);

  async function close(): Promise<void> {
    if (!sessionId) return;
    setBusy(true);
    try {
      await api.closeSegment(sessionId);
      await refreshState();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 flex justify-center opacity-0 transition-opacity group-hover:opacity-100">
      <button className="btn" onClick={() => void close()} disabled={busy} title="到这里为止收成一段">
        ⌄ 在这里断段
      </button>
    </div>
  );
}

/**
 * 一轮的操作：`⟨2/3⟩` 切支、`↻` 重生成、`✎` 编辑。**悬停才出现**（D172）。
 *
 * 这三样是「回头」这个方向的全部入口。此前后端全齐、界面一个都没有——而
 * `⟨2/3⟩` 尤其重要：**它是回到历史分叉点的唯一路径**（D147），没有它，
 * 重生成产生的兄弟节点永远回不去。
 */
function TurnControls({ node }: { node: Node }): JSX.Element {
  const siblings = useSession((s) => s.siblings[node.id ?? ""] ?? []);
  const path = useSession((s) => s.path);
  const phase = useSession((s) => s.phase);
  const switchBranch = useSession((s) => s.switchBranch);
  const regenerate = useSession((s) => s.regenerate);
  const edit = useSession((s) => s.edit);
  const editing = useSession((s) => s.editing);

  const at = siblings.indexOf(node.id ?? "");
  const behind = path.length - 1 - path.findIndex((n) => n.id === node.id);

  function reroll(): void {
    // **不是当前叶时，后面那几轮会移出当前路径。** 它们还在（切回来就是了），
    // 但这件事必须先说——用户按的是"重写这一轮"，看到的却是后面几轮消失了。
    if (behind > 0 && !confirm(`重新生成这一轮？后面 ${behind} 轮会移出当前路径（还能切回来）。`)) {
      return;
    }
    regenerate(node.id ?? "");
  }

  return (
    <div className="flex shrink-0 items-center gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
      {siblings.length > 1 && (
        <span className="flex items-center gap-0.5 font-mono text-[11px] text-dim">
          <Tap
            label="‹"
            title="上一支"
            disabled={at <= 0}
            onClick={() => switchBranch(siblings[at - 1] ?? "")}
          />
          {at + 1}/{siblings.length}
          <Tap
            label="›"
            title="下一支"
            disabled={at >= siblings.length - 1}
            onClick={() => switchBranch(siblings[at + 1] ?? "")}
          />
        </span>
      )}
      <Tap label="↻" title="重新生成这一轮" disabled={phase !== "idle"} onClick={reroll} />
      <Tap
        label="✎"
        title="改细纲 / 正文 / 状态"
        onClick={() => edit(editing === node.id ? null : (node.id ?? null))}
      />
    </div>
  );
}

function Tap({
  label,
  title,
  onClick,
  disabled,
}: {
  label: string;
  title: string;
  onClick: () => void;
  disabled?: boolean;
}): JSX.Element {
  return (
    <button
      title={title}
      onClick={onClick}
      disabled={disabled}
      className="rounded px-1 py-0.5 font-mono text-[11px] text-dim transition-colors hover:bg-ink-700 hover:text-fg disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-dim"
    >
      {label}
    </button>
  );
}
