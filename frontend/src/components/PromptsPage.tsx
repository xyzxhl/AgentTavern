import { useEffect, useState } from "react";

import {
  api,
  type PromptCard,
  type PromptDoc,
  type PromptGroup,
  type PromptSetCard,
  type Section,
} from "../api";
import { type Page, useSession } from "../store";
import { EditableTitle } from "./EditableTitle";
import { JsonEditor } from "./JsonEditor";
import { Pick, YesNo } from "./Pick";

/** 每一类默认打开哪一份——列表还没回来时也得有个选中项。 */
const FIRST: Record<PromptGroup, string> = { agent: "director", call: "summarize" };

/** 这一类住在哪个入口下。**两个入口并列**（S147）。 */
function pageOf(group: PromptGroup, open: string): Page {
  return group === "agent" ? { kind: "agents", open } : { kind: "calls", open };
}

/**
 * 提示词管理：**所有被输入模型的内容，用户都能审查和修改**（R31）。
 *
 * 这条判据比"提示词可编辑"强得多，它同时管**工具描述**、**运行时另外追加的
 * 整条消息**、以及嵌在渲染里的那些短句。所以这一页分三块，每块对应 YAML 里
 * 的一个键：
 *
 * | | 装什么 |
 * |---|---|
 * | **上下文** | 它收到的那一串消息（三类条目，可拖动排序） |
 * | **工具** | 它拿得到的工具，每个一条描述 + 开关；参数只读 |
 * | **特殊** | 运行时另外追加的整条消息，以及渲染里嵌的短句 |
 *
 * **同一套编辑界面，两个并列的入口**（S147）：`agent`（一个角色，名词命名）和
 * `call`（一次单次调用，动词命名）。编辑方式相同，但**不共用一个入口**——
 * 并进一页的一个小标题底下，读起来就成了「agent，以及一些附属的东西」，
 * 而那个误解会影响判断：比如以为该给单次调用配工具面。
 *
 * **保存前服务端会试装配一遍**：装不出来就拒绝并说是哪一节坏了——否则改坏一个
 * 字，下一轮生成直接 500，而错误信息离原因很远。
 */
