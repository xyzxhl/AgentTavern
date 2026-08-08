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
```

**director 从不读正文**——这是整个拓扑的地基。它因此能在同样的 token 预算下覆盖数倍跨度的历史。一旦正文回流到它的上下文（比如把 writer 做成它的一个工具），隔离就没了，全部论证作废。

**run = node = 一份细纲 = 一份正文**，四者一一对应。节点构成分支树，**状态是路径的函数**——切换分支必须同时重放状态，否则会出现"文本对、状态悄悄错"这类最隐蔽的 bug。

## 文档

| | |
|---|---|
| [Glossary.md](Glossary.md) | **术语**。每一条都对应一次实际的澄清 |
| [Principles.md](Principles.md) | **方法与判据**。用于做决定的东西，与具体设计分开 |
| [Decisions.md](Decisions.md) | **决策台账**。按主题分组，编号稳定；含已被推翻的结论 |
| [Optimization.md](Optimization.md) | **怎么改提示词/工具/装配规则**，以及怎么知道改对了 |
| [TechStack.md](TechStack.md) | 库选型与理由、目录结构、前后端契约 |

### design/

| | |
|---|---|
| [Architecture.md](design/Architecture.md) | 组件拓扑、一个 run 的时序、职责覆盖表、归因五类 |
| [Generation.md](design/Generation.md) | director / writer 拆分的**论证**：为什么两侧都读正文时不可分，换一刀就可分 |
| [Configuration.md](design/Configuration.md) | 元指令 / 控制维度 / 模式 / 能力开关 |
| [InputHandling.md](design/InputHandling.md) | 为何不对输入做显式分类；要避免的失败模式 |
| [State.md](design/State.md) | 为何取消固定核心 schema；代码能做什么、不能做什么 |
| [Retrieval.md](design/Retrieval.md) | 规则检索的准入标准；渐进式披露；写入侧靠用户兜底 |
| [Prose.md](design/Prose.md) | 文风怎么量：参考文本、诊断指标、**只产数不判分**的铁律，以及量出来之后改了哪几行提示词 |
| [Tools.md](design/Tools.md) | director 的工具清单，以及**明确不提供**的部分 |
| [Branching.md](design/Branching.md) | 分支树；"状态是路径的函数" |
| [NonTurnBased.md](design/NonTurnBased.md) | 创新点 (b)：非轮次形态的生成器与统一机制 |
| [Optional.md](design/Optional.md) | 真实性与戏剧性的取舍、强度旋钮 |

## 状态

主干设计闭合，后端可独立运行（107 项测试）。剩余为三类：

1. **需要实验数据**的两项——文风的结构层能否留在 writer 侧、细纲的粒度下限。二者是同一张力的两端，须一起测。
2. **仍开放的设计**——实体级检索的"信息需求在推演中才产生"这个鸡生蛋没解决，只是从代码手里交给了模型。
3. 前端。

## 参考项目

[TauriTavern](https://github.com/Darkatse/TauriTavern) —— SillyTavern 的 Tauri/Rust 移植。

**借鉴**：生成即工作区编辑（一次生成不是返回字符串，而是一组可审计可回滚的编辑）；三层身份（会话 / 工作区 / 单次执行）；append-only journal 为唯一真相、UI 从日志派生；工具结果绝不进 chat messages。

**不采用**：其 run 是 request-scoped 的（一个 run = 一条消息），与非轮次方向冲突；14 crate 的 Clean Architecture 分层对本项目规模是负债（其自身文档把"改一处需同步十份文档"列为风险第一位）；约一半复杂度来自 SillyTavern 兼容包袱。

**反面数据点**：它的 workspace 是文件系统而非事实模型——18 个内置工具里没有任何 entity / timeline / relationship 查询，一致性依赖模型自行在 `scratch/` 里记自然语言笔记。这条路走到底就是"模型自己写 markdown 备忘录"。
