import { useEffect, useRef } from "react";
import Markdown from "react-markdown";

import type { Node } from "../protocol";
import { useSession } from "../store";
import { Process } from "./Process";

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

        {path.map((node) => (
          <Turn
            key={node.id}
            said={node.user_input?.text}
            simulated={node.user_input?.source === "simulated"}
            events={traces[node.id ?? ""] ?? []}
            node={node}
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
  running,
  streamed,
  thinking,
}: {
  said?: string;
  simulated?: boolean;
  events: import("../protocol").TraceEvent[];
  node?: Node;
  running?: boolean;
  /** 流式攒出来的正文。**只在这一轮还没落地时用**——节点一到就让位（D160）。 */
  streamed?: string;
  /** 过程那半反过来：节点里没有它，落地之后照样留着（D166）。 */
  thinking?: import("../store").Thought[];
}): JSX.Element {
  return (
    <article className="mb-8">
      {said && (
        <div className="mb-3 flex items-baseline gap-2">
          <span className="select-none text-dim">›</span>
          <p className="min-w-0 flex-1 whitespace-pre-wrap text-sm text-dim">{said}</p>
          {/* **模拟输入要标出来**（D148）：那一条不是用户打的，是 goal_manager
              写的。不标的话用户会以为自己说过这句话。 */}
          {simulated && (
            <span className="shrink-0 rounded border border-line px-1 py-0.5 text-[10px] text-dim">
              目标推进
            </span>
          )}
        </div>
      )}

      <Process events={events} running={!!running} thinking={thinking} />

      {/* 还在写：把攒出来的字直接摆出来。**不做 Markdown**——半截语法
          （没闭合的 `**`、写了一半的列表）渲染出来会跳来跳去。 */}
      {!node && streamed && (
        <p className="whitespace-pre-wrap font-prose text-[15px] leading-[1.9] text-fg">
          {streamed}
          <span className="ml-0.5 inline-block h-4 w-[2px] animate-pulse bg-accent align-text-bottom" />
        </p>
      )}

      {node &&
        (node.prose ? (
          <div className="font-prose text-[15px] leading-[1.9] text-fg">
            <Markdown>{node.prose}</Markdown>
          </div>
        ) : (
          <p className="text-sm text-dim">（这一轮没有写出正文。）</p>
        ))}

      {node?.deviation_report && (
        <p className="mt-3 border-l-2 border-line pl-3 text-xs text-dim">
          偏离报告：{node.deviation_report}
        </p>
      )}
    </article>
  );
}