export function PromptsPage({ group }: { group: PromptGroup }): JSX.Element {
  const page = useSession((s) => s.page);
  const go = useSession((s) => s.go);
  const opened = page.kind === "agents" || page.kind === "calls" ? page.open : undefined;
  const picked = opened ?? FIRST[group];

  const [cards, setCards] = useState<PromptCard[]>([]);
  const [sets, setSets] = useState<PromptSetCard[]>([]);
  const [doc, setDoc] = useState<PromptDoc | null>(null);
  const [tab, setTab] = useState<Tab>("context");
  const [problem, setProblem] = useState("");
  const [saved, setSaved] = useState(false);
  const [dirty, setDirty] = useState(false);
  // **正在编辑的东西还不合法时，保存必须停下来。** 这里的「额外请求参数」是一段
  // JSON：解析不出来时它压根没进 `doc`，而按下保存会**把上一份原样写回去**，
  // 页头还写着「已保存」——用户以为自己那段改动生效了。同 S135：
  // 一个不出声的丢弃和失败没有区别。
  const [blocked, setBlocked] = useState<string | null>(null);

  const activeSet = sets.find((item) => item.active);

  async function reloadLibrary(): Promise<void> {
    const [nextSets, nextCards] = await Promise.all([api.listPromptSets(), api.listPrompts()]);
    setSets(nextSets);
    setCards(nextCards);
  }

  useEffect(() => {
    void reloadLibrary().catch((err: unknown) => setProblem((err as Error).message));
  }, []);

  useEffect(() => {
    setDoc(null);
    setProblem("");
    setBlocked(null);
    void api
      .getPrompt(picked)
      .then((next) => {
        setDoc(next);
        setDirty(false);
      })
      .catch((err: unknown) => setProblem((err as Error).message));
  }, [picked, activeSet?.id]);

  // **草稿改在本地，保存才落盘。** 一边拖顺序一边每步都写盘的话，中途那些不
  // 成立的中间状态会被试装配拦下来，用户得到一串莫名其妙的报错。
  const data = doc?.data ?? {};

  function patch(next: Record<string, unknown>): void {
    if (!doc) return;
    setDoc({ ...doc, data: { ...doc.data, ...next } });
    setSaved(false);
    setDirty(true);
  }

  async function save(): Promise<void> {
    if (!doc) return;
    if (blocked) {
      setSaved(false);
      setProblem(blocked);
      return;
    }
    setProblem("");
    try {
      setDoc(await api.putPrompt(doc.name, doc.data));
      setSaved(true);
      setDirty(false);
      setCards(await api.listPrompts());
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  async function activate(id: string): Promise<void> {
    if (id === activeSet?.id) return;
    if (dirty && !confirm("当前提示词还有未保存修改。放弃修改并切换套装？")) return;
    setProblem("");
    setSaved(false);
    try {
      await api.activatePromptSet(id);
      await reloadLibrary();
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  async function duplicate(): Promise<void> {
    if (!activeSet) return;
    if (dirty && !confirm("当前提示词还有未保存修改。副本只包含已保存内容，仍要继续？")) return;
    setProblem("");
    setSaved(false);
    try {
      await api.copyPromptSet(activeSet.id);
      await reloadLibrary();
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  async function renameSet(name: string): Promise<void> {
    if (!activeSet || activeSet.id === "default") return;
    setProblem("");
    try {
      await api.renamePromptSet(activeSet.id, name);
      await reloadLibrary();
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  async function removeSet(): Promise<void> {
    if (!activeSet || activeSet.id === "default") return;
    if (dirty && !confirm("当前提示词还有未保存修改。删除套装会一并放弃这些修改，仍要继续？")) {
      return;
    }
    if (!confirm(`删除提示词套装「${activeSet.name}」？当前进行中的 turn 不受影响。`)) return;
    setProblem("");
    setSaved(false);
    try {
      await api.deletePromptSet(activeSet.id);
      await reloadLibrary();
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  return (
    <div className="flex min-h-0 flex-1">
      {/* **这一栏只列这一类**（S147）——分组是靠两个并列的入口做的，不是靠这里
          的一个小标题。`group` 由后端给，前端不按名字猜：加一份 YAML 就要同时
          改两处的话，漏改的症状是它悄悄出现在错误的那一类里。 */}
      <nav className="w-40 shrink-0 border-r border-line p-2">
        {cards
          .filter((c) => c.group === group)
          .map((card) => (
            <button
              key={card.name}
              onClick={() => {
                if (dirty && !confirm("当前提示词还有未保存修改。放弃修改并打开另一份？")) {
                  return;
                }
                go(pageOf(group, card.name));
              }}
              className={`mb-0.5 block w-full rounded px-2 py-1.5 text-left transition-colors ${
                card.name === picked ? "bg-ink-600" : "hover:bg-ink-700"
              }`}
            >
              <span
                className={`block truncate text-xs ${card.name === picked ? "text-fg" : "text-dim"}`}
              >
                {LABEL[card.name] ?? card.name}
              </span>
              <span className="block text-[10px] text-dim/70">
                {card.sections} 段 · {card.tools} 工具
                {!card.enabled && " · 已关"}
              </span>
            </button>
          ))}
      </nav>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-line bg-ink-800 px-5 py-2">
          <span className="text-[11px] text-dim">套装</span>
          {activeSet && sets.length > 0 && (
            <Pick<string>
              value={activeSet.id}
              options={sets.map((item) => [item.id, item.name])}
              onChange={(id) => void activate(id)}
            />
          )}
          <span className="mx-1 h-4 w-px bg-line" />
          {activeSet?.id === "default" ? (
            <span className="text-xs text-fg">默认</span>
          ) : activeSet ? (
            <EditableTitle
              value={activeSet.name}
              onCommit={(name) => void renameSet(name)}
              className="text-xs text-fg"
            />
          ) : null}
          <div className="flex-1" />
          <button className="btn" onClick={() => void duplicate()} disabled={!activeSet}>
            复制当前套装
          </button>
          {activeSet && activeSet.id !== "default" && (
            <button className="btn" onClick={() => void removeSet()}>
              删除套装
            </button>
          )}
        </div>
        <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
          <span className="text-sm text-fg">{LABEL[picked] ?? picked}</span>
          <span className="font-mono text-[11px] text-dim">{picked}</span>
          {"enabled" in data && (
            <span className="flex items-center gap-1.5" title="关掉它，这一路整个不跑">
              <span className="text-[11px] text-dim">启用</span>
              <YesNo
                value={data.enabled !== false}
                onChange={(on) => patch({ enabled: on })}
              />
            </span>
          )}
          <div className="flex-1" />
          <Pick<Tab>
            value={tab}
            options={Object.entries(TAB) as [Tab, string][]}
            onChange={(next) => {
              setBlocked(null);
              setTab(next);
            }}
          />
          <button className="btn" onClick={() => void save()} disabled={!doc || !!blocked}>
            保存
          </button>
        </header>

        {problem && (
          <p className="border-b border-line bg-ink-800 px-5 py-2 text-[11px] leading-snug text-accent">
            {problem}
          </p>
        )}
        {saved && !problem && (
          <p className="border-b border-line px-5 py-1.5 text-[11px] text-dim">
            已保存，下一轮就生效。
          </p>
        )}

        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          {!doc ? (
            <p className="text-sm text-dim">读取中……</p>
          ) : tab === "call" ? (
            <CallKnobs data={data} onChange={patch} onBlocked={setBlocked} />
          ) : tab === "context" ? (
            <Sections
              items={(data.sections as Section[]) ?? []}
              onChange={(sections) => patch({ sections })}
            />
          ) : tab === "tools" ? (
            <Tools doc={doc} onChange={(tools) => patch({ tools })} />
          ) : (
            <Specials data={data} onChange={patch} />
          )}
        </div>
      </div>
    </div>
  );
}

const LABEL: Record<string, string> = {
  director: "推演",
  writer: "成文",
  state_manager: "状态",
  goal_manager: "目标",
  summarize: "摘要",
};

const TAB = { call: "调用", context: "上下文", tools: "工具", specials: "特殊" };
type Tab = keyof typeof TAB;

/** 一条消息能以谁的身份发出去。**没有 `tool`**：那个 role 的消息必须配着一次
 * 真实的工具调用（`tool_call_id`），装配器摆不出来。 */
const ROLES: [string, string][] = [
  ["system", "system"],
  ["user", "user"],
  ["assistant", "assistant"],
];

/**
 * 调用旋钮：**用哪个模型、流不流、额外塞什么进请求体**（S159）。
 *
 * 它们按 agent 各设一份，住在那份 YAML 顶层。以前 `model` 和 `stream` 在
 * `.env` 里、全局一份——而它们全都在改「这一次调用长什么样」，那正是这一页
 * 管的事（同 S80 那次上调的判据）。
 */
function CallKnobs({
  data,
  onChange,
  onBlocked,
}: {
  data: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  /** 这段 JSON 现在解析不出来——**保存要停下来**，否则那次改动被静默丢弃。 */
  onBlocked: (why: string | null) => void;
}): JSX.Element {
  const extra = (data.extra ?? {}) as Record<string, unknown>;
  const [text, setText] = useState(() => JSON.stringify(extra, null, 2));
  const [bad, setBad] = useState<string | null>(null);

  function edit(next: string): void {
    setText(next);
    const why = parse(next);
    setBad(why);
    onBlocked(why);
  }

  /** 解析并落进草稿；返回拦下保存的理由，没有就是 `null`。 */
  function parse(next: string): string | null {
    try {
      const parsed: unknown = JSON.parse(next || "{}");
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        return "必须是一个对象：键 → 值。";
      }
      onChange({ extra: parsed });
      return null;
    } catch (err) {
      return `不是合法 JSON：${(err as Error).message}`;
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <Knob
        label="模型"
        hint="这一份用哪个模型。留空会在调用那一刻当场报错——没有全局兜底，否则某一份忘了写，它会安静地用着别人的那个。"
      >
        <input
          value={String(data.model ?? "")}
          onChange={(e) => onChange({ model: e.target.value })}
          placeholder="模型名"
          className="w-72 rounded border border-line bg-ink-800 px-3 py-1.5 text-sm text-fg placeholder:text-dim/60 focus:border-dim focus:outline-none"
        />
      </Knob>

      <Knob label="边写边显示" hint="关掉则这一份写完才整段出现，产出完全一样。正文值得流着看，状态维护不必——所以它按 agent 分。">
        <YesNo value={data.stream !== false} onChange={(on) => onChange({ stream: on })} />
      </Knob>

      <Knob
        label="额外请求参数"
        hint="原样并进请求体，一个字都不解释。思考模式那类参数走这里——各家拼法不同，这里不替你猜。"
      >
        <JsonEditor value={text} onChange={edit} error={bad} rows={8} />
        <p className="mt-1.5 text-[11px] leading-snug text-dim">
          例：DeepSeek 是 <code className="font-mono text-accent">
            {'{"thinking": {"type": "enabled"}}'}
          </code>，OpenAI o 系是 <code className="font-mono text-accent">
            {'{"reasoning_effort": "high"}'}
          </code>。
        </p>
      </Knob>
    </div>
  );
}

function Knob({
  label,
  hint,
  children,
}: {
  label: string;
  hint: string;
  children: React.ReactNode;
}): JSX.Element {
  return (
    <div>
      <div className="mb-1.5 flex items-baseline gap-2">
        <span className="shrink-0 text-xs font-medium text-fg">{label}</span>
        <span className="min-w-0 flex-1 text-[11px] leading-snug text-dim">{hint}</span>
      </div>
      {children}
    </div>
  );
}

const KIND: Record<Section["type"], string> = {
  text: "text",
  context: "ctx",
  choice: "choice",
};

/** 上下文：三类条目，**统一可上下拖动排序**（R31）。 */
function Sections({
  items,
  onChange,
}: {
  items: Section[];
  onChange: (next: Section[]) => void;
}): JSX.Element {
  const [open, setOpen] = useState<number | null>(null);
  const [drag, setDrag] = useState<number | null>(null);

  function move(from: number, to: number): void {
    if (from === to) return;
    const next = [...items];
    const [item] = next.splice(from, 1);
    if (item) next.splice(to, 0, item);
    onChange(next);
    setOpen(null);
  }

  function edit(at: number, patch: Partial<Section>): void {
    onChange(items.map((s, i) => (i === at ? { ...s, ...patch } : s)));
  }

  function remove(at: number): void {
    const item = items[at];
    // **删一条 `context` 是在拿掉一整块数据**（前情、当前状态、本轮细纲……），
    // 而装配器不会为此报错——它只拦"未知的 context"，不拦"少了一段"。
    // 所以这里当场问一句：少给一段的症状是产出悄悄变差，不是报错。
    if (item?.type === "context" && !confirm(`删掉「${item.source}」这一整段数据？`)) return;
    onChange(items.filter((_, i) => i !== at));
    setOpen(null);
  }

  function add(): void {
    onChange([...items, { type: "text", role: "user", body: "" }]);
    setOpen(items.length);
  }

  return (
    <div className="mx-auto max-w-3xl">
      <p className="mb-3 text-[11px] leading-snug text-dim">
        一个条目 = <b className="font-medium text-fg">一条消息</b>，顺序就是这里的顺序。
        <br />
        <b className="font-medium text-fg">装配器不加任何标签</b>
        ——要 <code className="font-mono text-accent">&lt;输出契约&gt;</code>{" "}
        这样的标记就自己写进正文。编辑器里看到的那段字，必须就是模型收到的那段字。
      </p>

      {items.map((item, at) => (
        <article
          key={at}
          draggable
          onDragStart={() => setDrag(at)}
          onDragOver={(e) => e.preventDefault()}
          onDrop={() => drag !== null && move(drag, at)}
          className="mb-1 rounded border border-line"
        >
          <div className="flex items-center gap-2 px-2 py-1.5">
            <span className="cursor-grab select-none text-dim" title="拖动改顺序">
              ⠿
            </span>
            <span className="w-12 shrink-0 font-mono text-[10px] text-accent">
              {KIND[item.type]}
            </span>
            <span className="w-16 shrink-0 font-mono text-[10px] text-dim">{item.role ?? "—"}</span>
            <span className="min-w-0 flex-1 truncate text-xs text-dim">{preview(item)}</span>
            <button className="btn" onClick={() => setOpen(open === at ? null : at)}>
              ✎
            </button>
            <button className="btn" onClick={() => remove(at)} title="删掉这一条">
              ✕
            </button>
          </div>

          {open === at && (
            <div className="border-t border-line p-2">
              {/* **role 三种都能选**（R31）：它决定这条消息以谁的身份发出去，
                  和正文是同一件事的两半。`context` 节的内容由系统生成，
                  但它的 role 同样是模型收到的东西，所以那一类也能改。

                  **本来就没有 role 的那一节不给选**：前情是一串消息，每条自带
                  自己的 role，给它写一个装配器会当场拒绝。判据取自数据本身
                  （这一节有没有 role），不按 source 的名字硬编码——按名字猜的话，
                  加一种消息式的 context 就要同时改两处，而漏改的症状是一个
                  选了必炸的选项。 */}
              {item.role !== undefined ? (
                <div className="mb-2 flex items-center gap-1.5">
                  <span className="text-[11px] text-dim">以谁的身份：</span>
                  <Pick<string>
                    value={item.role}
                    options={ROLES}
                    onChange={(role) => edit(at, { role })}
                  />
                </div>
              ) : (
                <p className="mb-2 text-[11px] text-dim">
                  这一节是一串消息，每条自带自己的 role，所以它没有这个设置。
                </p>
              )}
              {item.type === "text" && (
                <Body value={item.body ?? ""} onChange={(body) => edit(at, { body })} />
              )}
              {item.type === "choice" && (
                <Choice item={item} onChange={(patch) => edit(at, patch)} />
              )}
            </div>
          )}
        </article>
      ))}

      {/* **加一节只写 YAML**（S75 那句话的兑现）——现在连 YAML 都不用开。 */}
      <button className="btn mt-2" onClick={add}>
        + 加一条 text
      </button>
    </div>
  );
}

function preview(item: Section): string {
  if (item.type === "context") return `${item.source}（内容由系统生成，位置和 role 可调）`;
  if (item.type === "choice") return `${item.name} · 当前：${item.selected}`;
  return (item.body ?? "").replace(/\s+/g, " ").slice(0, 60);
}

/** `choice`：**每一档的正文 + 当前选哪一档**（R31）。 */
function Choice({
  item,
  onChange,
}: {
  item: Section;
  onChange: (patch: Partial<Section>) => void;
}): JSX.Element {
  const options = item.options ?? {};
  return (
    <>
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] text-dim">当前选：</span>
        <Pick<string>
          value={item.selected ?? ""}
          options={Object.keys(options).map((name) => [name, name])}
          onChange={(selected) => onChange({ selected })}
        />
      </div>
      {Object.entries(options).map(([name, body]) => (
        <div key={name} className="mb-2">
          <div className="mb-0.5 font-mono text-[10px] text-dim">{name}</div>
          <Body
            value={body}
            onChange={(next) => onChange({ options: { ...options, [name]: next } })}
          />
        </div>
      ))}
    </>
  );
}

/** 工具：描述可编，**参数由 Python 生成、只读**（R31 落地 S10 那条担忧）。 */
function Tools({
  doc,
  onChange,
}: {
  doc: PromptDoc;
  onChange: (next: Record<string, unknown>) => void;
}): JSX.Element {
  const tools = (doc.data.tools ?? {}) as Record<string, Record<string, unknown>>;

  function edit(name: string, patch: Record<string, unknown>): void {
    onChange({ ...tools, [name]: { ...tools[name], ...patch } });
  }

  if (!Object.keys(tools).length) {
    return <p className="text-sm text-dim">它没有工具——这是一次干净的调用。</p>;
  }

  return (
    <div className="mx-auto max-w-3xl">
      <p className="mb-3 text-[11px] leading-snug text-dim">
        <b className="font-medium text-fg">关掉的工具不注册</b>
        ——模型根本看不到它，比"注册了但拒绝调用"省一整轮往返。
        <br />
        参数由代码生成、只读：一个工具怎么描述自己跟它的参数是同一件事，
        <b className="font-medium text-fg">并排摆着</b>，漂移就在眼前。
      </p>

      {Object.entries(tools).map(([name, spec]) => (
        <article key={name} className="mb-2 rounded border border-line px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs text-fg">{name}</span>
            <div className="flex-1" />
            {"enabled" in spec && (
              <>
                <span className="text-[11px] text-dim">注册</span>
                <YesNo
                  value={spec.enabled !== false}
                  onChange={(on) => edit(name, { enabled: on })}
                />
              </>
            )}
          </div>

          <p className="mt-1 font-mono text-[10px] leading-relaxed text-dim">
            参数（只读）：{params(doc, name)}
          </p>

          {"options" in spec ? (
            <Choice
              item={{ ...(spec as Section), type: "choice" }}
              onChange={(patch) => edit(name, patch)}
            />
          ) : (
            <div className="mt-1.5">
              <Body
                value={String(spec.body ?? "")}
                onChange={(body) => edit(name, { body })}
              />
            </div>
          )}
        </article>
      ))}
    </div>
  );
}

function params(doc: PromptDoc, tool: string): string {
  const props = doc.params[tool]?.properties ?? {};
  const names = Object.keys(props);
  if (!names.length) return "无";
  return names.map((n) => `${n}: ${props[n]?.type ?? "?"}`).join("，");
}

/**
 * 特殊：运行时**另外追加的整条消息**，以及嵌在渲染里的短句。
 *
 * 后者量不大但每轮都进上下文，其中几条是**明确的操作指令**（"用 X 展开"）——
 * 改了工具名却忘了改它们，模型就会去调一个不存在的工具。
 */
function Specials({
  data,
  onChange,
}: {
  data: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}): JSX.Element {
  const specials = (data.specials ?? {}) as Record<string, Record<string, unknown>>;
  const rendering = (data.rendering ?? {}) as Record<string, string>;

  function editSpecial(key: string, patch: Record<string, unknown>): void {
    onChange({ specials: { ...specials, [key]: { ...specials[key], ...patch } } });
  }

  if (!Object.keys(specials).length && !Object.keys(rendering).length) {
    return <p className="text-sm text-dim">它没有附属提示词，运行时不会往它那儿追加东西。</p>;
  }

  return (
    <div className="mx-auto max-w-3xl">
      {Object.entries(specials).map(([key, spec]) => {
        const sections = (spec.sections ?? []) as Section[];
        return (
          <article key={key} className="mb-2 rounded border border-line px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="text-xs text-fg">{SPECIAL[key] ?? key}</span>
              <span className="font-mono text-[10px] text-dim">{key}</span>
              <div className="flex-1" />
              <span className="text-[11px] text-dim">发送</span>
              <YesNo
                value={spec.enabled !== false}
                onChange={(on) => editSpecial(key, { enabled: on })}
              />
            </div>
            {typeof spec.mark === "string" && (
              <p className="mt-1 text-[10px] text-dim">
                重放时靠 <code className="font-mono text-accent">{spec.mark}</code>{" "}
                认出用户的插话——正文里必须含它，否则保存会被拒。
              </p>
            )}
            <div className="mt-1.5">
              {sections.map((item, at) => (
                <Body
                  key={at}
                  value={item.body ?? ""}
                  onChange={(body) =>
                    editSpecial(key, {
                      sections: sections.map((s, i) => (i === at ? { ...s, body } : s)),
                    })
                  }
                />
              ))}
            </div>
          </article>
        );
      })}

      {Object.keys(rendering).length > 0 && (
        <section className="mt-4">
          <h3 className="panel-title px-0">渲染文案</h3>
          <p className="mb-2 text-[11px] leading-snug text-dim">
            嵌在渲染里的短句。量不大但<b className="font-medium text-fg">每轮都进上下文</b>
            ，而且其中几条是明确的操作指令——改了工具名却忘了改它们，模型会去调一个不存在的工具。
            <br />
            <code className="font-mono text-accent">{"{{…}}"}</code> 是代码填进去的值，别删。
          </p>
          {Object.entries(rendering).map(([key, body]) => (
            <div key={key} className="mb-1.5 flex items-center gap-2">
              <span className="w-36 shrink-0 font-mono text-[10px] text-dim">{key}</span>
              <input
                value={body}
                onChange={(e) => onChange({ rendering: { ...rendering, [key]: e.target.value } })}
                className="min-w-0 flex-1 rounded border border-line bg-ink-800 px-2 py-1 font-mono text-[11px] text-fg outline-none focus:border-dim"
              />
            </div>
          ))}
        </section>
      )}
    </div>
  );
}

const SPECIAL: Record<string, string> = {
  append: "追加输入",
  no_input: "没有新输入时",
  nudge: "空回催促",
  nudge_final: "最后一轮的催促",
  converge: "查得差不多了",
  segment: "归段追问",
};

function Body({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}): JSX.Element {
  return (
    <textarea
      value={value}
      onChange={(e) => onChange(e.target.value)}
      spellCheck={false}
      // 内容多的那几节有两千字，固定高度要么太挤要么全是空白——按行数长。
      rows={Math.min(24, Math.max(2, value.split("\n").length + 1))}
      className="w-full resize-y rounded border border-line bg-ink-800 px-2 py-1.5 text-xs leading-relaxed text-fg outline-none focus:border-dim"
    />
  );
}
