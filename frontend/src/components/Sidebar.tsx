import { useState } from "react";

import { useSession } from "../store";
import { GoalPanel } from "./GoalPanel";
import { StatePanel } from "./StatePanel";

type Tab = "state" | "goal";

/**
 * 侧栏放**跨轮的东西**：世界状态与长任务。
 *
 * 过程搬进正文流了（它是每一轮自己的一部分）；这两样不属于任何一轮——状态是
 * 当前快照，goal 横跨很多轮，所以留在这里。
 */
export function Sidebar(): JSX.Element {
  const [tab, setTab] = useState<Tab>("state");
  const running = useSession((s) => Boolean(s.session?.meta?.goal));
  const progress = useSession((s) => s.goal);

  const left = progress ? (progress.subgoals ?? []).filter((sub) => !sub.done).length : null;

  return (
    <aside className="flex w-72 shrink-0 flex-col border-l border-line bg-ink-800">
      <div className="flex border-b border-line">
        <Tabs tab={tab} setTab={setTab} running={running} left={left} />
      </div>
      {tab === "state" ? <StatePanel /> : <GoalPanel />}
    </aside>
  );
}

function Tabs({
  tab,
  setTab,
  running,
  left,
}: {
  tab: Tab;
  setTab: (next: Tab) => void;
  running: boolean;
  left: number | null;
}): JSX.Element {
  return (
    <>
      {(["state", "goal"] as const).map((key) => (
        <button
          key={key}
          onClick={() => setTab(key)}
          className={`flex flex-1 items-center justify-center gap-1.5 px-3 py-2 text-[11px] uppercase tracking-wider transition-colors ${
            tab === key ? "border-b border-accent text-fg" : "text-dim hover:text-fg"
          }`}
        >
          {key === "state" ? "状态" : "目标"}
          {/* goal 跑着的时候在标签上留个记号——它会自己开下一轮，
              用户切到状态页去看时也该知道那边还在动。 */}
          {key === "goal" && running && (
            <span className="rounded bg-accent/20 px-1 text-[10px] text-accent">
              {left === null ? "•" : left}
            </span>
          )}
        </button>
      ))}
    </>
  );
}
