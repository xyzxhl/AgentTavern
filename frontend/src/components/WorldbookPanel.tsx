import { useEffect, useState } from "react";

import { api } from "../api";
import type { WorldbookEntry } from "../protocol";
import { useSession } from "../store";

/**
 * 设定管理（worldbook，D90）。**用一份 worldbook 开启一个 session**，
 * worldinfo 和初始 state 都是它的投影：
 *
 * - `content` → worldinfo。静态的东西：性格、背景、身份。由用户改。
 * - `seeds`   → 初始 state。会变的东西：在哪、知道什么、和谁什么关系。之后由剧情推进改。
 *
 * 一个条目通常两边都出。**种子存在的意义是移除诱因**：不给的话模型只能把
 * 「他现在在哪」从设定里抄进 state，抄出一份会随设定改动而分叉的副本。
 *
 * **占满主区而不是塞进侧栏**：content 常有几百字，在 320px 的窄栏里编辑很难受。
 */
export function WorldbookPanel({ onClose }: { onClose: () => void }): JSX.Element {
  const sessionId = useSession((s) => s.sessionId);
  const refreshState = useSession((s) => s.refreshState);

  const [entries, setEntries] = useState<WorldbookEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<WorldbookEntry | null>(null);
  const [seedsText, setSeedsText] = useState("{}");
  const [seedsError, setSeedsError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function reload(): Promise<void> {
    if (!sessionId) return;
    setEntries(await api.listWorldbook(sessionId));
  }

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  function open(entry: WorldbookEntry): void {
    setSelected(entry.id ?? null);
    setDraft({ ...entry });
    setSeedsText(JSON.stringify(entry.seeds ?? {}, null, 2));
    setSeedsError(null);
  }

  function create(): void {
    const fresh: WorldbookEntry = { name: "新条目", content: "", mode: "always", seeds: {} };
    setSelected(null);
    setDraft(fresh);
    setSeedsText("{}");
    setSeedsError(null);
  }

  async function save(): Promise<void> {
    if (!sessionId || !draft) return;
    let seeds: Record<string, unknown>;
    try {
      seeds = JSON.parse(seedsText || "{}") as Record<string, unknown>;
    } catch (err) {
      setSeedsError(`种子不是合法 JSON：${(err as Error).message}`);
      return;
    }
    if (seeds === null || typeof seeds !== "object" || Array.isArray(seeds)) {
      setSeedsError("种子必须是一个对象：键 → 值。");
      return;
    }

    setBusy(true);
    try {
      const saved = await api.saveWorldbookEntry(sessionId, { ...draft, seeds });
      await reload();
      // **种子改了，每一轮的状态都跟着变**——立刻把侧栏那份拉回来对齐，
      // 否则用户看到的还是旧值，而下一轮模型读到的已经是新的。
      await refreshState();
      open(saved);
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: WorldbookEntry): Promise<void> {
    if (!sessionId || !entry.id) return;
    const seedKeys = Object.keys(entry.seeds ?? {});
    const warning = seedKeys.length
      ? `\n\n它带着 ${seedKeys.length} 个种子（${seedKeys.join("、")}），` +
        "删掉之后这几个键会从**所有分支**的起点消失。"
      : "";
    if (!confirm(`删除「${entry.name}」？${warning}`)) return;

    setBusy(true);
    try {
      await api.deleteWorldbookEntry(sessionId, entry.id);
      await reload();
      await refreshState();
      if (selected === entry.id) {
        setSelected(null);
        setDraft(null);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="absolute inset-0 z-10 flex flex-col bg-ink-900">
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">世界设定</span>
        <span className="text-[11px] text-dim">{entries.length} 条</span>
        <div className="flex-1" />
        <button className="btn" onClick={create}>
          新建
        </button>
        <button className="btn" onClick={onClose}>
          关闭
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="w-60 shrink-0 overflow-y-auto border-r border-line p-2">
          {entries.length === 0 && (
            <p className="px-1 py-2 text-xs text-dim">还没有设定。新建一条开始。</p>
          )}
          {entries.map((entry) => (
            <button
              key={entry.id}
              onClick={() => open(entry)}
              className={`mb-0.5 block w-full rounded px-2 py-1.5 text-left transition-colors ${
                entry.id === selected ? "bg-ink-600" : "hover:bg-ink-700"
              }`}
            >
              <div className="flex items-baseline gap-1.5">
                <span className="min-w-0 flex-1 truncate text-xs text-fg">{entry.name}</span>
                {entry.mode === "on_demand" && (
                  <span className="shrink-0 text-[10px] text-dim">按需</span>
                )}
              </div>
              <div className="mt-0.5 truncate text-[11px] text-dim">
                {Object.keys(entry.seeds ?? {}).length > 0 &&
                  `${Object.keys(entry.seeds ?? {}).length} 个种子 · `}
                {entry.content?.slice(0, 24) || "（空）"}
              </div>
            </button>
          ))}
        </div>

        {draft ? (
          <Editor
            draft={draft}
            setDraft={setDraft}
            seedsText={seedsText}
            setSeedsText={setSeedsText}
            seedsError={seedsError}
            busy={busy}
            onSave={save}
            onDelete={selected ? () => remove(draft) : undefined}
          />
        ) : (
          <div className="grid flex-1 place-items-center text-sm text-dim">
            选一条设定，或新建
          </div>
        )}
      </div>
    </div>
  );
}

interface EditorProps {
  draft: WorldbookEntry;
  setDraft: (entry: WorldbookEntry) => void;
  seedsText: string;
  setSeedsText: (text: string) => void;
  seedsError: string | null;
  busy: boolean;
  onSave: () => void;
  onDelete?: () => void;
}

function Editor(props: EditorProps): JSX.Element {
  const { draft, setDraft, seedsText, setSeedsText, seedsError, busy, onSave, onDelete } = props;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-2xl space-y-5">
        <Field label="名称">
          <input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            className="w-full rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg focus:border-dim focus:outline-none"
          />
        </Field>

        <Field
          label="注入方式"
          hint="常驻：每轮整段进上下文。按需：只进索引，模型按需展开。全部条目都常驻时，展开工具根本不注册。"
        >
          <div className="flex gap-2">
            {(["always", "on_demand"] as const).map((mode) => (
              <button
                key={mode}
                onClick={() => setDraft({ ...draft, mode })}
                className={`btn ${draft.mode === mode ? "border-accent text-fg" : ""}`}
              >
                {mode === "always" ? "常驻" : "按需展开"}
              </button>
            ))}
          </div>
        </Field>

        <Field label="正文" hint="静态的东西：性格、背景、身份。不因剧情改变的那部分。">
          <textarea
            value={draft.content ?? ""}
            onChange={(e) => setDraft({ ...draft, content: e.target.value })}
            rows={12}
            className="w-full resize-y rounded border border-line bg-ink-800 px-3 py-2 text-sm leading-relaxed text-fg focus:border-dim focus:outline-none"
          />
        </Field>

        <Field
          label="种子"
          hint="会变的东西：在哪、知道什么、和谁什么关系。它们是初始 state，之后由剧情推进改。改这里会影响所有分支。"
        >
          <textarea
            value={seedsText}
            onChange={(e) => setSeedsText(e.target.value)}
            rows={8}
            spellCheck={false}
            className="w-full resize-y rounded border border-line bg-ink-800 px-3 py-2 font-mono text-xs text-fg focus:border-dim focus:outline-none"
          />
          {seedsError && <p className="mt-1 text-xs text-red-400">{seedsError}</p>}
        </Field>

        <div className="flex items-center gap-2 pb-6">
          <button className="btn border-accent text-fg" onClick={onSave} disabled={busy}>
            保存
          </button>
          {onDelete && (
            <button className="btn" onClick={onDelete} disabled={busy}>
              删除
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-2">
        <span className="text-xs font-medium text-fg">{label}</span>
        {hint && <span className="min-w-0 flex-1 text-[11px] leading-snug text-dim">{hint}</span>}
      </div>
      {children}
    </div>
  );
}
