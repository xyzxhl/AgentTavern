// 会话状态。**大部分状态来自 WS 推送而非请求-响应**，所以是一个由消息驱动的
// reducer，不是请求缓存（TechStack.md 选 Zustand 而非 TanStack Query 的理由）。

import { create } from "zustand";

import { SessionSocket, api, type ServerMeta } from "./api";
import type { ClientMessage, Node, Question, ServerMessage, Session, TraceEvent } from "./protocol";

export type RunPhase = "idle" | "running" | "asking";

export interface SessionState {
  meta: ServerMeta | null;
  sessionIds: string[];
  sessionId: string | null;
  session: Session | null;

  /** 当前路径上的节点，按顺序。**一个 node 就是一轮**（run = node = 细纲 = 正文）。 */
  path: Node[];
  state: Record<string, unknown>;

  phase: RunPhase;
  /** 这一轮已经发生的 trace。**每开一轮清空**——它讲的是"这一轮在做什么"。 */
  trace: TraceEvent[];
  /** 跨轮累积，开发者面板按 run 分组回看。 */
  allTrace: TraceEvent[];

  question: Question | null;
  queue: string[];
  usage: { tokensIn: number; tokensOut: number };
  notice: string | null;
  /** 上一轮被打断且**存成了草稿**——下一条输入会接着它跑，不是从头重来。 */
  resumable: boolean;

  connect: (sessionId: string) => Promise<void>;
  disconnect: () => void;
  bootstrap: () => Promise<void>;
  newSession: () => Promise<void>;
  /** 把状态从服务端拉回来对齐。**改了 worldbook 的种子之后必须调**——
   * 初始 state 是现算的，改种子会向后影响每一轮，而 WS 那侧不会为此推消息。 */
  refreshState: () => Promise<void>;
  send: (message: ClientMessage) => void;
  dismissNotice: () => void;
}

let socket: SessionSocket | null = null;

export const useSession = create<SessionState>((set, get) => {
  /** 一条服务端消息 → 一次状态更新。**穷尽 switch**，漏掉一种会是编译错误。 */
  function apply(message: ServerMessage): void {
    switch (message.type) {
      case "run_started":
        set({ phase: "running", trace: [], notice: null, resumable: false });
        break;

      case "trace":
        set((s) => ({
          trace: [...s.trace, message.event],
          allTrace: [...s.allTrace, message.event],
        }));
        break;

      case "question":
        // **挂起，不设超时**（D66）：等待期间逻辑上什么都没发生。
        set({ phase: "asking", question: message });
        break;

      case "run_finished":
        set((s) => ({
          phase: "idle",
          question: null,
          // 同一个 id 就替换（重生成会落新节点，切分支会整条换掉）。
          path: upsert(s.path, message.node),
        }));
        break;

      case "run_failed":
        set({ phase: "idle", notice: `这一轮失败了：${message.reason}` });
        break;

      case "run_interrupted":
        // **打断是挂起不是作废**（D118）。用户以为按的是"取消"，结果下一句话
        // 被接进了同一段推演——那是最难查的一类困惑，必须当场说清楚。
        set({
          phase: "idle",
          resumable: message.resumable ?? false,
          notice: message.resumable
            ? "已打断。推演存成了草稿，下一条输入会接着它跑；想从头来请重新生成。"
            : "已打断。",
        });
        break;

      case "state":
        set({ state: message.values });
        break;

      case "queue":
        set({ queue: message.pending ?? [] });
        break;

      case "usage":
        set((s) => ({
          usage: {
            tokensIn: s.usage.tokensIn + message.tokens_in,
            tokensOut: s.usage.tokensOut + message.tokens_out,
          },
        }));
        break;

      case "branches":
        break;

      case "goal_finished":
        set({ notice: `目标${message.outcome === "done" ? "达成" : "失败"}：${message.reason}` });
        break;

      case "goal_paused":
        set({ notice: `目标已暂停：${message.reason}` });
        break;

      case "error":
        set({ notice: message.message });
        break;
    }
  }

  return {
    meta: null,
    sessionIds: [],
    sessionId: null,
    session: null,
    path: [],
    state: {},
    phase: "idle",
    trace: [],
    allTrace: [],
    question: null,
    queue: [],
    usage: { tokensIn: 0, tokensOut: 0 },
    notice: null,
    resumable: false,

    async bootstrap() {
      const [meta, sessionIds] = await Promise.all([api.meta(), api.listSessions()]);
      set({ meta, sessionIds });
      const first = sessionIds[0];
      if (first) await get().connect(first);
    },

    async newSession() {
      const created = await api.createSession();
      set((s) => ({ sessionIds: [...s.sessionIds, created.id ?? ""] }));
      await get().connect(created.id ?? "");
    },

    async connect(sessionId) {
      get().disconnect();
      const detail = await api.getSession(sessionId);
      set({
        sessionId,
        session: detail.session,
        path: detail.path,
        state: detail.state,
        phase: "idle",
        trace: [],
        allTrace: [],
        question: null,
        queue: [],
        notice: null,
      });

      socket = new SessionSocket(sessionId, apply, (reason) => set({ notice: reason }));
      socket.open();
    },

    async refreshState() {
      const id = get().sessionId;
      if (!id) return;
      const detail = await api.getSession(id);
      set({ session: detail.session, state: detail.state, path: detail.path });
    },

    disconnect() {
      socket?.close();
      socket = null;
    },

    send(message) {
      if (!socket?.send(message)) set({ notice: "还没连上，这条没发出去。" });
    },

    dismissNotice: () => set({ notice: null }),
  };
});

function upsert(path: Node[], node: Node): Node[] {
  const at = path.findIndex((n) => n.id === node.id);
  if (at >= 0) return path.map((n, i) => (i === at ? node : n));
  return [...path, node];
}
