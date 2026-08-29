import { useEffect, useState } from "react";

import { apply, load, watchSystem } from "./appearance";
import { PromptsPage } from "./components/PromptsPage";
import { Composer } from "./components/Composer";
import { DeveloperPanel } from "./components/DeveloperPanel";
import { GoalBar } from "./components/GoalBar";
import { Notice } from "./components/Notice";
import { QuestionPrompt } from "./components/QuestionPrompt";
import { SessionList } from "./components/SessionList";
import { SettingsPage } from "./components/SettingsPage";
import { Sidebar } from "./components/Sidebar";
import { Transcript } from "./components/Transcript";
import { WorldbookLibrary } from "./components/WorldbookLibrary";
import { useSession } from "./store";

/**
 * 三栏：左（入口 + 会话记录）+ 主区 + 侧栏。
 *
 * **主区按页切换**（R17）：默认是正文流；点左栏上方的入口就整个换成那一页。
 * 独立页面不做浮层——浮层的语义是「临时打断，看完就关」，而编辑设定要待很久。
 *
 * 正文流的主区**只放要读的东西**（正文与输入）。状态与目标归侧栏，它们跨轮，
 * 不属于任何一轮；而每一轮自己的过程在正文流里（S125）。
 */
export function App(): JSX.Element {
  const bootstrap = useSession((s) => s.bootstrap);
  const disconnect = useSession((s) => s.disconnect);
  const page = useSession((s) => s.page);

  useEffect(() => {
    // 外观在最早一刻贴上去——晚一帧就是一次可见的闪白（S133）。
    apply(load());
    const stop = watchSystem(load);
    void bootstrap();
    return () => {
      stop();
      disconnect();
    };
  }, [bootstrap, disconnect]);

  return (
    <div className="flex h-full">
      <SessionList />

      <main className="relative flex min-w-0 flex-1 flex-col">
        {page.kind === "story" ? <Story /> : null}
        {page.kind === "worldbooks" ? <WorldbookLibrary /> : null}
        {page.kind === "agents" ? <PromptsPage group="agent" /> : null}
        {page.kind === "calls" ? <PromptsPage group="call" /> : null}
        {page.kind === "settings" ? <SettingsPage /> : null}
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
  const usage = useSession((s) => s.usage);
  const go = useSession((s) => s.go);
  const [devtools, setDevtools] = useState(false);

  const worldbookName = books.find((c) => c.id === sessionId)?.worldbook_name;
  const spent = usage.tokensIn + usage.tokensOut;

  return (
    <>
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">{title || "未命名"}</span>
        <span className="font-mono text-[11px] text-dim">{model}</span>
        {/* **不是「本局设定」，是一个指向它的标签**（R17）：编辑永远在设定页
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
        {/* **成本要看得见**（R24 那条「实时显示消耗，由用户手动打断」）。
            store 每轮都在累加，而界面此前一个字都没有。只算这次连接之后的——
            它是"刚才这一段花了多少"，不是这局故事的总账。 */}
        {spent > 0 && (
          <span
            className="font-mono text-[11px] text-dim"
            title={`本次连接：入 ${usage.tokensIn} / 出 ${usage.tokensOut}`}
          >
            {compact(spent)} tok
          </span>
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

      {/* 这个仍然是浮层：它的语义确实是「临时打断，看完就关」。 */}
      {devtools && sessionId && <DeveloperPanel onClose={() => setDevtools(false)} />}
    </>
  );
}

function compact(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
