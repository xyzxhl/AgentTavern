/**
 * 本文件由 `npm run gen:types` 生成，**不要手改**。
 * 来源：backend/agenttavern/server/protocol.py
 */

export type ClientMessage =
  Submit | Interject | Answer | Stop | SwitchBranch | EditNode | Regenerate | SetGoal | SetGoalBudget | GoalControl;
export type Type = "submit";
export type Text = string;
/**
 * 输入处理三档（D41）。取代了原先"转述二值 × 故事跨度"的设计。
 *
 * 故事跨度不再是独立维度：想快进就写在指令里（那本就是指令内容），
 * 想零推进就是 NARRATE_ONLY。
 */
export type InputHandling = "narrate_and_advance" | "narrate_only" | "advance_only";
/**
 * 模型剧情控制权。
 *
 * 直接反映为 ``ask_question`` 工具的调用积极性——**控制权越高，调用积极性越低**（D39）。
 * 三档均保持 run ↔ 细纲一一对应：交互式创作是在一个 run 内多次提问，
 * 而非拆成多个 run（D58）。
 */
export type ModelAuthority = "low" | "medium" | "high";
/**
 * 展开密度：每单位故事时间写多少字。
 *
 * 与"故事跨度"正交——同一场戏可以写 200 字也可以写 2000 字。
 * 同时影响两侧：细纲的事件密度（director）与正文的铺陈程度（writer）。
 */
