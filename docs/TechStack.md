# 技术栈

> **Python 后端 + TypeScript / React 前端**（D2）。以下库选型的依据是本项目的具体约束，不是通用最佳实践。

## 后端

| 用途 | 选择 | 理由 |
|---|---|---|
| Web 框架 | **FastAPI** | async 原生、WebSocket 内建、与 Pydantic 一体 |
| 并发 | **asyncio**（标准库） | 一个 session 一个 supervisor task；`ask_question` 挂起就是 `await` 一个 future |
| 传输 | **原生 WebSocket**（Starlette） | 见下 |
| 数据校验 / schema | **Pydantic v2** | 工具 schema、协议消息、落盘模型共用一套 |
| LLM 调用 | **`openai` SDK 的 chat completions** | 见下 |
| 配置 | **pydantic-settings**（`.env`） | 与 Pydantic 一体，无需额外的 dotenv 封装 |
| 存储 | **本地 JSON 文件** | 见下 |
| 测试 | pytest + pytest-asyncio | |
| 格式/检查 | ruff（lint + format）+ mypy | |

### 为什么仍然要 WebSocket，即使放弃了流式

D7 放弃逐 token 流式，但这些仍然需要双向通道与服务端主动推送：

- `ask_question` 的挂起 / 恢复（D66，无超时，可能等很久）
- 用户中途排队输入（F3a）
- 长循环下每个 run 完成的推送（无用户请求可搭载）
- agent 工作过程的实时呈现（trace 投影，F1′）
- 时间驱动模式（后续）

SSE 三条都不满足。不用 socket.io——不需要它的房间与 fallback。

### 用官方 SDK，不用 agent SDK

**不用 agent SDK / 编排框架**（LangChain、LangGraph、各家 agent SDK）：本项目对 tool loop 的控制粒度要求很高——run 内暂存区、`submit_outline` 的终止语义、契约校验、分阶段打断（D60）、无超时挂起（D66）。框架会挡在中间。

**但官方 SDK 可以用。** 区别在于它只负责**一次调用**（重试、错误分类、超时、解析），**loop 完全由我们自己写**——SDK 不会挡在中间。省下的是一整套自己写的 HTTP 管道。

只用 **chat completions 这一个面**意味着任何兼容端点都能接：OpenAI、OpenRouter、DeepSeek、本地 llama.cpp / vLLM。配置走 `.env`（`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`），换 provider 不动代码。本地端点常常不校验 key，而 SDK 要求非空，因此空值时填占位串。

**流式可开关**（D160，`Capabilities.stream`，默认开）。`complete()` 两条路返回同一种 `ModelResponse`。
它还接受 `model` 覆盖参数——四个 agent 将来可能用不同模型。

**仍保留一层自己的类型**，理由只有一个但很实在：**工具参数解析失败不抛异常**。OpenAI 格式里 `arguments` 是 JSON 字符串，模型时常输出不合法 JSON。这属于**模型可恢复的错误**，应作为结构化 tool result 回给它重试，而不是让整个 run 崩掉——所以 `ToolCall` 同时保留 `arguments_raw` 与 `parse_error`，这个区分 SDK 的类型表达不了。

另有一处 SDK 不管的：**部分端点在内容被过滤时返回 200 + 空 `choices`** 而非错误码。不在 gateway 拦住，下游会拿到空响应然后在莫名其妙的地方炸。

测试用 `httpx.MockTransport` 注入到 SDK 的 `http_client`，不打真实网络。

### 为什么本地 JSON 文件，不用数据库

**首要理由是用户可直接编辑**——这是 D65（state 必须用户可查看/编辑）的最彻底形式，也是数据主权取向的自然结果。数据库会把这层能力藏起来。

数据模型也天然契合：写入 **单线程**（D6），无并发写；`trace` 是 **append-only**（D8/D50）→ JSONL 逐行追加；`state` 是 **event-sourced 增量**（D49）→ 增量存在节点里，沿路径重放；分支树是 **parent 指针**（D46）→ 每节点一个文件。

**只存权威数据，不存派生快照。** "当前 state"是路径重放的结果（D48），不落盘——否则用户编辑了快照却被重建覆盖，是最糟的体验。用户要改当前状态，实质是改最新节点的 `state_delta`。

写入用 **临时文件 + `os.replace`** 保证原子性（Windows 上同样原子）。

### 目录布局

**两类一级资产，各占一棵目录树**（D170）：设定不属于任何 session，一份能开出好几局故事。

```
data/worldbooks/<worldbook_id>/
  worldbook.json            只有名字——**它没有字段**（D175）
  entries/<entry_id>.json   一条设定一个文件（世界 / 角色 / 初始状态）
                            题材与基调、剧情尺度也是 world 条目

data/sessions/<session_id>/
  session.json              worldbook_id、标题、goal、time_driven、冻结的初始状态
                            **没有配置**——控制维度与能力开关都在提示词里（D174）
  nodes/<node_id>.json      一个 run 一个节点：细纲、正文、state_delta、偏离报告、parent
  trace/<run_id>.jsonl      append-only，每 run 一个文件（匹配归因分析的粒度）
```

session 只存一个 `worldbook_id`，不内嵌拷贝：拷贝的话「改一次、所有故事都跟着」就没了，
而那正是把设定独立出来要买的东西。**共享的代价不需要额外机制**——初始状态在每局故事
自己的第一轮冻结进 `session.json`（D158），其余每一样都是每轮现读的。

规模上限：几百个 run × 每个几 KB，全量读取毫秒级。真到瓶颈再加索引缓存，模型不变。

## 前端

| 用途 | 选择 | 理由 |
|---|---|---|
| 构建 | **Vite** | |
| 状态 | **Zustand** | 大部分状态来自 WS 推送而非请求-响应，不需要 TanStack Query 那套缓存/失效机制；Redux 太重 |
| UI | **Tailwind + shadcn/ui**（Radix 底座） | state 面板、控制维度面板、分支切换器都是标准控件，不值得自己写；Radix 的无障碍处理到位 |
| 正文渲染 | react-markdown | |
| WS | 原生 WebSocket + 薄封装 | 同后端理由 |

**分支不做图形化可视化**：按 D46 参考常见 AI chat，`< 2/3 >` 切换器足够。

## 前后端契约

WebSocket 消息用 **Pydantic 模型定义，导出 JSON Schema 生成 TS 类型**，避免两边手写重复定义漂移。

## 目录

```
backend/agenttavern/
  models/         节点、state 增量、控制维度、goal 运行时、trace 事件
  storage/        JSON 文件、节点树、路径重放
  llm/            gateway（裸 HTTP + OpenAI 格式）
  tools/          design/Tools.md 的实现 + 注册表与调度（四份工具面）
  agents/         director / writer / state_manager / goal_manager
                  + summarizer（摘要）/ postrun（归段追问）/ replay（上下文重建）
  prompting/      提示词 YAML + 装配器。约定见 prompting/README.md
  orchestration/  一个 run 的时序、归段、eval 采样标记
  server/         FastAPI 入口、WS 协议、SessionRunner
  bench/          批量采集、指标、场景与报告
  config.py       .env（Settings）+ config.yaml（Limits）
frontend/src/
docs/
```

### 出站消息必须走同一条队列

trace 事件是从**同步回调**里产生的（`Tracer.on_event`），若各自 `create_task` 去发送就会与生命周期消息乱序——UI 上会看到「本轮结束」排在「开始新的一轮」前面。

因此 `SessionRunner` 把所有出站消息 `put_nowait` 进一条队列，由单个 sender 顺序送出。入队是同步的，顺序即产生顺序。
