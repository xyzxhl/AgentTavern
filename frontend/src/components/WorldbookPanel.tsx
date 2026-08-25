import { useEffect, useRef, useState } from "react";

import { api } from "../api";
import type { EntryKind, Worldbook, WorldbookBundle, WorldbookEntry } from "../protocol";
import { useSession } from "../store";
import { JsonEditor } from "./JsonEditor";
import { Pick, YesNo } from "./Pick";

const KINDS = ["world", "character", "state"] as const;

const KIND_LABEL: Record<EntryKind, string> = {
  world: "世界设定",
  character: "角色设定",
  state: "初始状态",
};

const KIND_HINT: Record<EntryKind, string> = {
  world: "一段不受限制的文本：地方、组织、规矩、历史。世界里能有什么东西枚举不完，所以它没有额外字段。",
  character:
    "一段正文 + 语言习惯。和世界设定分开，是因为角色比别的东西多一样只进 writer 的属性：他怎么说话。",
  state: "会变的东西：在哪、知道什么、和谁什么关系。它是这局故事第一帧的状态，之后由剧情推进改。",
};

/**
 * 设定管理（worldbook，S67 / S65 / S63）。**它是一级资产，不属于任何 session**——
 * 一份设定能开出好几个故事。
 *
 * 所以这个组件**吃 `worldbookId` 而不是当前会话**：同一个界面既从故事里打开
 * （改这局开在的那个世界），也从设定库里打开（改任何一份）。
 *
 * 三类条目：世界设定（一段不受限制的文本）、角色设定（正文 + 语言习惯）、
 * 初始状态（一棵键值树）。**用户扮演的角色不是第四类**，它是角色设定加一个标记。
 *
 * 静态与会变的分开，是因为可见性天然不同步——一个角色的背景可能几百字、该按需
 * 展开，而他「在不在场」必须每轮都在眼前；共用一个开关时只能迁就其中一个。
 *
 * **占满主区而不是塞进侧栏**：正文常有几百字，在 320px 的窄栏里编辑很难受。
 */
