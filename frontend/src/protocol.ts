/**
 * 本文件由 `npm run gen:types` 生成，**不要手改**。
 * 来源：backend/agenttavern/server/protocol.py
 */

export type ClientMessage =
  Submit | Interject | Answer | Stop | SwitchBranch | EditNode | Regenerate | SetGoal | SetGoalBudget | GoalControl;
export type Type = "submit";
export type Text = string;
export type Type1 = "interject";
export type Text1 = string;
export type Interrupt = boolean;
export type Type2 = "answer";
export type QuestionId = string;
export type Selected = string;
export type Type3 = "stop";
export type Type4 = "switch_branch";
export type NodeId = string;
export type Type5 = "edit_node";
export type NodeId1 = string;
export type EditedUserInput = string | null;
export type Outline = string | null;
export type Prose = string | null;
export type StateResult = {
  [k: string]: unknown;
} | null;
export type Type6 = "regenerate";
export type NodeId2 = string;
export type Type7 = "set_goal";
export type Description = string;
export type MaxRuns = number | null;
export type MaxTotalTokens = number | null;
export type Type8 = "set_goal_budget";
export type MaxRuns1 = number | null;
export type MaxTotalTokens1 = number | null;
export type Unlimited = boolean;
export type Type9 = "goal_control";
export type Action = "pause" | "resume" | "abandon";
export type ServerMessage =
  | RunStarted
  | Delta
  | Trace
  | Question
  | RunFinished
  | NodeUpdated
  | RunFailed
  | RunInterrupted
  | StateSnapshot
  | QueueStatus
  | UsageUpdate
  | Branches
  | GoalFinished
  | GoalProgress
  | GoalPaused
  | SessionUpdated
  | ServerError;
export type Type10 = "run_started";
export type RunId = string;
export type Type11 = "delta";
export type RunId1 = string;
export type Actor = string;
export type Kind = "content" | "reasoning";
export type Text2 = string;
export type Type12 = "trace";
export type RunId2 = string;
export type Seq = number;
export type At = string;
export type TraceEventType =
  | "run_started"
  | "run_finished"
  | "run_failed"
  | "run_interrupted"
  | "context_assembled"
  | "model_request"
  | "model_response"
  | "tool_call"
  | "tool_result"
  | "outline_submitted"
  | "prose_produced"
  | "cross_check"
  | "state_committed"
  | "goal_evaluated"
  | "user_edit"
  | "user_interjected";
export type Actor1 = "orchestrator" | "director" | "writer" | "goal_manager" | "state_manager" | "user";
export type Label = string;
export type TokensIn = number | null;
export type TokensOut = number | null;
export type CacheHit = number | null;
export type Type13 = "question";
export type QuestionId1 = string;
export type Question1 = string;
export type Options = string[];
export type Type14 = "run_finished";
export type Id = string;
export type ParentId = string | null;
export type CreatedAt = string;
export type RunStatus = "complete" | "failed";
export type Text3 = string;
/**
 * 这条输入是谁给的（S102）。
 *
 * **只有 goal_manager 读它。** director 不知道有 goal 这回事，两种输入在它眼里
 * 完全一样——那是有意的：模拟输入必须和真实输入长得一模一样，否则 director
 * 会对它区别对待。
 */
