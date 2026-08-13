import { useSession } from "../store";

/**
 * 世界状态。**它是派生数据**——权威是各节点的增量（D48），这里只是当前路径
 * 重放出来的结果。切分支时它会整份换掉。
 */
export function StatePanel(): JSX.Element {
  const state = useSession((s) => s.state);
  const keys = Object.keys(state).sort();

  if (keys.length === 0) {
    return <p className="px-3 py-3 text-xs text-dim">还没有状态。</p>;
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
      {keys.map((key) => (
        <div key={key} className="mb-3">
          <div className="font-mono text-[11px] text-accent">{key}</div>
          <Value value={state[key]} />
        </div>
      ))}
    </div>
  );
}

function Value({ value }: { value: unknown }): JSX.Element {
  if (Array.isArray(value)) {
    return (
      <ul className="mt-0.5 space-y-0.5">
        {value.map((item, i) => (
          <li key={i} className="flex gap-1.5 text-xs text-fg">
            <span className="text-dim">·</span>
            <span className="min-w-0 flex-1 break-words">{String(item)}</span>
          </li>
        ))}
      </ul>
    );
  }
  if (value !== null && typeof value === "object") {
    return (
      <pre className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px] text-fg">
        {JSON.stringify(value, null, 2)}
      </pre>
    );
  }
  return <p className="mt-0.5 break-words text-xs text-fg">{String(value)}</p>;
}
