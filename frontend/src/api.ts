// REST 与 WebSocket 的薄封装。**只做传输，不做状态**——状态在 store.ts。

import type {
  ClientMessage,
  Corpus,
  Node,
  ServerMessage,
  Session,
  TraceEvent,
  Worldbook,
  WorldbookBundle,
  WorldbookEntry,
} from "./protocol";

/** 列表里的一份资料：**带上「谁在用」**——删之前必须看得见这件事（S136）。 */
export interface CorpusCard {
  corpus: Corpus;
  sessions: string[];
}

/** 文件夹和知识库对不上的一处（S151）。**这是待办，不是已经发生的事**——
 * 更新与否由用户按下才发生。 */
export interface PendingChange {
  kind: "new" | "modified" | "renamed" | "deleted" | "unsupported";
  source_name: string;
  /** `new` 与 `unsupported` 时为空——那时还没有对应的资料。 */
  corpus_id: string;
  /** `unsupported` 时说清为什么。 */
  reason: string;
  /** `deleted` 时：哪几局故事挂着它。**按下之前就要看到后果**（S136）。 */
  sessions: string[];
}

export interface CorpusLibrary {
  corpora: CorpusCard[];
  pending: PendingChange[];
  /** 源文件夹的绝对路径。**位置固定不给配**（S149），所以这里只是告诉用户它在哪。 */
  folder: string;
  broken: string[];
}

/** 列表里的一份设定：**带上「谁在用」**——删之前必须看得见这件事（S63）。 */
export interface WorldbookCard {
  worldbook: Worldbook;
  entries: number;
  sessions: string[];
  /** 读不出来的那几条（S73）。**一条坏数据不该让整张列表 500**，但也不能
   * 悄悄跳过——用户得知道自己丢了什么。 */
  broken: string[];
}

/** 会话列表里的一条。**带标题**——一串 `sess_a1b2c3…` 认不出哪局是哪局（R17）。 */
export interface SessionCard {
  id: string;
  title: string;
  worldbook_id: string;
  worldbook_name: string;
}

export interface ServerMeta {
  developer: boolean;
  model: string;
  /** 边生成边推。**服务级**（S80）：关于交付，不关于能力，所以它没跟着配置
   * 一起上调到提示词层。在 `.env` 里定，运行时只读。 */
  stream: boolean;
}

/** 提示词列表里的一条。数字只为让列表有点信息量，编辑要进详情。 */
export interface PromptCard {
  name: string;
  /** `agent`（一个角色，名词命名）还是 `call`（一次单次调用，动词命名）——S147。
   *
   * **两类并列，不是主从。** 由后端给，不在前端按名字猜：加一份 YAML 就要
   * 同时改两处的话，漏改的症状是它悄悄出现在错误的那一类里。
   */
  group: PromptGroup;
  enabled: boolean;
  sections: number;
  tools: number;
  specials: number;
}

/** 提示词分两类（S147）：agent 是**一个角色**，call 是**一次动作**。 */
export type PromptGroup = "agent" | "call";

/** 一份提示词配置，加上**由 Python 生成的参数 schema**（只读）。
 *
 * 参数不搬进 YAML 是 S10 那条担忧的落地方式：「描述跟参数是同一件事，拆开
 * 必然对不上」——那个风险是真的，所以让它们**并排显示**，漂移摆在人眼前。
 */
export interface PromptDoc {
  name: string;
  data: Record<string, unknown>;
  params: Record<string, { properties?: Record<string, { description?: string; type?: string }> }>;
}

/** 一条提示词条目。**类型是显式字段**（R31）——拖动排序要先读出它是什么。 */
export interface Section {
  type: "text" | "context" | "choice";
  role?: string;
  body?: string;
  source?: string;
  name?: string;
  selected?: string;
  options?: Record<string, string>;
  [k: string]: unknown;
}

