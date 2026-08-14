import { useEffect } from "react";

import { Composer } from "./components/Composer";
import { Notice } from "./components/Notice";
import { QuestionPrompt } from "./components/QuestionPrompt";
import { SessionList } from "./components/SessionList";
import { Sidebar } from "./components/Sidebar";
import { Transcript } from "./components/Transcript";
import { useSession } from "./store";

/**
 * 三栏：左会话列表 + 右主区 + 侧栏。
 *
 * 主区**只放要读的东西**（正文与输入）。agent 在做什么、状态成了什么样，
 * 都归侧栏——它们是过程，不是内容，混在一起读正文时会被打断。
 */
export function App(): JSX.Element {
  const bootstrap = useSession((s) => s.bootstrap);
  const disconnect = useSession((s) => s.disconnect);
  const sessionId = useSession((s) => s.sessionId);
  const title = useSession((s) => s.session?.meta?.title);
  const model = useSession((s) => s.meta?.model);
  const developer = useSession((s) => s.meta?.developer ?? false);

  useEffect(() => {
    void bootstrap();
    return disconnect;
  }, [bootstrap, disconnect]);

  return (
    <div className="flex h-full">
      <SessionList />

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
          <span className="text-sm text-fg">{title || "未命名"}</span>
          <span className="font-mono text-[11px] text-dim">{model}</span>
          {developer && (
            <span className="rounded border border-accent/40 px-1.5 py-0.5 font-mono text-[10px] text-accent">
              开发者模式
            </span>
          )}
        </header>

        <Notice />

        {sessionId ? (
          <>
            <Transcript />
            <QuestionPrompt />
            <Composer />
          </>
        ) : (
          <div className="grid flex-1 place-items-center text-sm text-dim">
            左侧新建一个会话开始
          </div>
        )}
      </main>

      <Sidebar />
    </div>
  );
}
