import { useEffect, useRef } from "react";
import Markdown from "react-markdown";

import type { Node } from "../protocol";
import { useSession } from "../store";

/**
 * 正文流。**只显示正文，不显示细纲**——细纲是给 writer 看的中间产物，
 * 摆在正文旁边会把读者的注意力拽到"这一轮本来打算写什么"上去。
 * 要看细纲去侧栏的 trace。
 */
export function Transcript(): JSX.Element {
  const path = useSession((s) => s.path);
  const phase = useSession((s) => s.phase);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [path.length, phase]);

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-8">
        {path.length === 0 && phase === "idle" && (
          <p className="text-sm text-dim">说点什么开始这个故事。</p>
        )}

        {path.map((node) => (
          <Turn key={node.id} node={node} />
        ))}

        {phase === "running" && <Working />}
        <div ref={bottom} />
      </div>
    </div>
  );
}

function Turn({ node }: { node: Node }): JSX.Element {
  const said = node.user_input?.text;
  // **模拟输入要标出来**（D148）：那一条不是用户打的，是 goal_manager 写的。
  // 不标的话用户会以为自己说过这句话。
  const simulated = node.user_input?.source === "simulated";

  return (
    <article className="mb-8">
      {said && (
        <div className="mb-3 flex items-baseline gap-2">
          <span className="select-none text-dim">›</span>
          <p className="min-w-0 flex-1 whitespace-pre-wrap text-sm text-dim">{said}</p>
          {simulated && (
            <span className="shrink-0 rounded border border-line px-1 py-0.5 text-[10px] text-dim">
              目标推进
            </span>
          )}
        </div>
      )}

      {node.prose ? (
        <div className="prose-body font-prose text-[15px] leading-[1.9] text-fg">
          <Markdown>{node.prose}</Markdown>
        </div>
      ) : (
        <p className="text-sm text-dim">（这一轮没有写出正文。）</p>
      )}

      {node.deviation_report && (
        <p className="mt-3 border-l-2 border-line pl-3 text-xs text-dim">
          偏离报告：{node.deviation_report}
        </p>
      )}
    </article>
  );
}

function Working(): JSX.Element {
  const trace = useSession((s) => s.trace);
  const latest = trace.at(-1);
  return (
    <p className="flex items-center gap-2 text-sm text-dim">
      <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />
      {latest?.label ?? "正在开始"}
    </p>
  );
}