export type InputSource = "user" | "simulated";
export type SubmittedAt = string;
export type Outline1 = string;
export type OutlineSummary = string | null;
export type Interjections = string[];
export type ClosesSegment = boolean;
export type SegmentSpan = number;
export type Prose1 = string;
export type DeviationReport = string | null;
export type Notes = string[];
export type Type15 = "node_updated";
export type Type16 = "run_failed";
export type Reason = string;
export type Type17 = "run_interrupted";
export type Resumable = boolean;
export type Type18 = "state";
export type LeafId = string | null;
export type Type19 = "queue";
export type Pending = string[];
export type Type20 = "usage";
export type TokensIn1 = number;
export type TokensOut1 = number;
export type Type21 = "branches";
export type NodeId3 = string;
export type Siblings = string[];
export type ActiveIndex = number;
export type Type22 = "goal_finished";
export type Outcome = string;
export type Reason1 = string;
export type Type23 = "goal_progress";
export type Description1 = string;
export type Done = boolean;
export type Subgoals = SubGoal[];
export type SpentRuns = number;
export type SpentTokens = number;
export type MaxRuns2 = number | null;
export type MaxTotalTokens2 = number | null;
export type Type24 = "goal_paused";
export type Reason2 = string;
export type Type25 = "session_updated";
export type Id1 = string;
export type CreatedAt1 = string;
export type WorldbookId = string;
export type OpeningId = string | null;
export type Opening = string;
export type CorpusIds = string[];
export type Title = string;
export type Description2 = string;
export type MaxRuns3 = number | null;
export type MaxTotalTokens3 = number | null;
export type InitialState = {
  [k: string]: unknown;
} | null;
export type ActiveLeafId = string | null;
export type InputRevision = number;
export type EditableNodeId = string | null;
export type Role = "system" | "user" | "assistant" | "tool";
export type Content = string | null;
export type Id2 = string;
export type Name = string;
export type ArgumentsRaw = string;
export type Arguments = {
  [k: string]: unknown;
} | null;
export type ParseError = string | null;
export type ToolCalls = ToolCall[];
export type ToolCallId = string | null;
export type ReasoningContent = string | null;
export type Conversation = Message[];
export type Subgoals1 = SubGoal[];
export type SpentRuns1 = number;
export type SpentTokens1 = number;
export type Paused = boolean;
export type PausedReason = string;
export type StartedAtLeafId = string | null;
export type SeenLeafId = string | null;
export type Type26 = "error";
export type Message1 = string;
export type Id3 = string;
export type Name1 = string;
/**
 * 条目分四类（S67 / S65 / S168）。
 *
 * 早先是一条条目**同时**装静态设定和初始状态、共用一个 `mode`。拆开的理由是
 * 可见性天然不同步：一个角色的背景可能几百字、该按需展开，而他「在不在场」
 * 必须每轮都在眼前——共用一个开关时只能迁就其中一个。
 */
export type EntryKind = "world" | "character" | "state" | "opening";
export type Content1 = string;
export type Voice = string;
export type IsPlayer = boolean;
export type WorldbookMode = "always" | "on_demand";
export type Format = string;
export type Version = number;
export type Name2 = string;
export type Note = string;
export type Entries = WorldbookEntry[];
export type Id4 = string;
export type Name3 = string;
export type CorpusId = string;
export type Name4 = string;
export type Note1 = string;
export type Chars = number;
export type SourceName = string;
export type SourceHash = string;
export type Encoding = string;
/**
 * 索引建到哪一步了（S144）。
 *
 * **三种状态都要能被说出来**：查一份还在建的资料，回执得写「还在建」，
 * 不能返回空列表——空会被读成「资料里没有」。
 */
export type BuildState = "pending" | "building" | "ready" | "failed";
export type ChunksDone = number;
export type ChunksTotal = number;
export type Embedded = boolean;
export type Error = string;
export type CreatedAt2 = string;

export interface AgentTavern {
  ClientMessage: ClientMessage;
  ServerMessage: ServerMessage;
  Session: Session;
  WorldbookEntry: WorldbookEntry;
  WorldbookBundle: WorldbookBundle;
  Worldbook: Worldbook;
  Corpus: Corpus;
}
/**
 * 提交一条用户输入，**开新的一轮**（F3a「推进」）。
 *
 * **进队列，每条独立成一个 run，不打包**（S107）——RP 中每个用户动作都需要独立的
 * 世界响应。要插进正在跑的那一轮，用 `Interject`（F3b）。
 */
export interface Submit {
  type?: Type;
  text: Text;
}
/**
 * 把一句话插进**正在跑的**那一轮（F3b / S50）。
 *
 * **插入点是 agent 的一次操作之间，不是一轮之后。** 一次模型调用、一次工具调用
 * 做完就是一个边界。等一轮写完再说话不叫引导，那是事后补救——用户看着 director
 * 往错方向查了六次资料，得能当场拨回来。
 *
 * `interrupt` 决定在飞的那次操作怎么办：
 *
 * - ``False``（引导）：让它做完，在下一个边界插入
 * - ``True``（打断）：掐掉它，就在那里插入
 *
 * 没有 run 在跑时服务端会回落到 `Submit` 的语义——那本来就是"新的一轮"。
 */
