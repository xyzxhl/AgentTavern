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

interface TraceCache {
  events: TraceEvent[];
  nextBefore: number | null;
  hasMore: boolean;
}

const TRACE_PAGE_SIZE = 8;

/** 旧页在文件里更早，必须前插；游标保证两页不重叠。 */
export function mergeOlderTracePage(current: TraceEvent[], older: TraceEvent[]): TraceEvent[] {
  return [...older, ...current];
}

/** 展开长字段时一次只多渲染一块，避免把整段一次性交给 DOM。 */
export function nextVisibleChars(current: number, total: number, chunk = 4_000): number {
  return Math.min(total, current + chunk);
}

function exchangeKey(exchange: Exchange): string {
  return `${exchange.actor}:${exchange.seq}`;
}

/**
 * 开发者面板：**按 agent 展开每一次模型往返的完整输入输出**（S113 / S115）。
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
  const [fetched, setFetched] = useState<Record<string, TraceCache>>({});
  const [loading, setLoading] = useState<Record<string, boolean>>({});
  const [picked, setPicked] = useState<string | null>(null);

  // **推过来的优先，落盘的兜底**：当前连接期间的轮次内存里就有，更早的要去读
  // 文件——事件是推过来的，刷新一次页面之前那些就没了（S115）。
  const runs = path.map((n) => n.id ?? "").filter(Boolean);
  const liveEvents = runId ? traces[runId] : undefined;
  const events = runId
    ? liveEvents?.length
      ? liveEvents
      : (fetched[runId]?.events ?? [])
    : [];

  useEffect(() => {
    if (!sessionId || !runId || traces[runId]?.length || fetched[runId] || loading[runId]) return;
    setLoading((was) => ({ ...was, [runId]: true }));
    void api
      .readTracePage(sessionId, runId, TRACE_PAGE_SIZE)
      .then((got) => {
        setFetched((was) => ({
          ...was,
          [runId]: {
            events: got.events,
            nextBefore: got.next_before ?? null,
            hasMore: got.has_more ?? false,
          },
        }));
      })
      .catch((error: unknown) => console.error("读取 trace 失败", error))
      .finally(() => setLoading((was) => ({ ...was, [runId]: false })));
  }, [sessionId, runId, traces, fetched, loading]);

  useEffect(() => {
    setPicked(null);
  }, [runId]);

  async function loadOlder(): Promise<void> {
    if (!sessionId || !runId || traces[runId]?.length || loading[runId]) return;
    const current = fetched[runId];
    if (!current?.hasMore || current.nextBefore === null) return;

    setLoading((was) => ({ ...was, [runId]: true }));
    try {
      const got = await api.readTracePage(
        sessionId,
        runId,
        TRACE_PAGE_SIZE,
        current.nextBefore,
      );
      setFetched((was) => {
        const latest = was[runId] ?? current;
        return {
          ...was,
          [runId]: {
            events: mergeOlderTracePage(latest.events, got.events),
            nextBefore: got.next_before ?? null,
            hasMore: got.has_more ?? false,
          },
        };
      });
    } catch (error) {
      console.error("读取更早 trace 失败", error);
    } finally {
      setLoading((was) => ({ ...was, [runId]: false }));
    }
  }

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

  const selected = picked ?? (exchanges[0] ? exchangeKey(exchanges[0]) : null);
  const current = exchanges.find((exchange) => exchangeKey(exchange) === selected);

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

        <div
          className="w-44 shrink-0 overflow-y-auto border-r border-line p-2"
          onScroll={(event) => {
            if (event.currentTarget.scrollTop <= 24) void loadOlder();
          }}
          onWheel={(event) => {
            if (event.deltaY < 0 && event.currentTarget.scrollTop <= 24) void loadOlder();
          }}
        >
          <div className="panel-title">往返</div>
          {runId && fetched[runId]?.hasMore && !traces[runId]?.length && (
            <button
              className="mb-1 w-full rounded border border-line px-2 py-1 text-[10px] text-dim hover:text-fg"
              onClick={() => void loadOlder()}
              disabled={loading[runId]}
            >
              {loading[runId] ? "加载中…" : "加载更早记录"}
            </button>
          )}
          {runId && exchanges.length === 0 && (
            <p className="px-1 text-[11px] leading-snug text-dim">
              这一轮没有记录。服务不是以开发者模式启动的，或者那时还没开。
            </p>
          )}
          {exchanges.map((ex, i) => (
            <button
              key={`${ex.seq}-${i}`}
              onClick={() => setPicked(exchangeKey(ex))}
              className={`mb-0.5 block w-full rounded px-2 py-1 text-left text-[11px] transition-colors ${
                exchangeKey(ex) === selected
                  ? "bg-ink-600 text-fg"
                  : "text-dim hover:bg-ink-700 hover:text-fg"
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
 * 一条消息。**内容与思考段各有各的展开开关**（S128）。
 *
 * 合用一个不行：思考段动辄上万字（实测 director 一轮的思考段有两万多个分片），
 * 而它和正文是**两件不同的东西**——查"它写了什么"和查"它为什么这么写"是两次
 * 不同的排查，一次只想看一个。合在一起的话，思考段会把内容整个挤出视野。
 *
 * 两者都**默认折起**：一份 director 的 prompt 动辄几万字，全摊开之后想找的
 * 那一条就淹了。
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
  return (
    <div className="rounded border border-line">
      <div className="flex items-baseline gap-2 border-b border-line px-2.5 py-1">
        <span className="font-mono text-[10px] uppercase text-accent/80">{role}</span>
        {body.length > 0 && <span className="text-[10px] text-dim">{body.length} 字</span>}
      </div>

      {reasoning && <Foldable label="思考" text={reasoning} dim />}
      {body && <Foldable label="内容" text={body} />}

      {calls?.map((call, i) => (
        <Foldable
          key={i}
          label={`工具参数 · ${call.name ?? "?"}`}
          text={pretty(call.arguments_raw ?? "")}
        />
      ))}
    </div>
  );
}

const FOLD_AT = 400;
const OPEN_AT = 4_000;

/** 一段可折的长文本。短到一屏放得下就不折——为几行字加一个开关只是噪音。 */
function Foldable({
  label,
  text,
  dim,
}: {
  label: string;
  text: string;
  dim?: boolean;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const [visible, setVisible] = useState(FOLD_AT);
  const long = text.length > FOLD_AT;
  const shown = !long ? text : text.slice(0, open ? visible : FOLD_AT);
  const hasHidden = open && visible < text.length;

  useEffect(() => {
    setOpen(false);
    setVisible(FOLD_AT);
  }, [text]);

  function toggle(): void {
    if (open) {
      setOpen(false);
      return;
    }
    setVisible(Math.min(text.length, OPEN_AT));
    setOpen(true);
  }

  return (
    <div className="border-t border-line first:border-t-0">
      {long && (
        <button
          onClick={toggle}
          className="flex w-full items-baseline gap-1.5 px-2.5 pt-1.5 text-[10px] text-dim transition-colors hover:text-fg"
        >
          <span className="select-none">{open ? "▾" : "▸"}</span>
          <span>
            {label} {text.length} 字
          </span>
        </button>
      )}
      <div
        className={open && long ? "max-h-[28rem] overflow-y-auto" : ""}
        onScroll={(event) => {
          const box = event.currentTarget;
          if (hasHidden && box.scrollHeight - box.scrollTop - box.clientHeight < 96) {
            setVisible((was) => nextVisibleChars(was, text.length));
          }
        }}
      >
        <pre
          className={`whitespace-pre-wrap break-words px-2.5 py-1.5 font-mono text-[11px] leading-relaxed ${
            dim ? "text-dim/70" : "text-fg"
          }`}
        >
          {shown}
          {(!open || hasHidden) && long && <span className="text-dim">…</span>}
        </pre>
        {hasHidden && (
          <button
            className="w-full border-t border-line px-2.5 py-1 text-left text-[10px] text-dim hover:text-fg"
            onClick={() => setVisible((was) => nextVisibleChars(was, text.length))}
          >
            继续展开
          </button>
        )}
      </div>
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
