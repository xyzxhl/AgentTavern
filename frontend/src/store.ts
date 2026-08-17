// 会话状态。**大部分状态来自 WS 推送而非请求-响应**，所以是一个由消息驱动的
// reducer，不是请求缓存（TechStack.md 选 Zustand 而非 TanStack Query 的理由）。

import { create } from "zustand";

import { SessionSocket, api, type ServerMeta, type SessionCard } from "./api";
import type {
  ClientMessage,
  EditNode,
  GoalProgress,
  Node,
  Question,
  ServerMessage,
  Session,
  TraceEvent,
} from "./protocol";

export type RunPhase = "idle" | "running" | "asking";

/** 主区当前是哪一页。`story` 是默认的正文流；另外两个是左栏上方的独立页面入口。 */
export type Page = { kind: "story" } | { kind: "worldbooks"; open?: string } | { kind: "prompts" };

/** 某个 agent 连续说的一段过程（思考段，或它推演途中的自言自语）。 */
export interface Thought {
  actor: string;
  text: string;
}

export interface SessionState {
  meta: ServerMeta | null;
  /** 会话列表。**带标题和它开在哪份设定上**（D172）——一串 id 认不出哪局是哪局。 */
  sessions: SessionCard[];
  sessionId: string | null;
  session: Session | null;

  /** 当前路径上的节点，按顺序。**一个 node 就是一轮**（run = node = 细纲 = 正文）。 */
  path: Node[];
  state: Record<string, unknown>;

  /** 路径上**有兄弟**的那几轮 → 兄弟 id 列表（D46/D147）。独苗不列。
   *
   * 随路径一起拿，不是一个节点一次请求——切换器要显示 `⟨2/3⟩` 就得先知道有没有
   * 第 2 支，而一条 20 轮的路径会因此打 20 个请求。这份数据只在重生成之后才变。
   */
  siblings: Record<string, string[]>;

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
   * **按来源分开，不按 kind 分**：`content` 分片几个 agent 都在发——director
   * 那些是推演途中的自言自语，goal_manager 那些是它判定时说的话，只有 writer
   * 的才是正文。混在一起攒，界面上就会把「我先把这一轮推演清楚」当成故事的
   * 第一句显示出来。
   *
   * 过程那半**按 agent 分段留着**（D166）：一轮里最多四个 agent 说过话，
   * 拼成一大坨就分不出哪句是谁的了，而"是谁在想这件事"恰恰是查问题时的第一个
   * 问题。相邻的同一个 agent 合并成一段。
   *
   * **正文那半是旁路，不是权威**——分片可能因为断线而丢，所以它只用来
   * 「边写边看」，一等到 `run_finished` 就让位给节点里的正文。过程那半相反：
   * 它在节点里没有对应物，扔掉就永远看不到了，所以落地之后照样留着。
   */
  streaming: Record<string, { prose: string; process: Thought[] }>;

  /** 刚发出去、还没等到 `run_started` 的那句话。**只为把它归到正确的一轮名下。**
   * 服务端要到 `run_finished` 才把输入回给我们，而过程要在那之前就显示出来。 */
  pendingInput: string;

  question: Question | null;
  queue: string[];
  usage: { tokensIn: number; tokensOut: number };
  notice: string | null;
  /** 上一轮被打断且**存成了草稿**——下一条输入会接着它跑，不是从头重来。 */
  resumable: boolean;

  /** 这一轮是重生成出来的——**落地之后要重拉兄弟关系**，切换器才知道有第几支。 */
  rerolling: boolean;

  /** goal 的实时进度（D161）。`null` = 还没跑过一轮，或者没有 goal。
   *
   * **它是投影，不是权威**——权威在 `session.goal_run`。这里只为了让中间状态
   * 被划掉的那一刻界面就能动，不必等 goal 结束。
   */
  goal: GoalProgress | null;

