import { useEffect, useState } from "react";

import { api, type AgentCard, type AgentDoc, type Section } from "../api";
import { useSession } from "../store";

/**
 * agent 管理：**所有被输入模型的内容，用户都能审查和修改**（D173）。
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
 * **保存前服务端会试装配一遍**：装不出来就拒绝并说是哪一节坏了——否则改坏一个
 * 字，下一轮生成直接 500，而错误信息离原因很远。
 */
export function AgentsPage(): JSX.Element {
  const page = useSession((s) => s.page);
  const go = useSession((s) => s.go);
  const picked = page.kind === "agents" ? (page.open ?? "director") : "director";

  const [cards, setCards] = useState<AgentCard[]>([]);
  const [doc, setDoc] = useState<AgentDoc | null>(null);
  const [tab, setTab] = useState<"context" | "tools" | "specials">("context");
  const [problem, setProblem] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    void api.listAgents().then(setCards);
  }, []);

  useEffect(() => {
    setDoc(null);
    setProblem("");
    void api.getAgent(picked).then(setDoc);
  }, [picked]);

  // **草稿改在本地，保存才落盘。** 一边拖顺序一边每步都写盘的话，中途那些不
  // 成立的中间状态会被试装配拦下来，用户得到一串莫名其妙的报错。
  const data = doc?.data ?? {};

  function patch(next: Record<string, unknown>): void {
    if (!doc) return;
    setDoc({ ...doc, data: { ...doc.data, ...next } });
    setSaved(false);
  }

  async function save(): Promise<void> {
    if (!doc) return;
    setProblem("");
    try {
      setDoc(await api.putAgent(doc.name, doc.data));
      setSaved(true);
      setCards(await api.listAgents());
    } catch (err) {
      setProblem((err as Error).message);
    }
  }

  return (
    <div className="flex min-h-0 flex-1">
      <nav className="w-40 shrink-0 border-r border-line p-2">
        {cards.map((card) => (
          <button
            key={card.name}
            onClick={() => go({ kind: "agents", open: card.name })}
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
        <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
          <span className="text-sm text-fg">{LABEL[picked] ?? picked}</span>
          <span className="font-mono text-[11px] text-dim">{picked}</span>
          {"enabled" in data && (
            <button
              className={`btn ${data.enabled === false ? "" : "border-accent text-fg"}`}
              onClick={() => patch({ enabled: data.enabled === false })}
              title="关掉这个 agent，它这一路整个不跑"
            >
              {data.enabled === false ? "已关闭" : "启用中"}
            </button>
          )}
          <div className="flex-1" />
          {(["context", "tools", "specials"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`btn ${tab === t ? "border-accent text-fg" : ""}`}
            >
              {TAB[t]}
            </button>
          ))}
          <button className="btn" onClick={() => void save()} disabled={!doc}>
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
  summarizer: "摘要",
};

const TAB = { context: "上下文", tools: "工具", specials: "特殊" };

const KIND: Record<Section["type"], string> = {
  text: "text",
  context: "ctx",
  choice: "choice",
};

/** 上下文：三类条目，**统一可上下拖动排序**（D173）。 */
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
            {item.type !== "context" && (
              <button className="btn" onClick={() => setOpen(open === at ? null : at)}>
                ✎
              </button>
            )}
            <button className="btn" onClick={() => remove(at)} title="删掉这一条">
              ✕
            </button>
          </div>

          {open === at && (
            <div className="border-t border-line p-2">
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

      {/* **加一节只写 YAML**（D135 那句话的兑现）——现在连 YAML 都不用开。 */}
      <button className="btn mt-2" onClick={add}>
        + 加一条 text
      </button>
    </div>
  );
}

function preview(item: Section): string {
  if (item.type === "context") return `${item.source}（内容由系统生成，只有位置可调）`;
  if (item.type === "choice") return `${item.name} · 当前：${item.selected}`;
  return (item.body ?? "").replace(/\s+/g, " ").slice(0, 60);
}

/** `choice`：**每一档的正文 + 当前选哪一档**（D173）。 */
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
        {Object.keys(options).map((name) => (
          <button
            key={name}
            onClick={() => onChange({ selected: name })}
            className={`btn ${item.selected === name ? "border-accent text-fg" : ""}`}
          >
            {name}
          </button>
        ))}
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

/** 工具：描述可编，**参数由 Python 生成、只读**（D173 落地 D140 那条担忧）。 */
function Tools({
  doc,
  onChange,
}: {
  doc: AgentDoc;
  onChange: (next: Record<string, unknown>) => void;
}): JSX.Element {
  const tools = (doc.data.tools ?? {}) as Record<string, Record<string, unknown>>;

  function edit(name: string, patch: Record<string, unknown>): void {
    onChange({ ...tools, [name]: { ...tools[name], ...patch } });
  }

  if (!Object.keys(tools).length) {
    return <p className="text-sm text-dim">这个 agent 没有工具——它是一次干净的调用。</p>;
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
              <button
                className={`btn ${spec.enabled === false ? "" : "border-accent text-fg"}`}
                onClick={() => edit(name, { enabled: spec.enabled === false })}
              >
                {spec.enabled === false ? "已关闭" : "启用中"}
              </button>
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

function params(doc: AgentDoc, tool: string): string {
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
    return <p className="text-sm text-dim">这个 agent 没有附属提示词，运行时不会往它那儿追加东西。</p>;
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
              <button
                className={`btn ${spec.enabled === false ? "" : "border-accent text-fg"}`}
                onClick={() => editSpecial(key, { enabled: spec.enabled === false })}
              >
                {spec.enabled === false ? "已关闭" : "启用中"}
              </button>
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
