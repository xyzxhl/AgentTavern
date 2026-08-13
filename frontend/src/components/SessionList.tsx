import { useSession } from "../store";

export function SessionList(): JSX.Element {
  const ids = useSession((s) => s.sessionIds);
  const current = useSession((s) => s.sessionId);
  const connect = useSession((s) => s.connect);
  const newSession = useSession((s) => s.newSession);

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-ink-800">
      <div className="flex items-center justify-between pr-2">
        <span className="panel-title">会话</span>
        <button className="btn" onClick={() => void newSession()}>
          新建
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {ids.length === 0 && <p className="px-1 py-2 text-xs text-dim">还没有会话</p>}
        {ids.map((id) => (
          <button
            key={id}
            onClick={() => void connect(id)}
            className={`block w-full truncate rounded px-2 py-1.5 text-left font-mono text-xs transition-colors ${
              id === current ? "bg-ink-600 text-fg" : "text-dim hover:bg-ink-700 hover:text-fg"
            }`}
          >
            {id}
          </button>
        ))}
      </div>
    </aside>
  );
}
