import { useSession } from "../store";

/**
 * 世界状态。**它是派生数据**——权威是各节点的增量（S42），这里只是当前路径
 * 重放出来的结果。切分支时它会整份换掉。
 *
 * 所以这里的「改」不是改这块面板，是**改最新一轮的增量**（R22）——也就是正文流
 * 里最后一轮的 `✎`。**同一个操作，两个入口，不是两个概念**：日常说的"改现在的
 * 状态"就是它，历史某一轮走它自己的 `✎`。
 */
export function StatePanel(): JSX.Element {
  const state = useSession((s) => s.state);
  const latest = useSession((s) => s.path.at(-1)?.id ?? null);
  const edit = useSession((s) => s.edit);
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

      {latest && (
        <button
          className="btn mt-1"
          onClick={() => edit(latest)}
          title="状态是重放出来的——改它就是改最新一轮的增量"
        >
          改状态
        </button>
      )}
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