export function WorldbookPanel({
  worldbookId,
  onClose,
}: {
  worldbookId: string;
  onClose: () => void;
}): JSX.Element {
  const refreshState = useSession((s) => s.refreshState);
  const openSession = useSession((s) => s.sessionId);
  // 故事一开始，初始状态就冻结了（S67）——之后改条目只影响还没开始的那些故事。
  // 这里只报**当前打开的这局**：别的故事各自冻结在各自的第一轮，管不着。
  const frozen = useSession(
    (s) => s.session?.worldbook_id === worldbookId && s.session?.initial_state != null,
  );

  const [book, setBook] = useState<Worldbook | null>(null);
  const [users, setUsers] = useState<string[]>([]);

  const [entries, setEntries] = useState<WorldbookEntry[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<WorldbookEntry | null>(null);
  const [valuesText, setValuesText] = useState("{}");
  const [valuesError, setValuesError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  async function reload(): Promise<void> {
    const got = await api.getWorldbook(worldbookId);
    setEntries(got.entries);
    setBook(got.worldbook);
    setUsers(got.sessions);
  }

  /** 改完把当前会话的 state 拉回来对齐——**故事还没开始时，改初始状态会立刻改变
   * 它**；不拉的话用户看到的还是旧值，而下一轮模型读到的已经是新的。 */
  async function settle(): Promise<void> {
    await reload();
    if (openSession) await refreshState();
  }

  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [worldbookId]);

  function open(entry: WorldbookEntry): void {
    setSelected(entry.id ?? null);
    setDraft({ ...entry });
    setValuesText(JSON.stringify(entry.values ?? {}, null, 2));
    setValuesError(null);
  }

  function create(kind: EntryKind): void {
    setSelected(null);
    setDraft({
      name: "新条目",
      kind,
      content: "",
      voice: "",
      is_player: false,
      mode: "always",
      values: {},
      depth: 0,
    });
    setValuesText("{}");
    setValuesError(null);
  }

  async function save(): Promise<void> {
    if (!draft) return;

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
      const saved = await api.saveEntry(worldbookId, { ...draft, values });
      await settle();
      open(saved);
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: WorldbookEntry): Promise<void> {
    if (!entry.id) return;

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
      await api.deleteEntry(worldbookId, entry.id);
      await settle();
      if (selected === entry.id) {
        setSelected(null);
        setDraft(null);
      }
    } finally {
      setBusy(false);
    }
  }

  /** 导出成一个文件。**浏览器里"下载"就是造一个 a 标签点它**，没有别的路。 */
  async function exportFile(): Promise<void> {
    const bundle = await api.exportWorldbook(worldbookId);
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(bundle, null, 2)], { type: "application/json" }),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = `${bundle.name || "worldbook"}.worldbook.json`;
    link.click();
    URL.revokeObjectURL(url);
  }

  async function importFile(picked: File): Promise<void> {
    // **替换是破坏性的，问一句**；追加不必问，多几条设定删掉就是。
    const replace = confirm(
      [
        `导入「${picked.name}」`,
        "",
        "确定 = 替换：先清空现有设定再导入。",
        "取消 = 追加：现有设定全留着。",
      ].join("\n"),
    );
    setBusy(true);
    try {
      const bundle = JSON.parse(await picked.text()) as WorldbookBundle;
      const done = await api.importWorldbook(worldbookId, bundle, replace);
      await settle();
      setDraft(null);
      setSelected(null);
      alert(
        [
          `导入了 ${done.imported} 条` +
            (done.removed ? `，替换掉 ${done.removed} 条` : "") +
            "。",
          // 冻结之后导入初始状态不会改写既有的每一轮，用户必须知道（S67）。**一份
          // 设定可以开出好几局故事**（S63），所以报的是有几局已经开始了。
          done.frozen.length
            ? `${done.frozen.length} 局故事已开始、起点已冻结：初始状态只对还没开始的那些生效。`
            : "",
        ]
          .filter(Boolean)
          .join("\n"),
      );
    } catch (err) {
      alert(`导入失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
      if (file.current) file.current.value = "";
    }
  }

  return (
    <div className="absolute inset-0 z-10 flex flex-col bg-ink-900">
      <header className="flex items-center gap-2 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">{book?.name || "世界设定"}</span>
        <span className="text-[11px] text-dim">{entries.length} 条</span>
        {/* **一份设定开出好几局故事**（S63）：改它对它们全都生效，这件事得看得见。 */}
        {users.length > 1 && (
          <span className="text-[11px] text-accent">· {users.length} 局故事在用</span>
        )}
        {frozen && <span className="text-[11px] text-dim">· 本局已开始，起点已冻结</span>}
        <div className="flex-1" />
        {KINDS.map((kind) => (
          <button key={kind} className="btn" onClick={() => create(kind)}>
            + {KIND_LABEL[kind]}
          </button>
        ))}
        <span className="mx-1 h-4 w-px bg-line" />
        <button className="btn" onClick={() => void exportFile()} disabled={!entries.length}>
          导出
        </button>
        <button className="btn" onClick={() => file.current?.click()} disabled={busy}>
          导入
        </button>
        <input
          ref={file}
          type="file"
          accept=".json,application/json"
          className="hidden"
          onChange={(e) => {
            const picked = e.target.files?.[0];
            if (picked) void importFile(picked);
          }}
        />
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
                <span className="text-accent/70">
                  {KIND_LABEL[entry.kind ?? "world"]}
                  {entry.is_player && " · 我"}
                </span>
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
  const kind = draft.kind ?? "world";
  const isState = kind === "state";
  const isCharacter = kind === "character";

  return (
    <div className="min-w-0 flex-1 overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-2xl space-y-5">
        <Field label="类型" hint={KIND_HINT[kind]}>
          <Pick<EntryKind>
            value={kind}
            options={KINDS.map((k) => [k, KIND_LABEL[k]])}
            onChange={(next) => setDraft({ ...draft, kind: next })}
          />
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
          <Pick<"always" | "on_demand">
            value={draft.mode ?? "always"}
            options={[
              ["always", "常驻"],
              ["on_demand", "按需展开"],
            ]}
            onChange={(mode) => setDraft({ ...draft, mode })}
          />
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

        {isCharacter && (
          <Field
            label="用户扮演的就是他"
            hint="**用户扮演的角色是一般角色的子类**：它就是个角色，只是多回答一个「用户是谁」。一份设定里至多一条——选了他，原来那个会自动取消。"
          >
            <YesNo
              value={draft.is_player ?? false}
              onChange={(yes) => setDraft({ ...draft, is_player: yes })}
            />
          </Field>
        )}

        {isCharacter && (
          <Field
            label="语言习惯"
            hint="他怎么说话。**只进 writer**——它不参与检索，参与每一个 token。"
          >
            <input
              value={draft.voice ?? ""}
              onChange={(e) => setDraft({ ...draft, voice: e.target.value })}
              placeholder="说话绕，答非所问，很少给准话"
              className="w-full rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
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
