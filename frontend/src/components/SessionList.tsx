import { useState } from "react";

import { api } from "../api";
import { type Page, useSession } from "../store";
import { EditableTitle } from "./EditableTitle";

/**
 * 左栏：**上面是独立页面的入口，下面是会话记录**（R17）。
 *
 * 不做成 tab 切换——tab 会让会话列表在看设定时消失，而那没道理：设定不是故事的
 * 互斥模式，它是故事的**材料**。入口在上、列表在下，两者同时在场。
 *
 * **会话的标题在这里改**（S80）：session 上只剩标题、目标、节点树，为一个字段
 * 单开一个「会话设置」面板不划算——它就在你能看见这个标题的地方改。
 */
export function SessionList(): JSX.Element {
  const sessions = useSession((s) => s.sessions);
  const current = useSession((s) => s.sessionId);
  const page = useSession((s) => s.page);
  const go = useSession((s) => s.go);
  const connect = useSession((s) => s.connect);
  const newSession = useSession((s) => s.newSession);
  const refreshSessions = useSession((s) => s.refreshSessions);
  const refreshState = useSession((s) => s.refreshState);

  function open(id: string): void {
    void connect(id);
    go({ kind: "story" });
  }

  /** 正在改名的那一行。**这里是受控的**：列表行的文字本身是"打开这局故事"，
   * 点它改名会把主要动作顶掉，所以由旁边的 ✎ 触发（见 `EditableTitle`）。 */
  const [renaming, setRenaming] = useState<string | null>(null);

  async function rename(id: string, title: string): Promise<void> {
    await api.patchSession(id, { meta: { title } });
    await refreshSessions();
    if (id === current) await refreshState();
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
        {/* **单次调用自己一个入口**（S147）：`summarize` / `rerank` 是一次动作，
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
              className={`group mb-0.5 flex items-start rounded transition-colors ${
                here ? "bg-ink-600" : "hover:bg-ink-700"
              }`}
            >
              <div
                onClick={() => renaming !== card.id && open(card.id)}
                className="min-w-0 flex-1 cursor-pointer px-2 py-1.5 text-left"
              >
                <EditableTitle
                  value={card.title}
                  onCommit={(title) => void rename(card.id, title)}
                  editing={renaming === card.id}
                  onEditingChange={(on) => setRenaming(on ? card.id : null)}
                  className={`block w-full truncate text-xs ${here ? "text-fg" : "text-dim"}`}
                />
                {/* 第二行是它开在哪份设定上——列表里唯一需要的第二位信息。 */}
                <span className="block truncate text-[10px] text-dim/70">{card.worldbook_name}</span>
              </div>
              <button
                onClick={() => setRenaming(card.id)}
                title="改名"
                className="shrink-0 px-1.5 py-1.5 text-[11px] text-dim opacity-0 transition-opacity hover:text-fg group-hover:opacity-100"
              >
                ✎
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