async function json<T>(input: string, init?: RequestInit): Promise<T> {
  const res = await fetch(input, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  // **状态码 200 不等于拿到了 JSON**（R17）。开发时漏配一条代理前缀，Vite 会
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
    json<{
      session: Session;
      path: Node[];
      state: Record<string, unknown>;
      /** 路径上**有兄弟**的那几轮 → 兄弟 id 列表（S44）。独苗不列。 */
      siblings: Record<string, string[]>;
    }>(`/sessions/${id}`),

  /** 某一轮跑完之后的状态树，外加它之前的那一份。
   *
   * 「改结果」那个视图要有起点才能编辑，而那个起点**不是侧栏里的当前状态**
   * ——侧栏显示的是路径末端，改历史某一轮时两者不是同一个东西。
   */
  stateAtNode: (id: string, nodeId: string) =>
    json<{ before: Record<string, unknown>; values: Record<string, unknown> }>(
      `/sessions/${id}/nodes/${nodeId}/state`,
    ),

  // -- worldbook：**一级资产，不属于任何 session**（S63）--------------------
  //
  // 一份设定能开出好几个故事，所以它有自己的管理界面，不挂在某个会话下面。
  // 条目分三类：`world` / `character` 的 `content` 进 worldinfo（静态设定），
  // `state` 的 `values` 进初始状态（会变的部分）。
  //
  // **改设定立刻对所有故事生效**，那是对的——这些是"这个世界是什么"，改它等于
  // 改角色卡。已经发生过的剧情所依赖的事实是另一回事：初始状态在**每局故事
  // 各自的第一轮**冻结（S67），此后改它只影响还没开始的那些。
  listWorldbooks: () => json<WorldbookCard[]>("/worldbooks"),
  createWorldbook: (book?: Partial<Worldbook>) =>
    json<Worldbook>("/worldbooks", { method: "POST", body: JSON.stringify(book ?? null) }),
  /** 导入一份文件 → **得到一份新设定**。拿到别人分享的文件时要的是这个，
   * 而不是"先建个空的再往里导"（R17）。 */
  importAsNewWorldbook: (bundle: WorldbookBundle) =>
    json<Worldbook>("/worldbooks/import", { method: "POST", body: JSON.stringify(bundle) }),
  getWorldbook: (id: string) =>
    json<{
      worldbook: Worldbook;
      entries: WorldbookEntry[];
      sessions: string[];
      initial_state: Record<string, unknown>;
    }>(`/worldbooks/${id}`),
  /** **只能改名**——worldbook 上没有别的字段了（S64），题材与尺度都是条目。 */
  patchWorldbook: (id: string, name: string) =>
    json<Worldbook>(`/worldbooks/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }),
  /** 有故事开在上面时服务端返回 409——删掉的话那几局会当场失忆。 */
  deleteWorldbook: (id: string) =>
    json<{ deleted: string }>(`/worldbooks/${id}`, { method: "DELETE" }),

  // -- 外部资料（S136/S149）------------------------------------------------
  //
  // **用户那一侧的入口是文件夹**：把文件丢进 `folder`，列表里就会出现一条待办。
  // 系统自己不动手，更新由这里几个动作触发。
  listCorpora: () => json<CorpusLibrary>("/corpora"),
  /** 把文件夹里的一个文件收进知识库并开始处理（待办里的「新文件」）。 */
  importCorpus: (sourceName: string) =>
    json<Corpus>("/corpora/import", {
      method: "POST",
      body: JSON.stringify({ source_name: sourceName }),
    }),
  /** 重新处理：**把源文件重新读进快照再建一遍索引**（待办里的「已修改」）。 */
  rebuildCorpus: (id: string) => json<Corpus>(`/corpora/${id}/rebuild`, { method: "POST" }),
  /** 只更新记录，不重建——内容没变，索引就还是对的（待办里的「改名」）。 */
  renameCorpusSource: (id: string, sourceName: string) =>
    json<Corpus>(`/corpora/${id}/rename`, {
      method: "POST",
      body: JSON.stringify({ source_name: sourceName }),
    }),
  /** 删除。**有故事挂着也照删**（S136），受影响的那几局在返回值里。 */
  deleteCorpus: (id: string) =>
    json<{ deleted: string; affected_sessions: string[] }>(`/corpora/${id}`, { method: "DELETE" }),

  attachCorpus: (sessionId: string, corpusId: string) =>
    json<Corpus[]>(`/sessions/${sessionId}/corpora/${corpusId}`, { method: "PUT" }),
  detachCorpus: (sessionId: string, corpusId: string) =>
    json<Corpus[]>(`/sessions/${sessionId}/corpora/${corpusId}`, { method: "DELETE" }),

  saveEntry: (id: string, entry: WorldbookEntry) =>
    json<WorldbookEntry>(`/worldbooks/${id}/entries`, { method: "PUT", body: JSON.stringify(entry) }),
  deleteEntry: (id: string, entryId: string) =>
    json<{ deleted: string }>(`/worldbooks/${id}/entries/${entryId}`, { method: "DELETE" }),

  /** 用户手动归段（S22 的第三条支路）。**用户说这一段收了，就是收了。** */
  closeSegment: (id: string) =>
    json<{ closed: boolean; reason?: string; end?: string; span?: number }>(
      `/sessions/${id}/close-segment`,
      { method: "POST" },
    ),

  // -- 提示词管理（R31）----------------------------------------------------
  //
  // **一份配置 = 一个接收方收到的全部文字**：`sections`（消息串）、`tools`
  // （工具描述）、`specials`（运行时另外追加的整条消息）、`rendering`（嵌在
  // 渲染里的短句）、`enabled`（这一路跑不跑）。
  //
  // 接收方分两类（S147）：agent 与单次调用，**并列，走同一套读写接口**——
  // 所以路径是 `/prompts` 而不是 `/agents`。
  //
  // 原样进原样出——前端不认识里面的结构，只按三类条目画界面。
  listPrompts: () => json<PromptCard[]>("/prompts"),
  getPrompt: (name: string) => json<PromptDoc>(`/prompts/${name}`),
  /** 保存。**服务端先试装配再写盘**——装不出来回 422，正文里说是哪一节坏了。 */
  putPrompt: (name: string, data: Record<string, unknown>) =>
    json<PromptDoc>(`/prompts/${name}`, { method: "PUT", body: JSON.stringify({ data }) }),

  /** 回看某一轮落盘的 trace（S115）。开发者面板靠它看当前连接之前的轮次。 */
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
