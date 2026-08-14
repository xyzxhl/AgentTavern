// REST 与 WebSocket 的薄封装。**只做传输，不做状态**——状态在 store.ts。

import type { ClientMessage, Node, ServerMessage, Session, WorldbookEntry } from "./protocol";

export interface ServerMeta {
  developer: boolean;
  model: string;
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
  getSession: (id: string) =>
    json<{ session: Session; path: Node[]; state: Record<string, unknown> }>(`/sessions/${id}`),

  // -- worldbook：session 的起点（D90）--------------------------------------
  //
  // 条目的 `content` 进 worldinfo（静态设定），`seeds` 进初始 state（会变的
  // 部分）。**初始 state 是现算的**，所以改这里会影响每一个分支。
  listWorldbook: (id: string) => json<WorldbookEntry[]>(`/sessions/${id}/worldinfo`),
  saveWorldbookEntry: (id: string, entry: WorldbookEntry) =>
    json<WorldbookEntry>(`/sessions/${id}/worldinfo`, {
      method: "PUT",
      body: JSON.stringify(entry),
    }),
  deleteWorldbookEntry: (id: string, entryId: string) =>
    json<{ deleted: string }>(`/sessions/${id}/worldinfo/${entryId}`, { method: "DELETE" }),
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
