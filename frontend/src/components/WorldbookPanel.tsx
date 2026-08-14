import { useEffect, useState } from "react";

import { api } from "../api";
import type { EntryKind, WorldbookEntry } from "../protocol";
import { useSession } from "../store";
import { JsonEditor } from "./JsonEditor";

const KINDS = ["lore", "state", "player"] as const;

const KIND_LABEL: Record<EntryKind, string> = {
  lore: "世界信息",
  state: "初始状态",
  player: "我扮演的角色",
};

const KIND_HINT: Record<EntryKind, string> = {
  lore: "静态的东西：性格、背景、身份、地方长什么样。不因剧情改变。",
  state: "会变的东西：在哪、知道什么、和谁什么关系。它是这局故事第一帧的状态，之后由剧情推进改。",
  player: "同世界信息，另外回答「用户扮演谁」。一份设定里至多一条。",
};

/**
 * 设定管理（worldbook，D90 / D158）。**用一份 worldbook 开启一个 session。**
 *
 * 条目分两类，外加一个特殊的：世界信息（一段正文）、初始状态（一棵键值树）、
 * 我扮演的角色。**分开的理由是可见性天然不同步**——一个角色的背景可能几百字、
 * 该按需展开，而他「在不在场」必须每轮都在眼前；共用一个开关时只能迁就其中一个。
 *
 * **占满主区而不是塞进侧栏**：正文常有几百字，在 320px 的窄栏里编辑很难受。
 */
export function WorldbookPanel({ onClose }: { onClose: () => void }): JSX.Element {
  const sessionId = useSession((s) => s.sessionId);
  const refreshState = useSession((s) => s.refreshState);
  // 故事一开始，初始状态就冻结了（D158）——之后改条目只影响以后新建的会话。
  const frozen = useSession((s) => s.session?.initial_state != null);

  const [entries, setEntries] = useState<WorldbookEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<WorldbookEntry | null>(null);
  const [valuesText, setValuesText] = useState("{}");
  const [valuesError, setValuesError] = useState<string | null>(null);
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
    setValuesText(JSON.stringify(entry.values ?? {}, null, 2));
    setValuesError(null);
  }

  function create(kind: EntryKind): void {
    setSelected(null);
    setDraft({ name: "新条目", kind, content: "", mode: "always", values: {}, depth: 0 });
    setValuesText("{}");
    setValuesError(null);
  }

  async function save(): Promise<void> {
    if (!sessionId || !draft) return;

    let values: Record<string, unknown> = {};
    if (draft.kind === "state") {
      try {
        values = JSON.parse(valuesText || "{}") as Record<string, unknown>;
      } catch (err) {
        setValuesError(`不是合法 JSON：${(err as Error).message}`);
        return;
      }
      if (values === null || typeof values !== "object" || Array.isArray(values)) {
        setValuesError("初始状态必须是一个对象：键 → 值。");
        return;
      }
    }

    setBusy(true);
    try {
      const saved = await api.saveWorldbookEntry(sessionId, { ...draft, values });
      await reload();
      // 故事还没开始时，改初始状态会立刻改变当前 state——把侧栏那份拉回来对齐，
      // 否则用户看到的还是旧值，而下一轮模型读到的已经是新的。
      await refreshState();
      open(saved);
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: WorldbookEntry): Promise<void> {
    if (!sessionId || !entry.id) return;

    const keys = Object.keys(entry.values ?? {});
    let warning = "";
    if (entry.kind === "state" && keys.length) {
      warning = frozen
        ? "\n\n故事已经开始、起点已冻结：删掉它只影响以后新建的会话，既有的每一轮不受影响。"
        : `\n\n它是初始状态的一部分（${keys.join("、")}）。故事还没开始，删掉立刻生效。`;
    }
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
      <header className="flex items-center gap-2 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">世界设定</span>
        <span className="text-[11px] text-dim">{entries.length} 条</span>
        {frozen && (
          <span className="text-[11px] text-dim">· 故事已开始，起点已冻结</span>
        )}
        <div className="flex-1" />
        {KINDS.map((kind) => (
          <button key={kind} className="btn" onClick={() => create(kind)}>
            + {KIND_LABEL[kind]}
          </button>
        ))}
        <button className="btn" onClick={onClose}>
          关闭
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="w-64 shrink-0 overflow-y-auto border-r border-line p-2">
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
                <span className="text-accent/70">{KIND_LABEL[entry.kind ?? "lore"]}</span>
                {" · "}
                {entry.kind === "state"
                  ? `${Object.keys(entry.values ?? {}).length} 项`
                  : entry.content?.slice(0, 20) || "（空）"}
              </div>
            </button>
          ))}
        </div>

        {draft ? (
          <Editor
            draft={draft}
            setDraft={setDraft}
            valuesText={valuesText}
            setValuesText={setValuesText}
            valuesError={valuesError}
            busy={busy}
            onSave={save}
            onDelete={selected ? () => remove(draft) : undefined}
          />
        ) : (
          <div className="grid flex-1 place-items-center text-sm text-dim">选一条设定，或新建</div>
        )}
      </div>
    </div>
  );
}