export interface Interject {
  type?: Type1;
  text: Text1;
  interrupt?: Interrupt;
}
/**
 * 回答 director 通过 ``ask_question`` 提出的问题。
 *
 * ``selected`` **不校验是否在选项里**（S23）：用户可以三选一，也可以都不选、
 * 自己写一条。校验等于把「模型没想到的第四条路」判为非法，而那恰恰是最该
 * 传下去的一种回答。工具那侧按"在不在选项里"分开写回执。
 */
export interface Answer {
  type?: Type2;
  question_id: QuestionId;
  selected: Selected;
}
/**
 * 打断当前 run，不带话。元操作，进控制面。
 *
 * **不是"作废"**（S48）：掐掉在飞的那次操作后，这一轮挂起成草稿，下一条输入
 * 接着它跑。按下停止只说明"别再往下跑了"，没说"刚才那些都不算"。
 *
 * writer / state 阶段没有操作边界（各是一次不可分的生成），那里靠取消整个 task。
 * **掐到哪一段，哪一段就判为失败、产出作废**，已完成的上游存进草稿等续跑
 * （S49）。**半截正文不留**——留着，下一轮会接着那半句往下写，而用户
 * 按停止恰恰是因为不想要它。
 */
export interface Stop {
  type?: Type3;
}
/**
 * 切换活跃路径。**状态随之重放**（S42）——不这样做就会"文本对、状态悄悄错"。
 */
export interface SwitchBranch {
  type?: Type4;
  node_id: NodeId;
}
/**
 * 用户直接编辑节点数据（R22）。
 *
 * **直接改写，不做依赖检查**——乐观假设用户只改最新输出，此时本无下游。
 */
export interface EditNode {
  type?: Type5;
  node_id: NodeId1;
  user_input?: EditedUserInput;
  outline?: Outline;
  prose?: Prose;
  state_delta?: StateDelta | null;
  state_result?: StateResult;
}
/**
 * 一个 run 的状态变化。**权威数据**——当前状态是沿路径重放增量的结果（S42）。
 *
 * 每 run 必须提交，**哪怕为空**（S43）。空增量极便宜；漏提交的代价极高——
 * 分叉在几百个 run 后才爆，且爆时查不出源头，还会让分支重建出错。
 */
export interface StateDelta {
  set?: Set;
  append?: Append;
  remove?: Remove;
}
export interface Set {
  [k: string]: unknown;
}
export interface Append {
  [k: string]: unknown;
}
export interface Remove {
  [k: string]: unknown;
}
/**
 * 重生成：在**当前节点**的父节点下新建兄弟（S44）。原分支保留。
 *
 * ``node_id`` 必须是当前活跃叶——**分支只能在当前节点上开**（S44）。想在更早
 * 的地方分叉，先 `SwitchBranch` 切过去，再重生成或推进；**切换不受限制，
 * 受限的只是创建**。带着 id 而不是省掉它，是为了让"你以为的当前节点已经不是
 * 当前节点了"这件事当场报错，而不是悄悄重 roll 掉另一轮。
 */
export interface Regenerate {
  type?: Type6;
  node_id: NodeId2;
}
/**
 * 设一个新目标（S104）。**已有 goal 时报错**——同一时间只有一个。
 *
 * `description` 一旦定下就不能改：goal_manager 的对话是围着它建起来的，
 * 改了等于换了个目标，而对话还是旧的。要换就先 `GoalControl(abandon)`。
 */
export interface SetGoal {
  type?: Type7;
  description: Description;
  max_runs?: MaxRuns;
  max_total_tokens?: MaxTotalTokens;
}
/**
 * 改预算。**这是"加预算接着跑"的入口**（S96）。
 *
 * 和 `SetGoal` 分开，因为它俩的可改时机相反：目标定了就不能动，
 * 预算恰恰要能在跑到一半时调大——预算耗尽是暂停不是失败。
 */
