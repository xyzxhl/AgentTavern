# AgentTavern

基于 agent 的 AI 角色扮演（airp）系统。全新实现，不 fork SillyTavern / TauriTavern。

**两个创新点**：

- **(a) 用 agent 技术提高生成质量** —— 主动检索、显式状态维护，而非 prompt 拼接。
- **(b) 探索 airp agent 化后的新用户体验** —— 非轮次式方向。这不是确定的设计，而是探索方向；判据是"用户体验是否真的变了"，而非表面上是否还有 run 的概念。

## 一分钟版本

用户输入进队列，**逐条**消费，每条产出一个 run。一个 run 是这样跑的：

```
director（主 agent，tool loop）
    读：细纲历史 + 世界设定 + state 默认包 + 用户指令 + 元指令 + 控制维度
    产出：一篇**不受文风约束的文章**（细纲）
        ↓  固定、单向的静态 workflow 交接
writer（纯渲染，单次调用）
    读：细纲 + 正文上文 + 文风 + 角色语言习惯
    产出：正文
        ↓
state_manager（tool loop，run 末）
    读：全量状态 + 前 k 轮的（细纲 + 正文 + 当时记下的）+ 本轮细纲与正文
    产出：状态增量
```

**状态维护是第三个 agent，而且后置**：它读的是**已经写出来的正文**，不是 director 的计划——细纲只说定了什么，正文才是最终落成的东西。writer 的产出由此回流进状态，而这不违反下面那条地基，因为它跨了 run 边界、且经过一次有损压缩（正文折成键值）。

有 goal 时还有第四个：**goal_manager**，一份跨整个 goal 生命周期的长对话，在 run 末替用户写下一条输入、核对进度、判目标去向。

**director 从不读正文**——这是整个拓扑的地基。它因此能在同样的 token 预算下覆盖数倍跨度的历史。一旦正文回流到它的上下文（比如把 writer 做成它的一个工具），隔离就没了，全部论证作废。

**run = node = 一份细纲 = 一份正文**，四者一一对应。节点构成分支树，**状态是路径的函数**——切换分支必须同时重放状态，否则会出现"文本对、状态悄悄错"这类最隐蔽的 bug。

## 跑起来

仓库根的 `dev.ps1` 一条命令起前后端（后端 8000、前端 5173，日志打在同一个控制台，Ctrl+C 一起停）：

```powershell
.\dev.ps1 -Setup     # 第一次：建 venv、装 pip 与 npm 依赖，再起
.\dev.ps1            # 之后
```

然后打开 <http://127.0.0.1:5173>。

**先填 `backend/.env`**（`copy backend\.env.example backend\.env`，填 `LLM_API_KEY` 与 `LLM_MODEL`）。任何 OpenAI 兼容端点都行。不填也起得来——但要到第一次真调模型才 401，那时报错离原因已经很远，所以 `dev.ps1` 在启动前就拦掉。

端口写死在 `frontend/vite.config.ts`（监听 5173、代理目标 8000），要换在那一处换。**别只换一边**：漏掉代理那条不会报错，Vite 会把请求当成前端路由回一份 `index.html` 且状态码 200——而 `res.ok` 为真，异常拖到解析才发生（S135）。

## 文档

三层：**[req](req/) 用户要什么 → [spec](spec/) 系统做什么 → `backend/tests/` 怎么证明它做到了。**

