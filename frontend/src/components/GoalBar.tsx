import { useSession } from "../store";

/**
 * 跑着的目标，摆在输入框正上方（S128）。
 *
 * **它会自己开下一轮**——这件事必须一直看得见。只放侧栏标签后面的话，用户切到
 * 状态页就再也不知道那边还在动，而下一轮什么时候开、还剩几轮预算，恰恰是他
 * 决定「要不要现在插一句」时唯一需要的信息。
 *
 * 细节归侧栏那一页；这里只回答三件事：在推什么、推到哪儿、怎么叫停。
 */
export function GoalBar(): JSX.Element | null {
  const goal = useSession((s) => s.session?.meta?.goal);
  const run = useSession((s) => s.session?.goal_run);
  const progress = useSession((s) => s.goal);
  const send = useSession((s) => s.send);

  if (!goal) return null;

  const subgoals = progress?.subgoals ?? run?.plan?.subgoals ?? [];
  const done = subgoals.filter((s) => s.done).length;
  const spent = progress?.spent_runs ?? run?.spent_runs ?? 0;
  // **`null` 是"不设上限"，不是 0**（S97）：`??` 会把它悄悄换成另一条的值。
  const cap = (progress ? progress.max_runs : goal.max_runs) ?? null;
  const paused = run?.paused ?? false;

  return (
    <div className="flex items-center gap-3 border-t border-line bg-ink-800 px-6 py-1.5 text-xs">
      <span className={`shrink-0 ${paused ? "text-dim" : "text-accent"}`}>
        {paused ? "目标已暂停" : "目标推进中"}
      </span>

      <span className="min-w-0 flex-1 truncate text-dim">{goal.description}</span>

      {subgoals.length > 0 && (
        <span className="shrink-0 font-mono text-[11px] text-dim">
          {done}/{subgoals.length}
        </span>
      )}
      <span className="shrink-0 font-mono text-[11px] text-dim">
        {cap === null ? `第 ${spent + 1} 轮 · 不限` : `${spent}/${cap} 轮`}
      </span>

      <button
        className="btn shrink-0"
        onClick={() => send({ type: "goal_control", action: paused ? "resume" : "pause" })}
      >
        {paused ? "继续" : "暂停"}
      </button>
    </div>
  );
}
