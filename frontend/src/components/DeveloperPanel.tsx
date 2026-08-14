import { useEffect, useState } from "react";

import { api } from "../api";
import type { TraceEvent } from "../protocol";
import { useSession } from "../store";

const ACTOR_LABEL: Record<string, string> = {
  director: "推演",
  writer: "成文",
  state_manager: "状态",
  goal_manager: "目标",
  orchestrator: "编排",
};

interface Exchange {
  actor: string;
  seq: number;
  messages: WireMessage[];
  tools: string[];
  response: WireResponse;
}

interface WireMessage {
  role?: string;
  content?: string | null;
  reasoning_content?: string | null;
  tool_call_id?: string | null;
  tool_calls?: { name?: string; arguments_raw?: string }[];
}

interface WireResponse {
  content?: string;
  reasoning_content?: string | null;
  finish_reason?: string;
  tool_calls?: { name?: string; arguments_raw?: string }[];
}

/**
 * 开发者面板：**按 agent 展开每一次模型往返的完整输入输出**（D156 / D162）。
 *
 * 别处的 trace 都是投影——`context_assembled` 只有条数和字数，`model_response`
 * 只有工具名。投影够回答「它做了什么」，回答不了「它为什么这么做」，而调 prompt
 * 时要看的恰恰是后者。
 *
 * 只在服务以开发者模式启动时出现（`/meta` 的 `developer`）。
 */
export function DeveloperPanel({ onClose }: { onClose: () => void }): JSX.Element {
  const sessionId = useSession((s) => s.sessionId);
  const path = useSession((s) => s.path);
  const traces = useSession((s) => s.traces);

  const [runId, setRunId] = useState<string | null>(null);
  const [fetched, setFetched] = useState<Record<string, TraceEvent[]>>({});
  const [picked, setPicked] = useState(0);

  // **推过来的优先，落盘的兜底**：当前连接期间的轮次内存里就有，更早的要去读
  // 文件——事件是推过来的，刷新一次页面之前那些就没了（D162）。
  const runs = path.map((n) => n.id ?? "").filter(Boolean);
  const events = runId ? (traces[runId]?.length ? traces[runId] : (fetched[runId] ?? [])) : [];

  useEffect(() => {
    if (!sessionId || !runId || traces[runId]?.length || fetched[runId]) return;
    void api.readTrace(sessionId, runId).then((got) => {
      setFetched((was) => ({ ...was, [runId]: got }));
    });
  }, [sessionId, runId, traces, fetched]);

  useEffect(() => {
    setPicked(0);
  }, [runId]);

  const exchanges: Exchange[] = events
    .filter((e) => e.type === "model_request")
    .map((e) => {
      const payload = (e.payload ?? {}) as Record<string, unknown>;
      return {
        actor: e.actor,
        seq: e.seq,
        messages: (payload.messages ?? []) as WireMessage[],
        tools: (payload.tools ?? []) as string[],
        response: (payload.response ?? {}) as WireResponse,
      };
    });

  const current = exchanges[picked];

  return (
    <div className="absolute inset-0 z-10 flex flex-col bg-ink-900">
      <header className="flex items-center gap-3 border-b border-line px-5 py-2.5">
        <span className="text-sm text-fg">开发者</span>
        <span className="text-[11px] text-dim">每次模型往返的完整输入输出</span>
        <div className="flex-1" />
        <button className="btn" onClick={onClose}>
          关闭
        </button>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="w-52 shrink-0 overflow-y-auto border-r border-line p-2">
          <div className="panel-title">轮次</div>
          {runs.length === 0 && <p className="px-1 text-xs text-dim">还没有轮次。</p>}
          {runs.map((id, i) => (
            <button
              key={id}
              onClick={() => setRunId(id)}
              className={`mb-0.5 block w-full truncate rounded px-2 py-1 text-left font-mono text-[11px] transition-colors ${
                id === runId ? "bg-ink-600 text-fg" : "text-dim hover:bg-ink-700 hover:text-fg"
              }`}
            >
              第 {i + 1} 轮
            </button>
          ))}
        </div>

        <div className="w-44 shrink-0 overflow-y-auto border-r border-line p-2">
          <div className="panel-title">往返</div>
          {runId && exchanges.length === 0 && (
            <p className="px-1 text-[11px] leading-snug text-dim">
              这一轮没有记录。服务不是以开发者模式启动的，或者那时还没开。
            </p>
          )}
          {exchanges.map((ex, i) => (
            <button
              key={`${ex.seq}-${i}`}
              onClick={() => setPicked(i)}
              className={`mb-0.5 block w-full rounded px-2 py-1 text-left text-[11px] transition-colors ${
                i === picked ? "bg-ink-600 text-fg" : "text-dim hover:bg-ink-700 hover:text-fg"
              }`}
            >
              {i + 1}. {ACTOR_LABEL[ex.actor] ?? ex.actor}
              <span className="ml-1 text-dim">{ex.messages.length} 条</span>
            </button>
          ))}
        </div>

        <div className="min-w-0 flex-1 overflow-y-auto px-5 py-4">
          {current ? <ExchangeView exchange={current} /> : <Empty />}
        </div>
      </div>
    </div>
  );
}