| | |
|---|---|
| [Workflow.md](Workflow.md) | **改动怎么落地**：三层怎么保持一致、什么时候停下来问。**动手前先读这份** |
| [req/](req/) | **48 条需求**（R1–R48），按用户场景分八篇 |
| [spec/](spec/) | **154 条规格**（S1–S154），按机制对象分十五篇。每条带服务的 R、不做什么、为什么、验收 |
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
| [Generation.md](spec/Generation.md) | director / writer 拆分的**论证**：两侧都读正文时不可分，换一刀就可分 |
| [Tools.md](spec/Tools.md) | 四份工具面，以及**明确不提供**的部分 |
| [State.md](spec/State.md) | 为何不预设任何维度；记什么、不记什么 |
| [Branching.md](spec/Branching.md) | 分支树、"状态是路径的函数"、打断与续跑 |
| [Retrieval.md](spec/Retrieval.md) | 规则检索的准入标准、全量索引 + 按需展开、跨轮上下文 |
| [Corpus.md](spec/Corpus.md) | 外部资料检索。**尽力而为的一层**——它是唯一不满足「无损降维」的检索路 |
| [Worldbook.md](spec/Worldbook.md) | 设定库：条目分类、两路投影、交换格式 |
| [Prompting.md](spec/Prompting.md) | 提示词层：三类条目、所见即所得、档位取值住哪 |
| [Prose.md](spec/Prose.md) | 文风落成系统行为的那四条 |
| [Goal.md](spec/Goal.md) | 长任务：一份跨 run 的长对话、中间状态、预算 |
| [InputHandling.md](spec/InputHandling.md) | 不做显式分类；要避免的四种失败 |
| [UI.md](spec/UI.md) | 界面形态与信息架构 |
| [Trace.md](spec/Trace.md) | trace 与开发者模式 |
| [Session.md](spec/Session.md) | 一局故事本身存了什么 |

## 状态

**后端基本完成**，可独立运行（506 项测试，ruff / mypy strict 全绿）。四个 agent、分支与挂起、三项可选能力（goal / summarize / ask_question）都在真实模型上跑通过。剩余为三类：

1. **需要实验数据**的两项——文风的结构层能否留在 writer 侧、细纲的粒度下限。二者是同一张力的两端，须一起测。**后者实测已成问题**：`chapters` 12 轮里 `ratio_avg` 只有 1.21、最低 0.70（正文比细纲还短），细纲里出现了描写。
2. **仍开放的设计**——实体级检索的"信息需求在推演中才产生"这个鸡生蛋没解决，只是从代码手里交给了模型。
3. **前端已实现，只有资料库那一页在浏览器里走过**——会话、分支与回头、设定库、提示词管理、全局设置的验证靠的是 TestClient、真实服务加 curl、`tsc` 与构建，**没有人工点过**。资料库点过一遍（明暗两套主题），当场揪出三处只有看得见才发现的问题——**那三处类型检查和接口测试全都发现不了**。
   开发期的 Vite 代理表现在由 `tests/test_dev_proxy.py` 双向对账：漏一条前缀，那一页会拿到 `index.html` **且状态码 200**，而生产构建里一切正常——**所以它能坏很久没人发现**。`/agents` 改名成 `/prompts` 之后就这么坏过好几个提交。

三处**已知未验证**：摘要的"越界后换成摘要"从未被真实模型触发过（12 轮才用掉 `budget_chars` 的 15%）；`ask_question` 在刻意造出的岔路上 9 次只问 3 次；渐进式披露在几百实体下的规模表现。

## 参考项目

[TauriTavern](https://github.com/Darkatse/TauriTavern) —— SillyTavern 的 Tauri/Rust 移植。

**借鉴**：生成即工作区编辑（一次生成不是返回字符串，而是一组可审计可回滚的编辑）；三层身份（会话 / 工作区 / 单次执行）；append-only journal 为唯一真相、UI 从日志派生；工具结果绝不进 chat messages。

**不采用**：其 run 是 request-scoped 的（一个 run = 一条消息），与非轮次方向冲突；14 crate 的 Clean Architecture 分层对本项目规模是负债（其自身文档把"改一处需同步十份文档"列为风险第一位）；约一半复杂度来自 SillyTavern 兼容包袱。

**反面数据点**：它的 workspace 是文件系统而非事实模型——18 个内置工具里没有任何 entity / timeline / relationship 查询，一致性依赖模型自行在 `scratch/` 里记自然语言笔记。这条路走到底就是"模型自己写 markdown 备忘录"。