export interface SetGoalBudget {
  type?: Type8;
  max_runs?: MaxRuns1;
  max_total_tokens?: MaxTotalTokens1;
  unlimited?: Unlimited;
}
/**
 * 暂停 / 恢复 / 放弃当前 goal（S104）。
 *
 * **恢复不是"从头再来"**：对话原样留着，只是按当前路径补齐暂停期间发生的轮次。
 * 暂停期间用户可以自己跑几轮、也可以切分支——恢复时**以最终选择的分支为准**。
 */
export interface GoalControl {
  type?: Type9;
  action: Action;
}
export interface RunStarted {
  type?: Type10;
  run_id: RunId;
}
/**
 * 流式分片（S118）：**正文或思考的一小段**，边生成边推。
 *
 * **它是旁路，不是权威**——权威仍然是 `RunFinished` 带回来的那个节点。分片
 * 可能因为断线而丢，前端据此显示的东西必须能被最终结果整个替换掉，不能拿
 * 分片拼出来的字符串当真值存下来。
 *
 * **工具调用不走这里**：它们的参数是逐字拼出来的 JSON，半截 JSON 对界面毫无
 * 用处，只会逼前端去猜；等完整了随 trace 一次性给。
 */
export interface Delta {
  type?: Type11;
  run_id: RunId1;
  actor: Actor;
  kind: Kind;
  text: Text2;
}
/**
 * 一条 trace 事件的投影。
 *
 * 这里递出完整事件，**由客户端决定显示什么**——筛选规则与渲染模板属 UI 层，
 * 不得污染 trace schema（S111）。事件自带人类可读的 ``label``，
 * "呈现 agent 工作过程"这一体验就落在它上面。
 */
export interface Trace {
  type?: Type12;
  event: TraceEvent;
}
/**
 * 一条 trace 记录。落盘为 ``trace/<run_id>.jsonl`` 的一行。
 */
export interface TraceEvent {
  run_id: RunId2;
  seq: Seq;
  at?: At;
  type: TraceEventType;
  actor: Actor1;
  label: Label;
  payload?: Payload;
  tokens_in?: TokensIn;
  tokens_out?: TokensOut;
  cache_hit?: CacheHit;
}
export interface Payload {
  [k: string]: unknown;
}
/**
 * director 停下来问用户。**不设超时**（S24）——等待期间逻辑上什么都没发生。
 */
export interface Question {
  type?: Type13;
  question_id: QuestionId1;
  question: Question1;
  options: Options;
}
export interface RunFinished {
  type?: Type14;
  node: Node;
  notes?: Notes;
}
/**
 * 一个 run 的全部产物。落盘为 ``nodes/<id>.json``。
 *
 * 只存**权威数据**：当前状态是沿路径重放 ``state_delta`` 的结果，不落盘快照。
 */
export interface Node {
  id?: Id;
  parent_id?: ParentId;
  created_at?: CreatedAt;
  status?: RunStatus;
  user_input?: UserInput | null;
  outline?: Outline1;
  outline_summary?: OutlineSummary;
  interjections?: Interjections;
  closes_segment?: ClosesSegment;
  segment_span?: SegmentSpan;
  prose?: Prose1;
  deviation_report?: DeviationReport;
  state_delta?: StateDelta;
}
/**
 * 一条用户输入。
 *
 * 队列是 FIFO，**每条独立成一个 run，不得打包**（S107）——RP 中每个用户动作都
 * 需要独立的世界响应。把"我推开门"/"我环顾四周"/"我叫了一声"打包会跳过中间
 * 信息，而推开门看到什么本应影响要不要叫。
 */
export interface UserInput {
  text: Text3;
  source?: InputSource;
  submitted_at?: SubmittedAt;
}
/**
 * 一个节点被用户改过了（R22）。**回传整个节点，不回传"改了什么"。**
 *
 * 改「结果」那个视图下，落盘的增量是**服务端求差算出来的**——只把用户发来的
 * 那棵结果树塞回本地节点，本地的 `state_delta` 就成了旧的，而它才是权威数据
 * （S42）。回传整份是唯一不需要客户端猜的做法。
 */