function Empty(): JSX.Element {
  return <div className="grid h-full place-items-center text-sm text-dim">左侧选一轮</div>;
}

function ExchangeView({ exchange }: { exchange: Exchange }): JSX.Element {
  const chars = exchange.messages.reduce((n, m) => n + (m.content?.length ?? 0), 0);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline gap-3 text-[11px] text-dim">
        <span className="text-fg">{ACTOR_LABEL[exchange.actor] ?? exchange.actor}</span>
        <span>{exchange.messages.length} 条消息</span>
        <span>{chars} 字</span>
        {exchange.tools.length > 0 && <span>工具：{exchange.tools.join("、")}</span>}
      </div>

      {exchange.messages.map((m, i) => (
        <Block
          key={i}
          role={m.role ?? "?"}
          body={m.content ?? ""}
          reasoning={m.reasoning_content ?? ""}
          calls={m.tool_calls}
        />
      ))}

      <div className="border-t border-line pt-3">
        <div className="mb-1.5 text-[11px] uppercase tracking-wider text-accent">
          返回{exchange.response.finish_reason ? ` · ${exchange.response.finish_reason}` : ""}
        </div>
        <Block
          role="assistant"
          body={exchange.response.content ?? ""}
          reasoning={exchange.response.reasoning_content ?? ""}
          calls={exchange.response.tool_calls}
        />
      </div>
    </div>
  );
}

/**
 * 一条消息。**默认折起长内容**——一份 director 的 prompt 动辄几万字，全摊开
 * 之后想找的那一条就淹了。
 */
function Block({
  role,
  body,
  reasoning,
  calls,
}: {
  role: string;
  body: string;
  reasoning?: string;
  calls?: { name?: string; arguments_raw?: string }[];
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const long = body.length > 400;
  const shown = open || !long ? body : body.slice(0, 400);

  return (
    <div className="rounded border border-line">
      <div className="flex items-baseline gap-2 border-b border-line px-2.5 py-1">
        <span className="font-mono text-[10px] uppercase text-accent/80">{role}</span>
        {body.length > 0 && <span className="text-[10px] text-dim">{body.length} 字</span>}
        {long && (
          <button onClick={() => setOpen(!open)} className="ml-auto text-[10px] text-dim hover:text-fg">
            {open ? "收起" : "展开"}
          </button>
        )}
      </div>

      {reasoning && (
        <pre className="whitespace-pre-wrap break-words border-b border-line px-2.5 py-1.5 font-mono text-[11px] leading-relaxed text-dim/70">
          {reasoning}
        </pre>
      )}

      {shown && (
        <pre className="whitespace-pre-wrap break-words px-2.5 py-1.5 font-mono text-[11px] leading-relaxed text-fg">
          {shown}
          {!open && long && <span className="text-dim">…</span>}
        </pre>
      )}

      {calls?.map((call, i) => (
        <div key={i} className="border-t border-line px-2.5 py-1.5">
          <div className="font-mono text-[11px] text-accent">{call.name}</div>
          <pre className="mt-0.5 whitespace-pre-wrap break-words font-mono text-[11px] text-fg">
            {pretty(call.arguments_raw ?? "")}
          </pre>
        </div>
      ))}
    </div>
  );
}

/** 工具参数是 JSON 字符串。**解析失败就原样给**——模型时常输出不合法的 JSON，
 * 而那种时候原文正是要看的东西。 */
function pretty(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}
