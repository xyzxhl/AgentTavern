# AgentTavern

基于 agent 的 AI 角色扮演（airp）系统。全新实现，不 fork SillyTavern / TauriTavern。

**两个创新点**：

- **(a) 用 agent 技术提高生成质量** —— 主动检索、显式状态维护，而非 prompt 拼接。
- **(b) 探索 airp agent 化后的新用户体验** —— 非轮次式方向。这不是确定的设计，而是探索方向；判据是"用户体验是否真的变了"，而非表面上是否还有 run 的概念。

## 一分钟版本

顶层输入进队列，**逐条**消费；每条开启一个 turn。turn 内的纠正和引导不另开 turn，
重生成只为同一 turn 产生另一个版本。每个版本对应一个 run，它这样跑：

```
director（主 agent，tool loop）
    读：正文历史 + 世界设定 + state 默认包 + 用户指令 + 元指令 + 控制维度
    产出：一篇**不受文风约束的文章**（细纲）
        ↓  固定、单向的静态 workflow 交接
writer（纯渲染，单次调用）
    读：细纲 + 正文上文 + 文风 + 角色语言习惯
    产出：正文
        ↓
state_manager（tool loop，run 末）
    读：全量状态 + 前 k 轮正文 + 本轮正文
    产出：状态增量
```

**状态维护是第三个 agent，而且后置**：它只从**已经写出来的正文**提取状态变化，
不把 director 的计划当成已经发生的事实。正文也是 director、goal_manager 与摘要系统
跨轮读取的唯一叙事权威；细纲只在当前 turn 内把规划交给 writer。

有 goal 时还有第四个：**goal_manager**。它跨整个 goal 生命周期累积产品记录，在 run 末
替用户写下一条输入、核对进度、判目标去向；thinking 与工具往返仍只活在当前 turn。

director / writer 仍按「决定发生什么 / 决定怎样写」分工，但两者都可以读有限的正文上文：
director 为延续剧情，writer 为保持现场与行文一致。writer 必须从正文已经到达的位置继续，
不能复述上文。

每个 run 是一个 turn 的节点版本；它在 turn 内产生一份细纲和一份正文，完成节点只保存
正文与状态等权威结果，不保存细纲。重生成会为同一 turn 增加兄弟版本，而不是增加一个
turn。节点构成分支树，**状态是路径的函数**——
切换分支必须同时重放状态，否则会出现“文本对、状态悄悄错”这类最隐蔽的 bug。

## 跑起来

仓库根的 `dev.ps1` 一条命令起前后端（后端 8000、前端 5173，日志打在同一个控制台，Ctrl+C 一起停）：

```powershell
.\dev.ps1 -Setup     # 第一次：建 venv、装 pip 与 npm 依赖，再起
.\dev.ps1            # 之后
```

然后打开 <http://127.0.0.1:5173>。

**先填 `backend/.env`**（`copy backend\.env.example backend\.env`，填端点与
`LLM_API_KEY`）。模型名和流式开关按 S159 分别住在各 agent / 单次调用的 YAML 中，
没有 `.env` 里的全局模型兜底。任何 OpenAI 兼容端点都可以接。

端口写死在 `frontend/vite.config.ts`（监听 5173、代理目标 8000），要换在那一处换。**别只换一边**：漏掉代理那条不会报错，Vite 会把请求当成前端路由回一份 `index.html` 且状态码 200——而 `res.ok` 为真，异常拖到解析才发生（S135）。

## 文档

三层：**[req](req/) 用户要什么 → [spec](spec/) 系统做什么 → `backend/tests/` 怎么证明它做到了。**

| | |
|---|---|
| [Workflow.md](Workflow.md) | **改动怎么落地**：三层怎么保持一致、什么时候停下来问。**动手前先读这份** |
| [req/](req/) | **54 条需求**（R1–R54），按用户场景分七篇 |
| [spec/](spec/) | 编号已到 **S176**，按机制对象分十五篇；撤销条目保留编号。每条带服务的 R、不做什么、为什么、验收 |
| [Principles.md](Principles.md) | **36 条判据**（P1–P36）：从 req 推 spec 时用的工具箱 |
| [Glossary.md](Glossary.md) | **术语**。每一条都对应一次实际的澄清 |
| [Ideas.md](Ideas.md) | **还没成形的想法**。层次在 req 之上，想清楚了就下沉成 req |
| [TODO.md](TODO.md) | **三层对不齐的地方**。允许存在，但不许藏在正文里 |
| [Optimization.md](Optimization.md) | 怎么改提示词与装配规则，以及怎么知道改对了 |
| [TechStack.md](TechStack.md) | 库选型与理由、目录结构、前后端契约 |

### spec/ 十五篇

| | |
|---|---|
| [Architecture.md](spec/Architecture.md) | 四个 agent 的拓扑、一个 run 的时序、归因六类 |
| [Generation.md](spec/Generation.md) | director / writer 的职责边界：规划与呈现分开，正文是两侧共同的连续性依据 |
| [Tools.md](spec/Tools.md) | 四份工具面，以及**明确不提供**的部分 |
| [State.md](spec/State.md) | 为何不预设任何维度；记什么、不记什么 |
| [Branching.md](spec/Branching.md) | 分支树、"状态是路径的函数"、打断与续跑 |
| [Retrieval.md](spec/Retrieval.md) | 规则检索的准入标准、全量索引 + 按需展开、跨轮上下文 |
| [Corpus.md](spec/Corpus.md) | 外部资料检索。**尽力而为的一层**——它是唯一不满足「无损降维」的检索路 |
| [Worldbook.md](spec/Worldbook.md) | 设定库：条目分类、两路投影、交换格式 |
| [Prompting.md](spec/Prompting.md) | 提示词层：三类条目、所见即所得、档位取值住哪 |
| [Prose.md](spec/Prose.md) | 文风落成系统行为的那四条 |
| [Goal.md](spec/Goal.md) | 长任务：跨 run 的产品记录、中间状态、预算 |
| [InputHandling.md](spec/InputHandling.md) | 不做显式分类；要避免的四种失败 |
| [UI.md](spec/UI.md) | 界面形态与信息架构 |
| [Trace.md](spec/Trace.md) | trace 与开发者模式 |
| [Session.md](spec/Session.md) | 一局故事本身存了什么 |

## 状态

阶段性完成度、实现缺口、缺失测试和仍需实验的数据统一维护在 [TODO.md](TODO.md)。
README 不记录测试数量或某次手工验证结果，避免阶段性状态变成长久过期的第二份台账。

## 参考项目

[TauriTavern](https://github.com/Darkatse/TauriTavern) —— SillyTavern 的 Tauri/Rust 移植。

**借鉴**：生成即工作区编辑（一次生成不是返回字符串，而是一组可审计可回滚的编辑）；三层身份（会话 / 工作区 / 单次执行）；append-only journal 为唯一真相、UI 从日志派生；工具结果绝不进 chat messages。

**不采用**：其 run 是 request-scoped 的（一个 run = 一条消息），与非轮次方向冲突；14 crate 的 Clean Architecture 分层对本项目规模是负债（其自身文档把"改一处需同步十份文档"列为风险第一位）；约一半复杂度来自 SillyTavern 兼容包袱。

**反面数据点**：它的 workspace 是文件系统而非事实模型——18 个内置工具里没有任何 entity / timeline / relationship 查询，一致性依赖模型自行在 `scratch/` 里记自然语言笔记。这条路走到底就是"模型自己写 markdown 备忘录"。
