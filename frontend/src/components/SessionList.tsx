import { useSession } from "../store";

/**
 * 左栏：**上面是独立页面的入口，下面是会话记录**（D172）。
 *
 * 不做成 tab 切换——tab 会让会话列表在看设定时消失，而那没道理：设定不是故事的
 * 互斥模式，它是故事的**材料**。入口在上、列表在下，两者同时在场。
 */
export function SessionList(): JSX.Element {
  const sessions = useSession((s) => s.sessions);
  const current = useSession((s) => s.sessionId);
  const page = useSession((s) => s.page);
  const go = useSession((s) => s.go);
  const connect = useSession((s) => s.connect);
  const newSession = useSession((s) => s.newSession);
  const developer = useSession((s) => s.meta?.developer ?? false);

  function open(id: string): void {
    void connect(id);
    go({ kind: "story" });
  }

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-ink-800">
      <nav className="border-b border-line p-2">
        {/* **提示词是部署方写的东西**（D35），且改坏了整台服务不工作——所以跟
            开发者模式同一个开关（D156）。放开只是去掉这个条件。 */}
        {developer && (
          <Entry label="提示词管理" active={page.kind === "prompts"} onClick={() => go({ kind: "prompts" })} />
        )}
        <Entry
          label="世界书管理"
          active={page.kind === "worldbooks"}
          onClick={() => go({ kind: "worldbooks" })}
        />
      </nav>

      <div className="flex items-center justify-between pr-2">
        <span className="panel-title">会话</span>
        <button className="btn" onClick={() => void newSession()}>
          新建
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {sessions.length === 0 && <p className="px-1 py-2 text-xs text-dim">还没有会话</p>}
        {sessions.map((card) => {
          const here = card.id === current && page.kind === "story";
          return (
            <button
              key={card.id}
              onClick={() => open(card.id)}
              className={`mb-0.5 block w-full rounded px-2 py-1.5 text-left transition-colors ${
                here ? "bg-ink-600" : "hover:bg-ink-700"
              }`}
            >
              <span className={`block truncate text-xs ${here ? "text-fg" : "text-dim"}`}>
                {card.title || "未命名"}
              </span>
              {/* 第二行是它开在哪份设定上——列表里唯一需要的第二位信息。 */}
              <span className="block truncate text-[10px] text-dim/70">{card.worldbook_name}</span>
            </button>
          );
        })}
      </div>
    </aside>
  );
}

function Entry({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      onClick={onClick}
      className={`block w-full rounded px-2 py-1.5 text-left text-xs transition-colors ${
        active ? "bg-ink-600 text-fg" : "text-dim hover:bg-ink-700 hover:text-fg"
      }`}
    >
      {label}
    </button>
  );
}