interface EditorProps {
  draft: WorldbookEntry;
  setDraft: (entry: WorldbookEntry) => void;
  valuesText: string;
  setValuesText: (text: string) => void;
  valuesError: string | null;
  busy: boolean;
  onSave: () => void;
  onDelete?: () => void;
}

function Editor(props: EditorProps): JSX.Element {
  const { draft, setDraft, valuesText, setValuesText, valuesError, busy, onSave, onDelete } = props;
  const kind = draft.kind ?? "lore";
  const isState = kind === "state";

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-2xl space-y-5">
        <Field label="类型" hint={KIND_HINT[kind]}>
          <div className="flex gap-2">
            {KINDS.map((option) => (
              <button
                key={option}
                onClick={() => setDraft({ ...draft, kind: option })}
                className={`btn ${kind === option ? "border-accent text-fg" : ""}`}
              >
                {KIND_LABEL[option]}
              </button>
            ))}
          </div>
        </Field>

        <Field label="名称">
          <input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            className="w-full rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg focus:border-dim focus:outline-none"
          />
        </Field>

        <Field
          label="是否可展开"
          hint={
            isState
              ? "常驻：整棵树每轮都在眼前。按需：深过下面那个层级的部分只给条数，模型用 state_query 取。"
              : "常驻：整段每轮进上下文。按需：只进索引，模型用 worldinfo_read 展开。全部条目都常驻时，展开工具根本不注册。"
          }
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

        {isState && draft.mode === "on_demand" && (
          <Field
            label="展开层级"
            hint="给到第几层。0 = 不折叠。深过这一层的子树只显示有几项，模型按需取。"
          >
            <input
              type="number"
              min={0}
              value={draft.depth ?? 0}
              onChange={(e) => setDraft({ ...draft, depth: Number(e.target.value) || 0 })}
              className="w-24 rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg focus:border-dim focus:outline-none"
            />
          </Field>
        )}

        {isState ? (
          <Field
            label="初始状态"
            hint="从属关系用嵌套写，不用点分键名：{ 林昭: { 在场: true } }。Tab 缩进。"
          >
            <JsonEditor
              value={valuesText}
              onChange={setValuesText}
              error={valuesError}
              rows={14}
            />
          </Field>
        ) : (
          <Field label="正文" hint={KIND_HINT[kind]}>
            <textarea
              value={draft.content ?? ""}
              onChange={(e) => setDraft({ ...draft, content: e.target.value })}
              rows={14}
              className="w-full resize-y rounded border border-line bg-ink-800 px-3 py-2 text-sm leading-relaxed text-fg focus:border-dim focus:outline-none"
            />
          </Field>
        )}

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
        <span className="shrink-0 text-xs font-medium text-fg">{label}</span>
        {hint && <span className="min-w-0 flex-1 text-[11px] leading-snug text-dim">{hint}</span>}
      </div>
      {children}
    </div>
  );
}
