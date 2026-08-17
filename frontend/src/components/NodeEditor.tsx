import { useEffect, useState } from "react";

import { api } from "../api";
import type { Node, StateDelta } from "../protocol";
import { useSession } from "../store";

/**
 * 直接改一轮的产出（D65）。**细纲 / 正文 / 状态摆在同一处**——它们是同一个节点
 * 的三个面，分到三个地方去改，用户就得先知道"这一轮"是由这三样组成的。
 *
 * 改完在 trace 里留一条 `USER_EDIT`（D50）：手改过的正文不能在回看时被当成
 * 模型的产出去归因。
 *
 * **改的只是这一轮，下游照常重放。** 那不是危险，那就是 event-sourced 的正常
 * 行为（D48/D49）——所以界面上不写"改历史会连锁"之类的警告，那只会让用户不敢
 * 用一个本来正常的功能。
 */
export function NodeEditor({ node }: { node: Node }): JSX.Element {
  const editNode = useSession((s) => s.editNode);
  const edit = useSession((s) => s.edit);
  const sessionId = useSession((s) => s.sessionId);

  const [outline, setOutline] = useState(node.outline ?? "");
  const [prose, setProse] = useState(node.prose ?? "");

  // 状态那部分是两个视图，不是两个概念（见 `StateEditor`）。
  const [view, setView] = useState<"delta" | "result">("delta");
  const [delta, setDelta] = useState(() => pretty(node.state_delta ?? {}));
  const [result, setResult] = useState<string | null>(null);
  const [problem, setProblem] = useState("");

  // 「结果」视图的起点得现拉：**它不是侧栏里的当前状态**——侧栏显示的是路径
  // 末端，而这里要的是这一轮跑完时的那一份，改历史某一轮时两者不是一个东西。
  useEffect(() => {
    if (view !== "result" || result !== null || !sessionId || !node.id) return;
    void api
      .stateAtNode(sessionId, node.id)
      .then((at) => setResult(pretty(at.values)))
      .catch((err: Error) => setProblem(err.message));
  }, [view, result, sessionId, node.id]);

  function save(): void {
    const patch: {
      nodeId: string;
      outline?: string;
      prose?: string;
      state_delta?: StateDelta;
      state_result?: Record<string, unknown>;
    } = { nodeId: node.id ?? "" };

    if (outline !== (node.outline ?? "")) patch.outline = outline;
    if (prose !== (node.prose ?? "")) patch.prose = prose;

    // **只发当前这个视图。** 两个一起发服务端会拒（它不知道该听谁的），而那
    // 恰恰是对的：另一个视图里显示的是编辑前的样子，一起发等于把它覆盖回去。
    try {
      if (view === "delta") {
        const parsed = JSON.parse(delta) as StateDelta;
        if (delta !== pretty(node.state_delta ?? {})) patch.state_delta = parsed;
      } else if (result !== null) {
        patch.state_result = JSON.parse(result) as Record<string, unknown>;
      }
    } catch (err) {
      setProblem(`状态不是合法的 JSON：${(err as Error).message}`);
      return;
    }

    editNode(patch);
  }

  return (
    <div className="mt-4 rounded border border-line bg-ink-800 p-3">
      <Field label="细纲" hint="director 写的，writer 照着它写正文">
        <textarea
          className="h-32 w-full resize-y rounded border border-line bg-ink-900 px-2 py-1.5 text-xs leading-relaxed text-fg outline-none focus:border-dim"
          value={outline}
          onChange={(e) => setOutline(e.target.value)}
        />
      </Field>

      <Field label="正文">
        <textarea
          className="h-40 w-full resize-y rounded border border-line bg-ink-900 px-2 py-1.5 font-prose text-[13px] leading-[1.8] text-fg outline-none focus:border-dim"
          value={prose}
          onChange={(e) => setProse(e.target.value)}
        />
      </Field>

      <div className="mb-1.5 mt-3 flex items-baseline gap-2">
        <span className="panel-title">状态</span>
        <div className="flex-1" />
        <Toggle now={view} pick={setView} />
      </div>

      {view === "delta" ? (
        <>
          <p className="mb-1 text-[10px] leading-snug text-dim">
            这一轮对状态下的一串指令。<Code>set</Code> 覆盖（值为 <Code>null</Code>{" "}
            是删掉这个键）、<Code>append</Code> 往清单里加、<Code>remove</Code> 从清单里减。
          </p>
          <textarea
            className="h-44 w-full resize-y rounded border border-line bg-ink-900 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-fg outline-none focus:border-dim"
            value={delta}
            onChange={(e) => setDelta(e.target.value)}
            spellCheck={false}
          />
        </>
      ) : (
        <>
          <p className="mb-1 text-[10px] leading-snug text-dim">
            这一轮<b className="font-medium text-fg">跑完之后</b>
            世界是什么样。保存时和上一轮求差、反推成指令写回去——落盘的仍然只有指令。
          </p>
          <textarea
            className="h-44 w-full resize-y rounded border border-line bg-ink-900 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-fg outline-none focus:border-dim"
            value={result ?? "读取中……"}
            onChange={(e) => setResult(e.target.value)}
            disabled={result === null}
            spellCheck={false}
          />
        </>
      )}

      {problem && <p className="mt-1.5 text-[11px] text-accent">{problem}</p>}

      <div className="mt-3 flex items-center gap-2">
        <button className="btn" onClick={save}>
          保存
        </button>
        <button className="btn" onClick={() => edit(null)}>
          取消
        </button>
        <span className="text-[10px] text-dim">
          只改这一轮；后面几轮照常重放在它上面。
        </span>
      </div>
    </div>
  );
}

/** 同一个编辑器的两个视图，**不是两个概念**（D65）。 */
function Toggle({
  now,
  pick,
}: {
  now: "delta" | "result";
  pick: (v: "delta" | "result") => void;
}): JSX.Element {
  return (
    <div className="flex overflow-hidden rounded border border-line text-[11px]">
      {(["delta", "result"] as const).map((v) => (
        <button
          key={v}
          onClick={() => pick(v)}
          className={`px-2 py-0.5 transition-colors ${
            now === v ? "bg-ink-600 text-fg" : "text-dim hover:text-fg"
          }`}
        >
          {v === "delta" ? "指令" : "结果"}
        </button>
      ))}
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
    <div className="mb-3">
      <div className="mb-1 flex items-baseline gap-2">
        <span className="panel-title">{label}</span>
        {hint && <span className="text-[10px] text-dim">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

function Code({ children }: { children: string }): JSX.Element {
  return <code className="font-mono text-accent">{children}</code>;
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
