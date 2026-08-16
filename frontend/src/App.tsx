import { useEffect, useState } from "react";

import { Composer } from "./components/Composer";
import { DeveloperPanel } from "./components/DeveloperPanel";
import { GoalBar } from "./components/GoalBar";
import { Notice } from "./components/Notice";
import { QuestionPrompt } from "./components/QuestionPrompt";
import { SessionList } from "./components/SessionList";
import { SettingsPanel } from "./components/SettingsPanel";
import { Sidebar } from "./components/Sidebar";
import { Transcript } from "./components/Transcript";
import { WorldbookLibrary } from "./components/WorldbookLibrary";
import { useSession } from "./store";

/**
 * 三栏：左（入口 + 会话记录）+ 主区 + 侧栏。
 *
 * **主区按页切换**（D172）：默认是正文流；点左栏上方的入口就整个换成那一页。
 * 独立页面不做浮层——浮层的语义是「临时打断，看完就关」，而编辑设定要待很久。
 *
 * 正文流的主区**只放要读的东西**（正文与输入）。状态与目标归侧栏，它们跨轮，
 * 不属于任何一轮；而每一轮自己的过程在正文流里（D167）。
 */
export function App(): JSX.Element {
  const bootstrap = useSession((s) => s.bootstrap);
  const disconnect = useSession((s) => s.disconnect);
  const page = useSession((s) => s.page);

  useEffect(() => {
    void bootstrap();
    return disconnect;
  }, [bootstrap, disconnect]);

  return (
    <div className="flex h-full">
      <SessionList />

      <main className="relative flex min-w-0 flex-1 flex-col">
        {page.kind === "story" ? <Story /> : null}
        {page.kind === "worldbooks" ? <WorldbookLibrary /> : null}
        {page.kind === "prompts" ? <Placeholder /> : null}
      </main>

      {/* 侧栏只在正文流下出现——独立页面要宽。 */}
      {page.kind === "story" && <Sidebar />}
    </div>
  );
}

function Story(): JSX.Element {
  const sessionId = useSession((s) => s.sessionId);
  const title = useSession((s) => s.session?.meta?.title);
  const model = useSession((s) => s.meta?.model);
  const developer = useSession((s) => s.meta?.developer ?? false);
  const worldbookId = useSession((s) => s.session?.worldbook_id ?? "");
  const books = useSession((s) => s.sessions);
  const go = useSession((s) => s.go);
  const [settings, setSettings] = useState(false);
  const [devtools, setDevtools] = useState(false);

  const worldbookName = books.find((c) => c.id === sessionId)?.worldbook_name;

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">{title || "未命名"}</span>
        <span className="font-mono text-[11px] text-dim">{model}</span>
        {/* **不是「本局设定」，是一个指向它的标签**（D172）：编辑永远在设定页
            发生，一件事一个入口。两个入口各自维护列表状态，迟早显示旧的东西。 */}
        {worldbookId && (
          <button
            onClick={() => go({ kind: "worldbooks", open: worldbookId })}
            className="rounded border border-line px-1.5 py-0.5 text-[11px] text-dim transition-colors hover:border-dim hover:text-fg"
            title="这局故事开在这份设定上——点击去编辑"
          >
            {worldbookName || "设定"} ↗
          </button>
        )}
        {developer && (
          <button
            onClick={() => setDevtools(true)}
            className="rounded border border-accent/40 px-1.5 py-0.5 font-mono text-[10px] text-accent transition-colors hover:bg-accent/10"
            title="每次模型往返的完整输入输出"
          >
            开发者模式
          </button>
        )}
        <div className="flex-1" />
        {sessionId && (
          <button className="btn" onClick={() => setSettings(true)}>
            配置
          </button>
        )}
      </header>

      <Notice />

      {sessionId ? (
        <>
          <Transcript />
          <QuestionPrompt />
          <GoalBar />
          <Composer />
        </>
      ) : (
        <div className="grid flex-1 place-items-center text-sm text-dim">
          左侧新建一个会话开始
        </div>
      )}

      {/* 这两个仍然是浮层：它们的语义确实是「临时打断，看完就关」。 */}
      {settings && sessionId && <SettingsPanel onClose={() => setSettings(false)} />}
      {devtools && sessionId && <DeveloperPanel onClose={() => setDevtools(false)} />}
    </>
  );
}

/** 提示词页还没做（UI.md §6 定了形态，实现排在批 3）。 */
function Placeholder(): JSX.Element {
  return (
    <div className="grid flex-1 place-items-center px-8 text-center text-sm text-dim">
      <div>
        <p>提示词管理还没做。</p>
        <p className="mt-2 text-xs text-dim/70">
          形态见 docs/design/UI.md §6：左 agent 列表 / 右「上下文 · 工具 · 特殊」，
          <br />
          三类条目（text / context / choice）可拖动排序。
        </p>
      </div>
    </div>
  );
}