export interface NodeUpdated {
  type?: Type15;
  node: Node;
}
export interface RunFailed {
  type?: Type16;
  reason: Reason;
}
/**
 * 用户打断了这一轮。**打断是挂起，不是作废**（S48）。
 *
 * `resumable` 为真表示推演已存成草稿：下一条输入会**接在它后面**继续，
 * 而不是从头重来。UI 必须把这件事说清楚——用户以为按的是"取消"，
 * 结果下一句话被接进了同一段推演，那是最难查的一类困惑。
 */
export interface RunInterrupted {
  type?: Type17;
  resumable?: Resumable;
}
/**
 * 当前路径上的完整状态。**派生数据**——权威是各节点的增量（S42）。
 */
export interface StateSnapshot {
  type?: Type18;
  values: Values;
  leaf_id?: LeafId;
}
export interface Values {
  [k: string]: unknown;
}
/**
 * 队列可见性：用户应能看到待处理的输入并撤销尚未消费的部分。
 */
export interface QueueStatus {
  type?: Type19;
  pending: Pending;
}
/**
 * 成本可见性：**实时显示消耗，由用户手动打断**，不做自动预算拦截。
 */
export interface UsageUpdate {
  type?: Type20;
  tokens_in: TokensIn1;
  tokens_out: TokensOut1;
}
export interface Branches {
  type?: Type21;
  node_id: NodeId3;
  siblings: Siblings;
  active_index: ActiveIndex;
}
/**
 * goal 走完了，控制权交回用户（S96）。
 *
 * **`outcome` 必须传出去**：两种终态都停下来，但那是两件完全不同的事——
 * 一个是达成了，一个是这个目标已经不可能达成了，用户的下一步动作完全不同。
 */
export interface GoalFinished {
  type?: Type22;
  outcome: Outcome;
  reason?: Reason1;
}
/**
 * goal 又推了一轮之后的进度（S129）。
 *
 * **不推整个 `GoalRun`**：它带着 goal_manager 那份跨 run 的长对话，一轮就上
 * 十万字，而界面一个字都用不上。这里只给要显示的东西。
 *
 * **为什么需要这条消息**：进度只写在盘上的 `goal_run` 里，WS 那侧此前只有
 * 「结束」和「暂停」两条——也就是说 goal 跑着的那几轮，界面上什么都不会变，
 * 直到它突然结束。而中间状态一条条被划掉，恰恰是用户最想看的东西。
 */
export interface GoalProgress {
  type?: Type23;
  subgoals?: Subgoals;
  spent_runs?: SpentRuns;
  spent_tokens?: SpentTokens;
  max_runs?: MaxRuns2;
  max_total_tokens?: MaxTotalTokens2;
}
/**
 * 一个**中间状态**。goal_manager 给出来的，不是用户写的。
 *
 * **它描述状态，不是步骤**，而且彼此之间**没有先后依赖**（S93）：可以被跳过、
 * 以不同顺序实现、同时实现，也可能因剧情变化而不再必要。所以这个列表是一组，
 * 不是一条链——序号只用来指代，不表示顺序。
 */
export interface SubGoal {
  description: Description1;
  done?: Done;
}
/**
 * goal 停下了，**但没有结束**（S92）。
 *
 * 和 `GoalFinished` 分开是因为用户的下一步完全不同：结束了就是结束了，
 * 暂停则是"要不要接着跑"——最常见的来源是预算到顶，把上限调大就能继续，
 * 对话原样留着，不用重新判一遍。
 */