export type ExpansionDensity = "sparse" | "normal" | "rich";
export type AllowSpeakingForUser = boolean | null;
export type ProseStyle = string | null;
export type Pov = string | null;
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
export type Outline = string | null;
export type Prose = string | null;
export type StateSet = {
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
export type Type9 = "goal_control";
export type Action = "pause" | "resume" | "abandon";
export type ServerMessage =
  | RunStarted
  | Delta
  | Trace
  | Question
  | RunFinished
  | RunFailed
  | RunInterrupted
  | StateSnapshot
  | QueueStatus
  | UsageUpdate
  | Branches
  | GoalFinished
  | GoalProgress
  | GoalPaused
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
  | "contract_check"
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
 * 这条输入是谁给的（D148）。
 *
 * **只有 goal_manager 读它。** director 不知道有 goal 这回事，两种输入在它眼里
 * 完全一样——那是有意的：模拟输入必须和真实输入长得一模一样，否则 director
 * 会对它区别对待。
 */
export type InputSource = "user" | "simulated";
export type SubmittedAt = string;
/**
 * 输入处理三档（D41）。取代了原先"转述二值 × 故事跨度"的设计。
 *
 * 故事跨度不再是独立维度：想快进就写在指令里（那本就是指令内容），
 * 想零推进就是 NARRATE_ONLY。
 */
export type InputHandling1 = "narrate_and_advance" | "narrate_only" | "advance_only";
/**
 * 模型剧情控制权。
 *
 * 直接反映为 ``ask_question`` 工具的调用积极性——**控制权越高，调用积极性越低**（D39）。
 * 三档均保持 run ↔ 细纲一一对应：交互式创作是在一个 run 内多次提问，
 * 而非拆成多个 run（D58）。
 */
export type ModelAuthority1 = "low" | "medium" | "high";
/**
 * 展开密度：每单位故事时间写多少字。
 *
 * 与"故事跨度"正交——同一场戏可以写 200 字也可以写 2000 字。
 * 同时影响两侧：细纲的事件密度（director）与正文的铺陈程度（writer）。
 */
export type ExpansionDensity1 = "sparse" | "normal" | "rich";
export type AllowSpeakingForUser1 = boolean;
export type ProseStyle1 = string;
export type Pov1 = string;
export type Outline1 = string;
export type OutlineSummary = string | null;
export type Role = "system" | "user" | "assistant" | "tool";
export type Content = string | null;
export type Id1 = string;
export type Name = string;
export type ArgumentsRaw = string;
export type Arguments = {
  [k: string]: unknown;
} | null;
export type ParseError = string | null;
export type ToolCalls = ToolCall[];
export type ToolCallId = string | null;
export type ReasoningContent = string | null;
export type DirectorMessages = Message[];
export type ClosesSegment = boolean;
export type SegmentSpan = number;
export type Prose1 = string;
export type DeviationReport = string | null;
export type Notes = string[];
export type Type15 = "run_failed";
export type Reason = string;
export type Type16 = "run_interrupted";
export type Resumable = boolean;
export type Type17 = "state";
export type LeafId = string | null;
export type Type18 = "queue";
export type Pending = string[];
export type Type19 = "usage";
export type TokensIn1 = number;
export type TokensOut1 = number;
export type Type20 = "branches";
export type NodeId3 = string;
export type Siblings = string[];
export type ActiveIndex = number;
export type Type21 = "goal_finished";
export type Outcome = string;
export type Reason1 = string;
export type Type22 = "goal_progress";
export type Description1 = string;
export type Done = boolean;
export type Subgoals = SubGoal[];
export type SpentRuns = number;
export type SpentTokens = number;
export type MaxRuns2 = number;
export type MaxTotalTokens2 = number;
export type Type23 = "goal_paused";
export type Reason2 = string;
export type Type24 = "error";
export type Message1 = string;
export type Id2 = string;
export type CreatedAt1 = string;
export type Title = string;
export type ContentScale = string;
export type GenreTone = string;
export type UserCharacter = string;
export type Description2 = string;
export type MaxRuns3 = number;
export type MaxTotalTokens3 = number;
export type TimeDriven = boolean;
export type Nudge = boolean;
export type State = boolean;
export type StateQuery = boolean;
export type WorldinfoRead = boolean;
export type HistorySearch = boolean;
export type UpstreamRuns = number;
export type StateRecentRuns = number;
export type SummarizeMode = "manual" | "auto";
export type AskQuestion = boolean;
export type Stream = boolean;
export type Trace1 = boolean;
export type InitialState = {
  [k: string]: unknown;
} | null;
export type ActiveLeafId = string | null;
export type Conversation = Message[];
export type Subgoals1 = SubGoal[];
export type SpentRuns1 = number;
export type SpentTokens1 = number;
export type Paused = boolean;
export type PausedReason = string;
export type StartedAtLeafId = string | null;
export type SeenLeafId = string | null;
export type Id3 = string;
export type Name1 = string;
/**
 * 条目分三类（D158 / D159）。
 *
 * 早先是一条条目**同时**装静态设定和初始状态、共用一个 `mode`。拆开的理由是
 * 可见性天然不同步：一个角色的背景可能几百字、该按需展开，而他「在不在场」
 * 必须每轮都在眼前——共用一个开关时只能迁就其中一个。
 */
export type EntryKind = "world" | "character" | "state";
export type Content1 = string;
export type Voice = string;
export type IsPlayer = boolean;
export type WorldbookMode = "always" | "on_demand";
export type Depth = number;
export type Summary = string | null;
export type Format = string;
export type Version = number;
export type Name2 = string;
export type Note = string;
export type Entries = WorldbookEntry[];

export interface AgentTavern {
  ClientMessage: ClientMessage;
  ServerMessage: ServerMessage;
  Session: Session;
  WorldbookEntry: WorldbookEntry;
  WorldbookBundle: WorldbookBundle;
}
/**
 * 提交一条用户输入，**开新的一轮**（F3a「推进」）。
 *
 * **进队列，每条独立成一个 run，不打包**（D26）——RP 中每个用户动作都需要独立的
 * 世界响应。要插进正在跑的那一轮，用 `Interject`（F3b）。
 */
export interface Submit {
  type?: Type;
  text: Text;
  control_override?: ControlOverride | null;
}
/**
 * 单轮覆盖。全部字段可选；未指定即沿用常驻设置。
 *
 * UI 上应做成**快捷方式**（``/只转述``）而非选择题——显式化的代价边界
 * 在交互形态上，不在维度数量上（D45）。
 */
export interface ControlOverride {
  input_handling?: InputHandling | null;
  model_authority?: ModelAuthority | null;
  expansion_density?: ExpansionDensity | null;
  allow_speaking_for_user?: AllowSpeakingForUser;
  prose_style?: ProseStyle;
  pov?: Pov;
}
/**
 * 把一句话插进**正在跑的**那一轮（F3b / D119）。
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
 * ``selected`` **不校验是否在选项里**（D155）：用户可以三选一，也可以都不选、
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
 * **不是"作废"**（D118）：掐掉在飞的那次操作后，这一轮挂起成草稿，下一条输入
 * 接着它跑。按下停止只说明"别再往下跑了"，没说"刚才那些都不算"。
 *
 * writer / state 阶段没有操作边界（各是一次不可分的生成），那里靠取消整个 task。
 * **掐到哪一段，哪一段就判为失败、产出作废**，已完成的上游存进草稿等续跑
 * （D142，推翻 D60 的「保留正文片段」）——半截正文留着，下一轮会接着那半句
 * 往下写，而用户按停止恰恰是因为不想要它。
 */
export interface Stop {
  type?: Type3;
}
/**
 * 切换活跃路径。**状态随之重放**（D48）——不这样做就会"文本对、状态悄悄错"。
 */
export interface SwitchBranch {
  type?: Type4;
  node_id: NodeId;
}
/**
 * 用户直接编辑节点数据（D65）。
 *
 * **直接改写，不做依赖检查**——乐观假设用户只改最新输出，此时本无下游。
 */
export interface EditNode {
  type?: Type5;
  node_id: NodeId1;
  outline?: Outline;
  prose?: Prose;
  state_set?: StateSet;
}
/**
 * 重生成：在**当前节点**的父节点下新建兄弟（D46/D147）。原分支保留。
 *
 * ``node_id`` 必须是当前活跃叶——**分支只能在当前节点上开**（D147）。想在更早
 * 的地方分叉，先 `SwitchBranch` 切过去，再重生成或推进；**切换不受限制，
 * 受限的只是创建**。带着 id 而不是省掉它，是为了让"你以为的当前节点已经不是
 * 当前节点了"这件事当场报错，而不是悄悄重 roll 掉另一轮。
 */
export interface Regenerate {
  type?: Type6;
  node_id: NodeId2;
}
/**
 * 设一个新目标（D148）。**已有 goal 时报错**——同一时间只有一个。
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
 * 改预算。**这是"加预算接着跑"的入口**（D148）。
 *
 * 和 `SetGoal` 分开，因为它俩的可改时机相反：目标定了就不能动，
 * 预算恰恰要能在跑到一半时调大——预算耗尽是暂停不是失败。
 */
export interface SetGoalBudget {
  type?: Type8;
  max_runs?: MaxRuns1;
  max_total_tokens?: MaxTotalTokens1;
}
/**
 * 暂停 / 恢复 / 放弃当前 goal（D148）。
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
 * 流式分片（D160）：**正文或思考的一小段**，边生成边推。
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
 * 不得污染 trace schema（D25）。事件自带人类可读的 ``label``，
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
 * director 停下来问用户。**不设超时**（D66）——等待期间逻辑上什么都没发生。
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
  control?: ControlDimensions;
  outline?: Outline1;
  outline_summary?: OutlineSummary;
  director_messages?: DirectorMessages;
  closes_segment?: ClosesSegment;
  segment_span?: SegmentSpan;
  prose?: Prose1;
  deviation_report?: DeviationReport;
  state_delta?: StateDelta;
}
/**
 * 一条用户输入。
 *
 * 队列是 FIFO，**每条独立成一个 run，不得打包**（D26）——RP 中每个用户动作都
 * 需要独立的世界响应。把"我推开门"/"我环顾四周"/"我叫了一声"打包会跳过中间
 * 信息，而推开门看到什么本应影响要不要叫。
 */
export interface UserInput {
  text: Text3;
  source?: InputSource;
  submitted_at?: SubmittedAt;
}
/**
 * 每 run 生效的控制维度取值。
 *
 * 由用户显式选择而非模型推断；与输入内容冲突时**显式优先**（D15）。
 */
export interface ControlDimensions {
  input_handling?: InputHandling1;
  model_authority?: ModelAuthority1;
  expansion_density?: ExpansionDensity1;
  allow_speaking_for_user?: AllowSpeakingForUser1;
  prose_style?: ProseStyle1;
  pov?: Pov1;
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
  id: Id1;
  name: Name;
  arguments_raw?: ArgumentsRaw;
  arguments?: Arguments;
  parse_error?: ParseError;
}
/**
 * 一个 run 的状态变化。**权威数据**——当前状态是沿路径重放增量的结果（D48）。
 *
 * 每 run 必须提交，**哪怕为空**（D19）。空增量极便宜；漏提交的代价极高——
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
export interface RunFailed {
  type?: Type15;
  reason: Reason;
}
/**
 * 用户打断了这一轮。**打断是挂起，不是作废**（D118）。
 *
 * `resumable` 为真表示推演已存成草稿：下一条输入会**接在它后面**继续，
 * 而不是从头重来。UI 必须把这件事说清楚——用户以为按的是"取消"，
 * 结果下一句话被接进了同一段推演，那是最难查的一类困惑。
 */
export interface RunInterrupted {
  type?: Type16;
  resumable?: Resumable;
}
/**
 * 当前路径上的完整状态。**派生数据**——权威是各节点的增量（D48）。
 */
export interface StateSnapshot {
  type?: Type17;
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
  type?: Type18;
  pending: Pending;
}
/**
 * 成本可见性：**实时显示消耗，由用户手动打断**，不做自动预算拦截。
 */
export interface UsageUpdate {
  type?: Type19;
  tokens_in: TokensIn1;
  tokens_out: TokensOut1;
}
export interface Branches {
  type?: Type20;
  node_id: NodeId3;
  siblings: Siblings;
  active_index: ActiveIndex;
}
/**
 * goal 走完了，控制权交回用户（D141/D148）。
 *
 * **`outcome` 必须传出去**：两种终态都停下来，但那是两件完全不同的事——
 * 一个是达成了，一个是这个目标已经不可能达成了，用户的下一步动作完全不同。
 */
export interface GoalFinished {
  type?: Type21;
  outcome: Outcome;
  reason?: Reason1;
}
/**
 * goal 又推了一轮之后的进度（D161）。
 *
 * **不推整个 `GoalRun`**：它带着 goal_manager 那份跨 run 的长对话，一轮就上
 * 十万字，而界面一个字都用不上。这里只给要显示的东西。
 *
 * **为什么需要这条消息**：进度只写在盘上的 `goal_run` 里，WS 那侧此前只有
 * 「结束」和「暂停」两条——也就是说 goal 跑着的那几轮，界面上什么都不会变，
 * 直到它突然结束。而中间状态一条条被划掉，恰恰是用户最想看的东西。
 */
export interface GoalProgress {
  type?: Type22;
  subgoals?: Subgoals;
  spent_runs?: SpentRuns;
  spent_tokens?: SpentTokens;
  max_runs?: MaxRuns2;
  max_total_tokens?: MaxTotalTokens2;
}
/**
 * 一个**中间状态**。goal_manager 给出来的，不是用户写的。
 *
 * **它描述状态，不是步骤**，而且彼此之间**没有先后依赖**（D150）：可以被跳过、
 * 以不同顺序实现、同时实现，也可能因剧情变化而不再必要。所以这个列表是一组，
 * 不是一条链——序号只用来指代，不表示顺序。
 */
export interface SubGoal {
  description: Description1;
  done?: Done;
}
/**
 * goal 停下了，**但没有结束**（D148）。
 *
 * 和 `GoalFinished` 分开是因为用户的下一步完全不同：结束了就是结束了，
 * 暂停则是"要不要接着跑"——最常见的来源是预算到顶，把上限调大就能继续，
 * 对话原样留着，不用重新判一遍。
 */
export interface GoalPaused {
  type?: Type23;
  reason: Reason2;
}
export interface ServerError {
  type?: Type24;
  message: Message1;
}
export interface Session {
  id?: Id2;
  created_at?: CreatedAt1;
  meta?: SessionMeta;
  control?: ControlDimensions;
  modes?: SessionModes;
  capabilities?: Capabilities;
  initial_state?: InitialState;
  active_leaf_id?: ActiveLeafId;
  goal_run?: GoalRun | null;
}
/**
 * Session 元指令：**这个世界是什么**。定义目标，不定义过程。
 *
 * 分界线是「换个人来读还成不成立」：题材与基调、尺度、角色语言习惯都是世界
 * 自己的属性，跟着世界设定走，换谁来读都一样；文风和视角不是——它们是读者
 * 这一侧的偏好，所以和铺陈程度一起归 `ControlDimensions`。
 *
 * **没有「否定项」字段**（D139）。不能提前揭示的东西属于世界设定，写进
 * worldinfo 或 state；「不许写什么」这类写作纪律属于提示词，写进 YAML。
 * 单立一个字段等于给同一件事开第三个入口，而它两侧都没人注入。
 */
export interface SessionMeta {
  title?: Title;
  content_scale?: ContentScale;
  genre_tone?: GenreTone;
  character_voices?: CharacterVoices;
  user_character?: UserCharacter;
  goal?: Goal | null;
}
export interface CharacterVoices {
  [k: string]: string;
}
/**
 * 长任务终止条件（D28）。
 *
 * 机制：正常一轮生成结束后检查是否达标，未达标则开启下一 run。
 *
 * **goal 不是模式**——有无 goal 系统跑法一样，只是 run 末多一次 goal_manager 的
 * 调用。达标判定一律交给模型，不降级为代码：那些结构化字段的值本就是模型写的，
 * 代码读取比对不是独立验证（D30）。
 */
export interface Goal {
  description: Description2;
  max_runs?: MaxRuns3;
  max_total_tokens?: MaxTotalTokens3;
}
/**
 * 模式：改变触发源的东西。目前只有一项。
 */
export interface SessionModes {
  time_driven?: TimeDriven;
}
/**
 * 能力开关。**关闭时对应工具不注册**——模型根本看不到它。
 *
 * 这是第四类配置，与元指令（目标）/ 控制维度（处理方式）/ 模式（触发源）都不同：
 * 它决定**系统能做什么**，而不同故事需要的能力差别很大。这与 D74 是同一个判断——
 * 不预设所有故事都一样。
 *
 * 「不注册」比「注册了但拒绝调用」强：后者仍然占 prompt 体量、仍会被尝试、
 * 失败还要消耗一轮往返。工具面越小，模型的决策空间越小（分配原则 P9）。
 */
export interface Capabilities {
  nudge?: Nudge;
  state?: State;
  state_query?: StateQuery;
  worldinfo_read?: WorldinfoRead;
  history_search?: HistorySearch;
  upstream_runs?: UpstreamRuns;
  state_recent_runs?: StateRecentRuns;
  summarize?: SummarizeMode;
  ask_question?: AskQuestion;
  stream?: Stream;
  trace?: Trace1;
}
/**
 * 一次 goal 从创建到结束期间的**运行时状态**（D148）。
 *
 * 和 `SessionMeta.goal` 分开：那个是**用户配置**（目标是什么、给多少预算），
 * 这个是**跑起来之后的东西**（对话、进度、花销）。混在一起的话，"用户改了什么"
 * 和"系统跑出了什么"就分不开，而前者要禁改、后者每轮都在变。
 *
 * **它挂在 session 上，不挂节点。** goal 跑着的时候不允许切分支，所以 D141 让
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
/**
 * 把 goal 拆成的一串阶段（D141/D148）。
 *
 * **它们是脚手架，不是执行契约。** 存在的目的是逼模型在动手之前先想清楚差距
 * 在哪；某一条最终有没有原样实现并不要紧——剧情变了就重给一版。因此没有
 * 「第几版」这种计数，也没有次数上限。
 *
 * 但 `finish(done)` **要求全部标为成立**。这两条不矛盾，合起来才是要点：
 * **重给随时可以，而宣布完成之前必须用「当前的」那一版重核一遍**，不是照着
 * 三轮前那份打勾。
 *
 * **它挂在 session 上**（`GoalRun.plan`），不再挂节点。D141 让它跟着节点走是
 * 为了分支隔离；goal 跑着的时候不允许切分支（D148），那个理由不成立了。
 */
export interface GoalPlan {
  subgoals?: Subgoals1;
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
  depth?: Depth;
  summary?: Summary;
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
