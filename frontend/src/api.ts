// REST 与 WebSocket 的薄封装。**只做传输，不做状态**——状态在 store.ts。

import type {
  ClientMessage,
  Node,
  ServerMessage,
  Session,
  TraceEvent,
  WorldbookBundle,
  WorldbookEntry,
} from "./protocol";

export interface ServerMeta {
  developer: boolean;
  model: string;
  /** 各控制维度有哪些档。**从提示词 YAML 现读**，前端不写死一份。 */
  options: { prose_style: string[]; pov: string[] };
}

async function json<T>(input: string, init?: RequestInit): Promise<T> {
  const res = await fetch(input, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return (await res.json()) as T;
}

export const api = {
  meta: () => json<ServerMeta>("/meta"),
  listSessions: () => json<string[]>("/sessions"),
  createSession: (session?: Partial<Session>) =>
    json<Session>("/sessions", { method: "POST", body: JSON.stringify(session ?? null) }),
  patchSession: (id: string, patch: Record<string, unknown>) =>
    json<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  getSession: (id: string) =>
    json<{ session: Session; path: Node[]; state: Record<string, unknown> }>(`/sessions/${id}`),

  // -- worldbook：session 的起点（D90 / D158）---------------------------------
  //
  // 条目分两类：`lore` 的 `content` 进 worldinfo（静态设定），`state` 的 `values`
  // 进初始状态（会变的部分）。**初始状态在故事开始时冻结**——开始之前改立刻生效，
  // 之后改只影响以后新建的会话。
  listWorldbook: (id: string) => json<WorldbookEntry[]>(`/sessions/${id}/worldinfo`),
  saveWorldbookEntry: (id: string, entry: WorldbookEntry) =>
    json<WorldbookEntry>(`/sessions/${id}/worldinfo`, {
      method: "PUT",
      body: JSON.stringify(entry),
    }),
  deleteWorldbookEntry: (id: string, entryId: string) =>
    json<{ deleted: string }>(`/sessions/${id}/worldinfo/${entryId}`, { method: "DELETE" }),

  /** 回看某一轮落盘的 trace（D162）。开发者面板靠它看当前连接之前的轮次。 */
  readTrace: (id: string, runId: string) =>
    json<TraceEvent[]>(`/sessions/${id}/trace/${runId}`),

  // 导入导出。格式与三条约定见 docs/design/WorldbookFormat.md。
  exportWorldbook: (id: string) => json<WorldbookBundle>(`/sessions/${id}/worldbook/export`),
  importWorldbook: (id: string, bundle: WorldbookBundle, replace: boolean) =>
    json<{ imported: number; removed: number; frozen: boolean }>(
      `/sessions/${id}/worldbook/import?replace=${replace}`,
      { method: "POST", body: JSON.stringify(bundle) },
    ),
};

/**
 * 一条 session 的 WebSocket。
 *
 * **不自动重连。** 重连之后 UI 会拿到一份和服务端不一致的状态——服务端那侧
 * 正在跑的 run 还在跑，而前端已经忘了自己发过什么。断了就明说，由用户决定
 * 是否刷新，这比悄悄接上再显示错的东西好。
 */
export class SessionSocket {
  private ws: WebSocket | null = null;

  constructor(
    private readonly sessionId: string,
    private readonly onMessage: (message: ServerMessage) => void,
    private readonly onClosed: (reason: string) => void,
  ) {}

  open(): void {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${scheme}://${location.host}/sessions/${this.sessionId}/ws`);
    this.ws = ws;

    ws.onmessage = (event) => {
      // 解析失败不能拖垮整条连接——一条坏消息只是一条坏消息。
      try {
        this.onMessage(JSON.parse(event.data as string) as ServerMessage);
      } catch (err) {
        console.error("无法解析的服务端消息", event.data, err);
      }
    };
    ws.onerror = () => this.onClosed("连接出错");
    ws.onclose = (event) => this.onClosed(event.wasClean ? "连接已关闭" : "连接断开");
  }

  send(message: ClientMessage): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }

  close(): void {
    // 主动关闭时不该再触发"连接断开"的提示。
    if (!this.ws) return;
    this.ws.onclose = null;
    this.ws.close();
    this.ws = null;
  }
}
