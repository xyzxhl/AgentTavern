import type { TraceEvent } from "../protocol";
import { useSession } from "../store";

/**
 * agent 在做什么。**UI 是 trace 的投影**（D25）——筛选与渲染规则在这一侧，
 * 不反过来污染 trace schema。
 *
 * 每条事件自带人类可读的 `label`（"确认了她现在在哪"，而不是
 * `state_query(location)`），所以这里主要就是把 label 摆出来。
 */
export function TracePanel(): JSX.Element {
  const trace = useSession((s) => s.trace);

  if (trace.length === 0) {
    return <p className="px-3 py-3 text-xs text-dim">这一轮还没开始。</p>;
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
      {trace.map((event, i) => (
        <Row key={`${event.run_id}-${event.seq}-${i}`} event={event} />
      ))}
    </div>
  );
}

const ACTOR_LABEL: Record<string, string> = {
  director: "推演",
  writer: "成文",
  state_manager: "状态",
  goal_manager: "目标",
  orchestrator: "编排",
  user: "用户",
};

function Row({ event }: { event: TraceEvent }): JSX.Element {
  // 完整输入输出那一类由开发者面板管，这里不展开——它一条就有几万字。
  const dim = event.type === "model_request";
  return (
    <div className="mb-1.5 flex gap-2 text-xs">
      <span className="w-8 shrink-0 pt-px text-right font-mono text-[10px] text-dim">
        {ACTOR_LABEL[event.actor] ?? event.actor}
      </span>
      <span className={`min-w-0 flex-1 break-words ${dim ? "text-dim/60" : "text-fg"}`}>
        {event.label}
      </span>
    </div>
  );
}
