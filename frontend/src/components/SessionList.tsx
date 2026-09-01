import { type Page, useSession } from "../store";

/**
 * 左栏：**上面是独立页面的入口，下面是会话记录**（R17）。
 *
 * 不做成 tab 切换——tab 会让会话列表在看设定时消失，而那没道理：设定不是故事的
 * 互斥模式，它是故事的**材料**。入口在上、列表在下，两者同时在场。
 *
 * **这一行只负责打开**：改名在故事页顶栏点标题（S155）——一件事一种交互形态。
 */
export function SessionList(): JSX.Element {
  const sessions = useSession((s) => s.sessions);
  const current = useSession((s) => s.sessionId);
  const page = useSession((s) => s.page);
  const go = useSession((s) => s.go);
  const connect = useSession((s) => s.connect);
  const newSession = useSession((s) => s.newSession);

  function open(id: string): void {
    void connect(id);
    go({ kind: "story" });
  }

  return (
    <aside className="flex w-56 shrink-0 flex-col border-r border-line bg-ink-800">
      <nav className="border-b border-line p-2">
        {/* **提示词管理一直在**（S134）。它曾经跟开发者模式同一个开关，理由是
            「提示词是部署方写的东西、改坏了整台服务不工作」——但**这台服务的
            部署方就是用户自己**，而调提示词是这个项目里最日常的动作。
            开发者模式管的是另一件事：**能不能看到每次模型往返的完整输入输出**。 */}
        <Entry
          label="agent 提示词"
          active={page.kind === "agents"}
          page={{ kind: "agents" }}
          go={go}
        />
        {/* **单次调用自己一个入口**（S147）：`summarize` 是一次动作，
            不是一个角色。挂在 agent 那一页的小标题底下，读起来就是「agent，
            以及一些附属的东西」——而那个误解会影响判断，比如以为该给它们工具面。 */}
        <Entry
          label="单次调用"
          active={page.kind === "calls"}
          page={{ kind: "calls" }}
          go={go}
        />
        <Entry
          label="世界书管理"
          active={page.kind === "worldbooks"}
          page={{ kind: "worldbooks" }}
          go={go}
        />
        {/* **资料库和设定库并列**（S136）：都是一级资产，都能给好几局故事用。
            但它们回答的是不同的问题——设定是"这个世界是什么"（可能每轮在场），
            资料是"别人写的参考"（只在被查到时进上下文）。 */}
        <Entry
          label="资料库"
          active={page.kind === "corpora"}
          page={{ kind: "corpora" }}
          go={go}
        />
        <Entry
          label="全局设置"
          active={page.kind === "settings"}
          page={{ kind: "settings" }}
          go={go}
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
            <div
              key={card.id}
              className={`mb-0.5 flex items-start rounded transition-colors ${
                here ? "bg-ink-600" : "hover:bg-ink-700"
              }`}
            >
              {/* **这一行只负责打开**：改名在故事页顶栏点标题（S155）。
                  这里以前还有一个 ✎，于是「怎么改名」在界面上有两种答案——
                  而那正是 S134/S155 那条判据要消掉的东西。 */}
              <button onClick={() => open(card.id)} className="min-w-0 flex-1 px-2 py-1.5 text-left">
                <span className={`block truncate text-xs ${here ? "text-fg" : "text-dim"}`}>
                  {card.title || "未命名"}
                </span>
                {/* 第二行是它开在哪份设定上——列表里唯一需要的第二位信息。 */}
                <span className="block truncate text-[10px] text-dim/70">{card.worldbook_name}</span>
              </button>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

function Entry({
  label,
  active,
  page,
  go,
}: {
  label: string;
  active: boolean;
  page: Page;
  go: (next: Page) => void;
}): JSX.Element {
  return (
    <button
      onClick={() => go(page)}
      className={`block w-full rounded px-2 py-1.5 text-left text-xs transition-colors ${
        active ? "bg-ink-600 text-fg" : "text-dim hover:bg-ink-700 hover:text-fg"
      }`}
    >
      {label}
    </button>
  );
}