export interface GoalPaused {
  type?: Type24;
  reason: Reason2;
}
/**
 * **服务端在 run 之外改了 session，整份推回去。**
 *
 * 没有它的时候，凡是不触发一轮生成的改动客户端一律不知道：按「暂停」目标真的
 * 暂停了，而按钮还写着「暂停」；按「放弃」目标真的没了，而面板照旧挂着。
 * **服务端做了事，界面一动不动**——用户读到的是"这个按钮坏了"。
 *
 * 它没有被发现是因为**设目标那一条顺带 `nudge()` 起了一轮**，正文开始动，
 * 看起来像是有反应；而 pause / abandon / 加预算三条不起轮，就全成了哑的。
 *
 * **推整份而不是推差异**：session 不大，而"哪几个字段变了"是个每加一个字段
 * 就要重想一次的问题——漏掉一个的症状又是同一种，界面显示旧的东西。
 *
 * 客户端**不必也不该自己在发完之后去拉一次**：WebSocket 发出去和 HTTP GET
 * 之间没有先后保证，那一拉可能落在服务端写盘之前，拿回旧的那份。
 */
export interface SessionUpdated {
  type?: Type25;
  session: Session;
}
export interface Session {
  id?: Id1;
  created_at?: CreatedAt1;
  worldbook_id?: WorldbookId;
  opening_id?: OpeningId;
  opening?: Opening;
  corpus_ids?: CorpusIds;
  meta?: SessionMeta;
  initial_state?: InitialState;
  active_leaf_id?: ActiveLeafId;
  input_revision?: InputRevision;
  editable_node_id?: EditableNodeId;
  goal_run?: GoalRun | null;
}
/**
 * **这局故事**自己的元指令。定义目标，不定义过程。
 *
 * 这里以前还装着题材与基调、尺度、角色语言习惯、用户扮演谁——**那些是世界
 * 自己的属性，已经搬进 `WorldMeta`**（S63）。搬家的依据是这个类原本就写着的
 * 判别式「换个人来读还成不成立」：它同样答得出「换局故事还成不成立」，而答案
 * 也是成立。当时没搬，是因为 worldbook 还不是独立的东西（S68：一份 worldbook
 * 开启一个 session），"世界的属性"和"故事的属性"没有区别的余地。
 *
 * 文风和视角两样也不在这儿：**它们上调到了 writer.yaml**（R31），
 * 和铺陈程度一样是一个 `choice` 的 `selected`。
 *
 * **没有「否定项」字段**（S91）。不能提前揭示的东西属于世界设定，写进
 * worldinfo 或 state；「不许写什么」这类写作纪律属于提示词，写进 YAML。
 * 单立一个字段等于给同一件事开第三个入口，而它两侧都没人注入。
 */
export interface SessionMeta {
  title?: Title;
  goal?: Goal | null;
}
/**
 * 长任务终止条件（R24）。
 *
 * 机制：正常一轮生成结束后检查是否达标，未达标则开启下一 run。
 *
 * **goal 不是模式**——有无 goal 系统跑法一样，只是 run 末多一次 goal_manager 的
 * 调用。达标判定一律交给模型，不降级为代码：那些结构化字段的值本就是模型写的，
 * 代码读取比对不是独立验证（P11）。
 */
export interface Goal {
  description: Description2;
  max_runs?: MaxRuns3;
  max_total_tokens?: MaxTotalTokens3;
}
/**
 * 一次 goal 从创建到结束期间的**运行时状态**（S92）。
 *
 * 和 `SessionMeta.goal` 分开：那个是**用户配置**（目标是什么、给多少预算），
 * 这个是**跑起来之后的东西**（对话、进度、花销）。混在一起的话，"用户改了什么"
 * 和"系统跑出了什么"就分不开，而前者要禁改、后者每轮都在变。
 *
 * **它挂在 session 上，不挂节点。** goal 跑着的时候不允许切分支，所以 S104 让
 * `GoalPlan` 跟着节点走的那个理由（分支隔离）不再成立。
 */