  /** 主区显示哪一页。**独立页面不是浮层**（D172）：编辑设定要待很久，
   * 而浮层的语义是「临时打断，看完就关」。 */
  page: Page;
  go: (page: Page) => void;

  /** 展开了节点编辑器的那一轮（D65）。**一次只开一个**——两个编辑器同时开着，
   * 保存顺序就成了隐含语义。`null` 是都收着。 */
  editing: string | null;
  edit: (nodeId: string | null) => void;

  /** 切到另一支（D147）。**回到历史分叉点的唯一路径**——重生成产生的兄弟节点
   * 只能从这里回去。整条路径会换掉，状态随之重放（D48）。 */
  switchBranch: (nodeId: string) => void;
  /** 在某一轮重开一支（D46）。不是当前叶时**先切过去**——D147 限制的是创建，
   * 切换不受限，所以"在第 3 轮重新生成"就是这两步。 */
  regenerate: (nodeId: string) => void;
  /** 直接改一个节点的产出（D65）。改完服务端把整份节点回传（`node_updated`）。 */
  editNode: (patch: { nodeId: string } & Partial<EditNode>) => void;

  connect: (sessionId: string) => Promise<void>;
  disconnect: () => void;
  bootstrap: () => Promise<void>;
  refreshSessions: () => Promise<void>;
  newSession: (worldbookId?: string) => Promise<void>;
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
          const now = s.streaming[at] ?? { prose: "", process: [] };
          return {
            streaming: {
              ...s.streaming,
              [at]: {
                prose: now.prose + (isProse ? message.text : ""),
                process: isProse ? now.process : append(now.process, message.actor, message.text),
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
          // **正文到此为止，思考段留着**（D166）。
          //
          // 正文有权威版本——节点里那份，留两份只会不一致。思考段没有：它不在
          // 节点里，也不在开发者模式之外的任何地方，扔掉就是永远看不到了。
          // 而 goal_manager **是在这条消息之后才跑的**，它的分片挂在同一个
          // run_id 上；这里整条抹掉的话，它的思考连落脚的地方都没有。
          streaming: {
            ...s.streaming,
            [message.node.id ?? ""]: {
              prose: "",
              process: s.streaming[message.node.id ?? ""]?.process ?? [],
            },
          },
          // 同一个 id 就替换（重生成会落新节点，切分支会整条换掉）。
          path: upsert(s.path, message.node),
        }));
        // **重生成刚给某一轮添了个兄弟**，切换器得知道。只在重生成之后拉一次：
        // 平常推进落的是独苗，兄弟关系没变，每轮都拉等于白拉一份完整路径。
        if (get().rerolling) {
          set({ rerolling: false });
          void get().refreshState();
        }
        break;

      case "node_updated":
        // **整份替换那个节点。** 改「结果」时落盘的增量是服务端求差算出来的，
        // 把发出去的东西塞回本地，`state_delta` 就成了旧的——而它是权威（D48）。
        set((s) => ({ path: upsert(s.path, message.node) }));
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
        // **叶子不是我们以为的那个 → 整条路径换过了**（切分支）。这条自纠比让
        // `switchBranch` 自己去拉可靠：它是在收到服务端的确认之后才动的，
        // 而"发完就拉"会和服务端处理那条消息抢先后。
        if (message.leaf_id && message.leaf_id !== get().path.at(-1)?.id) {
          void get().refreshState();
        }
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

      case "goal_progress":
        set({ goal: message });
        break;

      case "goal_finished":
        // goal 结束时服务端把 `meta.goal` 一并清掉了（不留历史 goal），
        // 所以这里也要把进度清干净，否则面板会挂着一份没有主人的进度。
        set({
          goal: null,
          notice: `目标${message.outcome === "done" ? "达成" : "失败"}：${message.reason}`,
        });
        void get().refreshState();
        break;

      case "goal_paused":
        set({ notice: `目标已暂停：${message.reason}` });
        void get().refreshState();
        break;

