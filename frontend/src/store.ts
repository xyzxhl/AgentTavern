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

  /** trace **按 run 分组**：`run_id` → 这一轮的全部事件。
   *
   * 过程是正文流的一部分（每一轮自己的"它做了什么"），不是一块全局日志——
   * 所以不能只留"当前这一轮"。**`run_id` 就是 `node.id`**（run = node），
   * 于是渲染某一轮时直接拿它的 id 来查。
   */
  traces: Record<string, TraceEvent[]>;

  /** 正在跑的那一轮：还没有节点，但已经有 run_id 和用户说的那句话。 */
  live: { runId: string; input: string } | null;

  /** 流式攒出来的字：`run_id` → `{ 正文, 过程 }`（D160）。
   *
   * **按来源分开，不按 kind 分**：`content` 分片两个 agent 都在发——director
   * 那些是推演途中的自言自语，writer 的才是正文。混在一起攒，界面上就会把
   * 「我先把这一轮推演清楚」当成故事的第一句显示出来。
   *
   * **它是旁路，不是权威**——分片可能因为断线而丢，所以这里攒的东西只用来
   * 「边写边看」，一等到 `run_finished` 就让位给节点里的正文，不当真值留着。
   */
  streaming: Record<string, { prose: string; process: string }>;

  /** 刚发出去、还没等到 `run_started` 的那句话。**只为把它归到正确的一轮名下。**
   * 服务端要到 `run_finished` 才把输入回给我们，而过程要在那之前就显示出来。 */
  pendingInput: string;

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
  /** 把状态从服务端拉回来对齐。**改了 worldbook 的初始状态条目之后必须调**——
   * 故事开始之前它是现算的，改了会立刻改变当前 state，而 WS 那侧不会为此推消息。 */
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
        set((s) => ({
          phase: "running",
          notice: null,
          resumable: false,
          // 刚提交的那句话归到这一轮名下。自动续跑时没人打字，留空。
          live: { runId: message.run_id, input: s.pendingInput },
          pendingInput: "",
        }));
        break;

      case "delta": {
        const at = message.run_id;
        // **只有 writer 写的 `content` 是正文。** 别的都是过程：director 的
        // 思考段、它推演途中的自言自语、writer 自己的思考段。
        const isProse = message.actor === "writer" && message.kind === "content";
        set((s) => {
          const now = s.streaming[at] ?? { prose: "", process: "" };
          return {
            streaming: {
              ...s.streaming,
              [at]: {
                prose: now.prose + (isProse ? message.text : ""),
                process: now.process + (isProse ? "" : message.text),
              },
            },
          };
        });
        break;
      }

      case "trace":
        set((s) => ({
          traces: {
            ...s.traces,
            [message.event.run_id]: [...(s.traces[message.event.run_id] ?? []), message.event],
          },
        }));
        break;

      case "question":
        // **挂起，不设超时**（D66）：等待期间逻辑上什么都没发生。
        set({ phase: "asking", question: message });
        break;

      case "run_finished":
        // 节点落地，那一轮的过程随节点一起收起——`traces` 按 run_id 存着，
        // 而 `run_id` 就是 `node.id`，所以这里什么都不用搬。
        set((s) => ({
          phase: "idle",
          question: null,
          live: null,
          // **攒出来的字到此为止**：节点里的正文才是权威，留着两份只会不一致。
          streaming: without(s.streaming, message.node.id ?? ""),
          // 同一个 id 就替换（重生成会落新节点，切分支会整条换掉）。
          path: upsert(s.path, message.node),
        }));
        break;

      case "run_failed":
        set({ phase: "idle", live: null, notice: `这一轮失败了：${message.reason}` });
        break;

      case "run_interrupted":
        // **打断是挂起不是作废**（D118）。用户以为按的是"取消"，结果下一句话
        // 被接进了同一段推演——那是最难查的一类困惑，必须当场说清楚。
        set({
          phase: "idle",
          live: null,
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
    traces: {},
    live: null,
    streaming: {},
    pendingInput: "",
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
        traces: {},
        live: null,
        streaming: {},
        pendingInput: "",
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
      if (!socket?.send(message)) {
        set({ notice: "还没连上，这条没发出去。" });
        return;
      }
      // 只有 `submit` 会开新的一轮；插话落在正在跑的那一轮里，不另起。
      if (message.type === "submit") set({ pendingInput: message.text });
    },

    dismissNotice: () => set({ notice: null }),
  };
});

function without<T>(map: Record<string, T>, key: string): Record<string, T> {
  const { [key]: _gone, ...rest } = map;
  return rest;
}

function upsert(path: Node[], node: Node): Node[] {
  const at = path.findIndex((n) => n.id === node.id);
  if (at >= 0) return path.map((n, i) => (i === at ? node : n));
  return [...path, node];
}