export interface GoalRun {
  conversation?: Conversation;
  plan?: GoalPlan | null;
  spent_runs?: SpentRuns1;
  spent_tokens?: SpentTokens1;
  paused?: Paused;
  paused_reason?: PausedReason;
  started_at_leaf_id?: StartedAtLeafId;
  seen_leaf_id?: SeenLeafId;
}
export interface Message {
  role: Role;
  content?: Content;
  tool_calls?: ToolCalls;
  tool_call_id?: ToolCallId;
  reasoning_content?: ReasoningContent;
}
/**
 * 模型发起的一次工具调用。
 *
 * ``arguments`` 在 OpenAI 格式里是 **JSON 字符串**，而模型时常输出不合法的 JSON。
 * 这里把解析结果和解析错误都留下——**参数错误是模型可恢复的错误**，应作为结构化
 * 的 tool result 回给它重试，而不是让整个 run 崩掉。
 */
export interface ToolCall {
  id: Id2;
  name: Name;
  arguments_raw?: ArgumentsRaw;
  arguments?: Arguments;
  parse_error?: ParseError;
}
/**
 * 把 goal 拆成的一串阶段（S93）。
 *
 * **它们是脚手架，不是执行契约。** 存在的目的是逼模型在动手之前先想清楚差距
 * 在哪；某一条最终有没有原样实现并不要紧——剧情变了就重给一版。因此没有
 * 「第几版」这种计数，也没有次数上限。
 *
 * 但 `finish(done)` **要求全部标为成立**。这两条不矛盾，合起来才是要点：
 * **重给随时可以，而宣布完成之前必须用「当前的」那一版重核一遍**，不是照着
 * 三轮前那份打勾。
 *
 * **它挂在 session 上**（`GoalRun.plan`），不再挂节点。S104 让它跟着节点走是
 * 为了分支隔离；goal 跑着的时候不允许切分支（S104），那个理由不成立了。
 */
export interface GoalPlan {
  subgoals?: Subgoals1;
}
export interface ServerError {
  type?: Type26;
  message: Message1;
}
export interface WorldbookEntry {
  id?: Id3;
  name: Name1;
  kind?: EntryKind;
  content?: Content1;
  voice?: Voice;
  is_player?: IsPlayer;
  values?: Values1;
  mode?: WorldbookMode;
}
export interface Values1 {
  [k: string]: unknown;
}
/**
 * 一份可以拷走的 worldbook。
 */
export interface WorldbookBundle {
  format?: Format;
  version?: Version;
  name?: Name2;
  note?: Note;
  entries?: Entries;
}
/**
 * 一份设定，**一级资产**（S63）：不属于任何 session，可以开出好几个。
 *
 * **它只有名字，没有别的字段**（S64）。题材与基调、剧情尺度曾经是这里的两个
 * 字段（S63 刚把它们从 session 搬过来），现在是两条 `WORLD` 条目——
 *
 * > `WORLD` 的定义本来就是「一段不受限制的文本」，而"现代，悬疑，克制"和
 * > "旧货店在老城区一条窄巷尽头"是同一种东西：**一段关于这个世界的字**。
 * > 凭什么一个要特殊处理。
 *
 * 买到的是**一种东西、一个编辑器**：设定页不再分成"上面一个属性表单 + 下面一个
 * 条目列表"，只有条目。代价是题材与尺度不再是独立可挪的一节，位置由
 * `render_worldinfo` 决定——那正是「不特殊处理」的定义本身带来的。
 *
 * `character_voices` / `user_character` 更早一步走的同一条路（S65）：它们成了
 * `CHARACTER` 条目上的 `voice` 与 `is_player`。
 *
 * 条目不在这个模型里——它们一条一个文件，由 `WorldbookRepository` 管。
 */
export interface Worldbook {
  id?: Id4;
  name?: Name3;
}
/**
 * 一份资料的元信息。**正文不在这里**——它单独落一个纯文本文件（S143）。
 */
export interface Corpus {
  corpus_id?: CorpusId;
  name?: Name4;
  note?: Note1;
  chars?: Chars;
  source_name?: SourceName;
  source_hash?: SourceHash;
  encoding?: Encoding;
  build?: BuildStatus;
  created_at?: CreatedAt2;
}
export interface BuildStatus {
  state?: BuildState;
  chunks_done?: ChunksDone;
  chunks_total?: ChunksTotal;
  embedded?: Embedded;
  error?: Error;
}
