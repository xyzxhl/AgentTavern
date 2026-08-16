// REST 与 WebSocket 的薄封装。**只做传输，不做状态**——状态在 store.ts。

import type {
  ClientMessage,
  Node,
  ServerMessage,
  Session,
  TraceEvent,
  Worldbook,
  WorldbookBundle,
  WorldbookEntry,
} from "./protocol";

/** 列表里的一份设定：**带上「谁在用」**——删之前必须看得见这件事（D170）。 */
export interface WorldbookCard {
  worldbook: Worldbook;
  entries: number;
  sessions: string[];
}

/** 会话列表里的一条。**带标题**——一串 `sess_a1b2c3…` 认不出哪局是哪局（D172）。 */
export interface SessionCard {
  id: string;
  title: string;
  worldbook_id: string;
  worldbook_name: string;
}

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
  // **状态码 200 不等于拿到了 JSON**（D172）。开发时漏配一条代理前缀，Vite 会
  // 把请求当成前端路由、回一份 `index.html` 且状态码 200——于是异常一路拖到
  // `res.json()` 才发生，而那时的报错（`Unexpected token '<'`）离原因很远。
  // 在这儿认出来，说得出是哪个地址。
  const kind = res.headers.get("content-type") ?? "";
  if (!kind.includes("json")) {
    throw new Error(`${input} 没有返回 JSON（content-type: ${kind || "无"}）——检查代理配置`);
  }
  return (await res.json()) as T;
}

export const api = {
  meta: () => json<ServerMeta>("/meta"),
  listSessions: () => json<SessionCard[]>("/sessions"),
  createSession: (session?: Partial<Session>) =>
    json<Session>("/sessions", { method: "POST", body: JSON.stringify(session ?? null) }),
  patchSession: (id: string, patch: Record<string, unknown>) =>
    json<Session>(`/sessions/${id}`, { method: "PATCH", body: JSON.stringify(patch) }),
  getSession: (id: string) =>
    json<{ session: Session; path: Node[]; state: Record<string, unknown> }>(`/sessions/${id}`),

  // -- worldbook：**一级资产，不属于任何 session**（D170）--------------------
  //
  // 一份设定能开出好几个故事，所以它有自己的管理界面，不挂在某个会话下面。
  // 条目分三类：`world` / `character` 的 `content` 进 worldinfo（静态设定），
  // `state` 的 `values` 进初始状态（会变的部分）。
  //
  // **改设定立刻对所有故事生效**，那是对的——这些是"这个世界是什么"，改它等于
  // 改角色卡。已经发生过的剧情所依赖的事实是另一回事：初始状态在**每局故事
  // 各自的第一轮**冻结（D158），此后改它只影响还没开始的那些。
  listWorldbooks: () => json<WorldbookCard[]>("/worldbooks"),
  createWorldbook: (book?: Partial<Worldbook>) =>
    json<Worldbook>("/worldbooks", { method: "POST", body: JSON.stringify(book ?? null) }),
  /** 导入一份文件 → **得到一份新设定**。拿到别人分享的文件时要的是这个，
   * 而不是"先建个空的再往里导"（D172）。 */
  importAsNewWorldbook: (bundle: WorldbookBundle) =>
    json<Worldbook>("/worldbooks/import", { method: "POST", body: JSON.stringify(bundle) }),
  getWorldbook: (id: string) =>
    json<{
      worldbook: Worldbook;
      entries: WorldbookEntry[];
      sessions: string[];
      initial_state: Record<string, unknown>;
    }>(`/worldbooks/${id}`),
  /** **只能改名**——worldbook 上没有别的字段了（D175），题材与尺度都是条目。 */
  patchWorldbook: (id: string, name: string) =>
    json<Worldbook>(`/worldbooks/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  /** 有故事开在上面时服务端返回 409——删掉的话那几局会当场失忆。 */
  deleteWorldbook: (id: string) =>
    json<{ deleted: string }>(`/worldbooks/${id}`, { method: "DELETE" }),

  saveEntry: (id: string, entry: WorldbookEntry) =>
    json<WorldbookEntry>(`/worldbooks/${id}/entries`, { method: "PUT", body: JSON.stringify(entry) }),
  deleteEntry: (id: string, entryId: string) =>
    json<{ deleted: string }>(`/worldbooks/${id}/entries/${entryId}`, { method: "DELETE" }),

  /** 回看某一轮落盘的 trace（D162）。开发者面板靠它看当前连接之前的轮次。 */
  readTrace: (id: string, runId: string) => json<TraceEvent[]>(`/sessions/${id}/trace/${runId}`),

  // 导入导出。格式与三条约定见 docs/design/WorldbookFormat.md。
  exportWorldbook: (id: string) => json<WorldbookBundle>(`/worldbooks/${id}/export`),
  importWorldbook: (id: string, bundle: WorldbookBundle, replace: boolean) =>
    json<{ imported: number; removed: number; frozen: string[] }>(
      `/worldbooks/${id}/import?replace=${replace}`,
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
