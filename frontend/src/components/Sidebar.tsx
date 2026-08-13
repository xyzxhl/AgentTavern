import { useState } from "react";

import { StatePanel } from "./StatePanel";
import { TracePanel } from "./TracePanel";

type Tab = "state" | "trace";

/** 侧栏放**过程**：世界状态与 agent 在做什么。正文那侧只放要读的东西。 */
export function Sidebar(): JSX.Element {
  const [tab, setTab] = useState<Tab>("trace");

  return (
    <aside className="flex w-80 shrink-0 flex-col border-l border-line bg-ink-800">
      <div className="flex border-b border-line">
        {(["trace", "state"] as const).map((key) => (
          <button
            key={key}
            onClick={() => setTab(key)}
            className={`flex-1 px-3 py-2 text-[11px] uppercase tracking-wider transition-colors ${
              tab === key ? "border-b border-accent text-fg" : "text-dim hover:text-fg"
            }`}
          >
            {key === "trace" ? "过程" : "状态"}
          </button>
        ))}
      </div>
      {tab === "trace" ? <TracePanel /> : <StatePanel />}
    </aside>
  );
}