      case "error":
        set({ notice: message.message });
        break;
    }
  }

  return {
    meta: null,
    sessions: [],
    sessionId: null,
    page: { kind: "story" },
    session: null,
    path: [],
    state: {},
    siblings: {},
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
    rerolling: false,
    goal: null,
    editing: null,

    go: (page) => set({ page }),
    edit: (nodeId) => set({ editing: nodeId }),

    switchBranch(nodeId) {
      // 路径整条换掉，状态随之重放（D48）。这里只发消息——服务端回的那份
      // `state` 快照带着新的叶子 id，`apply` 认出来之后自己去把路径拉回来。
      get().send({ type: "switch_branch", node_id: nodeId });
      set({ editing: null });
    },

    regenerate(nodeId) {
      // **D147 限制的是创建，不是切换**：分支只能在当前节点上开，所以"在第 3 轮
      // 重新生成"就是「先切过去、再重生成」两步。这两步在这里做完，用户按的是
      // 一个按钮——但后面那几轮会移出当前路径，所以调用处要先问过。
      const { path, send } = get();
      if (path.at(-1)?.id !== nodeId) send({ type: "switch_branch", node_id: nodeId });
      send({ type: "regenerate", node_id: nodeId });
      set({ editing: null, rerolling: true });
    },

    editNode({ nodeId, ...patch }) {
      get().send({ type: "edit_node", node_id: nodeId, ...patch });
      set({ editing: null });
    },

    async bootstrap() {
      const [meta, sessions] = await Promise.all([api.meta(), api.listSessions()]);
      set({ meta, sessions });
      const first = sessions[0];
      if (first) await get().connect(first.id);
    },

    async refreshSessions() {
      set({ sessions: await api.listSessions() });
    },

    async newSession(worldbookId) {
      // 不传就由服务端现配一份空设定（D170）：**先开故事、边写边补**也要走得通。
      const created = await api.createSession(
        worldbookId ? { worldbook_id: worldbookId } : undefined,
      );
      await get().refreshSessions();
      await get().connect(created.id ?? "");
      set({ page: { kind: "story" } });
    },

    async connect(sessionId) {
      get().disconnect();
      const detail = await api.getSession(sessionId);
      set({
        sessionId,
        session: detail.session,
        path: detail.path,
        state: detail.state,
        siblings: detail.siblings,
        phase: "idle",
        traces: {},
        live: null,
        streaming: {},
        pendingInput: "",
        question: null,
        queue: [],
        notice: null,
        goal: null,
        editing: null,
        rerolling: false,
      });

      socket = new SessionSocket(sessionId, apply, (reason) => set({ notice: reason }));
      socket.open();
    },

    async refreshState() {
      const id = get().sessionId;
      if (!id) return;
      const detail = await api.getSession(id);
      set({
        session: detail.session,
        state: detail.state,
        path: detail.path,
        siblings: detail.siblings,
      });
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

/** 接上最后一段，来源变了才另起一段。**分片是逐字来的**，一片一段的话
 * 一轮下来会有两万多个元素，而它们本来就是一句话。 */
function append(process: Thought[], actor: string, text: string): Thought[] {
  const last = process.at(-1);
  if (last?.actor === actor) {
    return [...process.slice(0, -1), { actor, text: last.text + text }];
  }
  return [...process, { actor, text }];
}

function upsert(path: Node[], node: Node): Node[] {
  const at = path.findIndex((n) => n.id === node.id);
  if (at >= 0) return path.map((n, i) => (i === at ? node : n));
  // **重生成落的是兄弟：它顶掉原来那一支，不是接在它后面**（D46）。同父的那个
  // 还留在路径上的话，界面会把同一轮的两个版本前后摆着，读起来像故事写了两遍。
  const sibling = path.findIndex((n) => n.parent_id === node.parent_id);
  if (sibling >= 0) return [...path.slice(0, sibling), node];
  return [...path, node];
}
