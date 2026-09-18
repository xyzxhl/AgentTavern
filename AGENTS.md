# AgentTavern 协作约定

本文件作用于整个仓库。它只收纳跨模块、长期稳定、会直接影响实现正确性的规则；具体产品行为仍以 `docs/spec/` 为准。

## 开始工作前

1. 先读 `docs/README.md` 与 `docs/Workflow.md`。
2. 按任务读取对应的 `docs/req/` 与 `docs/spec/`；提出新设计时再读 `docs/Principles.md`。
3. 查 `docs/TODO.md`，确认相关规格是否已知缺实现、缺测试或仍未定。
4. `docs/Ideas.md` 只是候选方向，示例小说、SillyTavern 预设与文风技能只是参考材料，都不是需求或规格。
5. 若 req、spec、测试、实现或不同文档互相冲突，先把冲突报告出来，不凭直觉静默选一边。

文档链路是：

```text
用户故事 -> req（用户要什么） -> spec（系统做什么）+ test（如何证明） -> code
```

- `spec` 是当前系统行为的权威；若它与 req 冲突，应修正文档链路，而不是只让代码“服从其中一份”。
- `R<n>`、`S<n>`、`P<n>` 永不复用、永不重排。废弃条目保留编号，写明现行替代方案。
- spec 条目应有：服务的需求、规格、不做什么、为什么、验收。写不出可失败的验收时，先判断它是否其实是需求、论证、想法或人工评估规则。

## 产品与拓扑不变量

- AgentTavern 是全新实现的 agent 化 AI 角色扮演系统，不背负 SillyTavern / TauriTavern 兼容约束。
- 正常剧情生成始终走 agent 路径，不保留绕过它的“直接生成”快路径。
- 一条从队列出队的顶层 user input 开启一个 turn；turn 内纠正或引导不另开 turn，
  重生成只为同一 turn 创建新版本。`run = node version = 一份细纲 = 一份正文`；
  完整 run 在故事树上产生一个原子节点，节点还包含状态增量与偏离报告。未完成的 run
  只能是草稿，不能伪装成节点。
- 默认数据流固定、单向：

```text
director -> writer -> state_manager -> goal_manager（仅有 goal 时）
```

- director 是主 agent，负责推演与细纲；它读细纲历史、worldinfo、state、用户输入与用户编写的静态开场，但绝不读 writer 生成的节点正文或文风。
- writer 是无工具的单次渲染调用，只把细纲落成正文；它不承担剧情规划。
- state_manager 在正文完成后运行，是 state 的唯一模型写入者；director 只能查询 state。
- goal_manager 只在长任务中存在，跨 run 维护目标产品记录与进度。不要让 goal 语义渗入 director；自主推进应通过模拟输入走和普通输入相同的生成路径。
- agent 之间不做动态 handoff、协商回流或自行生成子 agent。writer 也不能做成 director 的工具，否则正文会回流并破坏上下文隔离。
- 流式输出只是旁路；节点落盘后的完整结果才是权威，客户端必须能用最终结果整体替换流式片段。

## 数据边界与历史

- `worldinfo` 是用户维护的静态设定；`state` 是剧情推进形成的当前世界状态；`history` 是沿故事路径累积的细纲历史。不要混用这三个词。
- state 是 history 的检索捷径，不是第二份剧情记录。存储层不预设时间、地点、人物属性等固定维度；值保持任意嵌套 JSON。
- 当前 state 是“初始状态 + 当前路径上的节点增量”的重放结果，不是全局单例，也不是独立权威快照。切换分支必须同时切换并重放 state 与 history。
- worldbook、corpus、session 都是一级资产：worldbook 与 corpus 可被多个 session 引用，session 只保存引用，不复制资产。
- worldbook 的初始状态在第一轮开始时冻结进 session，成为所有分支的共同重放起点；其它静态设定每轮现读。
- worldbook 的开场白是可选候选；创建 session 时明确选择并复制，显示在第一个 turn 前但不占节点。既有故事不随候选改写。
- 「用户扮演谁」属于 worldbook，不是 session 覆盖项；复用同一 worldbook 的 session
  共用同一个玩家角色。
- trace 全局 append-only，不随分支切换；默认不落盘，开发者模式下按 run 完整落盘。
- director 的跨 turn history 每轮只保留「顶层 user input → 按顺序的插话 → 细纲」；
  goal 跨 turn 只保留产品记录与当前有效 plan。thinking、tool call/result 不进入两者。
- 只持久化权威数据。缓存、索引、摘要和当前 state 都应能由其来源重建，不能反过来成为真相。

## 分支、输入与中断

- 顶层输入按 FIFO 逐条消费，每条独立开启一个 turn，不得打包；该 turn 的每个完成版本
  各有一个 run / node。排队输入只在当前 turn 完成后开始。
- turn 内插话分两种：纠正会取消当前操作再追加，引导会让当前操作完成并在下一个合法
  边界追加。两者都留在当前 turn，只产生一个节点。
- 用户写出的动作是锚点，不能被省略或偷换；动作结果由世界规则决定，失败也必须在正文里可见且有据。
- 重生成总是创建兄弟节点，不覆盖旧节点。切换到任意历史节点时，所有路径相关数据一起切换。
- 停止生成表示挂起当前 run，不表示否定已经完成的上游工作。恢复后仍只落一个节点。
- 被打断阶段的半成品作废；尤其半截正文不能进入节点。工具调用与工具结果必须保持协议配对。
- thinking、tool call/result 与运行时错误的生命周期只到当前 turn：运行中留在内存，
  挂起时为恢复而持久化进草稿；turn 完成、放弃或失败后不进入节点或后续 context。
  开发者 trace 是独立的审计旁路，不是模型上下文来源。
- 只允许直接编辑当前活跃路径最后一个已完成 turn，且只能在它完成后、下一条顶层
  user input 成功提交前编辑。有 run 正在执行或挂起时一律禁止编辑。编辑不重跑 agent、
  不重算后代、摘要或缓存；可能返回旧值的派生缓存必须失效，但不立即重建。细纲、正文、
  状态之间的冲突由用户负责。

## 模型、代码与检索的边界

- 代码只判断形状、协议、计数和其它可机械验证的事实，不判断自然语言内容是否合理。
- 正则和阈值默认是采样器或诊断信号，不是内容裁判。不要用字数、模板命中、风格指标等硬门禁迫使模型牺牲质量。
- 规则可以覆盖全集后降低精度，但不能静默筛掉模型不知道存在的信息。折叠项必须留下可用地址与体量，并能按地址展开。
- state、worldinfo、history 的展开必须全量返回；若完整结果超出上下文，整个 turn 明确
  失败，不分页、不截断、不静默降级，也不留下可恢复草稿或节点。
- 外部 corpus 检索是明确的例外：它是尽力而为的增强，不承诺查全；“这次检索没找到”不能写成“资料里没有”。
- 工具描述只写作用和参数格式。行为判据只保留在对应 YAML 中；同一判据不要在工具描述、提示词和代码里复制多份。
- 可由模型修正的工具调用错误应作为工具结果返回，让当前 loop 恢复，不要把整轮直接抛掉。
- state_manager 的模型调用、协议或存储失败使原子 run 整轮失败；不能降级成“空增量”落节点。
- corpus 的检索降级只进诊断 trace，不进入模型回执，也不在正常用户界面逐次显示。

## 提示词与配置

- `backend/agenttavern/prompting/*.yaml` 与 `backend/agenttavern/prompting/sets/*/*.yaml` 的提示词正文属于用户。除非任务明确要求，不要顺手润色、改写或“统一语气”。结构、顺序、开关和文件头说明可以按规格修改。
- 凡新增且可能进入后续 context 的**产品内容**，都必须有明确归属和可审查入口；
  不要在 Python/TypeScript 中新藏一份重复提示词。thinking、tool call/result 与运行时错误
  是只读执行记录，不在这项可编辑承诺内。
- 一份 agent 文件描述该 agent 收到的全部可编排内容。`text`、`context`、`choice` 三类条目必须显式标型；一个 section 对应一条消息。
- 提示词按完整套装管理，不做稀疏覆盖或跨套装继承。当前套装是服务级选择，在 turn 开始时随全部配置一起冻结。
- 未知 context、缺失变量、非法 role、无效 selected 等配置错误应尽早、明确失败，不能静默跳过或悄悄回退。
- 模型、流式、额外调用参数与所有 choice 按 agent 存在 YAML 中，是服务级配置；
  不提供 `.env` 全局模型/流式兜底，也不提供 session 覆盖。
- turn 开始时冻结全部 agent/call 配置；运行中与草稿恢复沿用快照，保存修改从下一 turn 生效。
- 一次只改一个提示词、装配规则或参数。提示词实验要先量自然方差，再比较分布；同一批素材、固定上文、干净判定上下文，避免把噪声写成结论。

## 修改与验证纪律

- 用户直接改 req、spec、test 或 code 时，沿链向上、向下各检查一层，并报告孤儿 req、孤儿 spec 与野生代码；不要自动掩盖缺口。
- 改任何持久化 schema（字段、枚举值、目录形状）时，同一个改动必须加入迁移；迁移入口是 `Store.migrate()`。
- `frontend/src/protocol.ts` 是由后端 Pydantic 协议生成的文件，不得手改。协议变化应先改 `backend/agenttavern/server/protocol.py`，再运行类型生成。
- 写入继续保持单线程；文件替换保持原子。出站 WebSocket 消息统一同步入队，由单一发送者维持产生顺序。
- 当前目标是单用户本地服务：同一 session 同时只允许一个 WebSocket/runner 连接。
- 不把随机模型实验用于证明纯代码不变量。路径隔离、消息形状、状态重放等应使用确定性测试；创作质量必须保留人工评估。
- 优化与排错按数据流从上游到下游：state 写入 -> 检索 -> director -> writer -> 可选能力。上游错误常会伪装成 writer 问题。

提交前分别检查每一步的退出码，不把失败藏进管道：

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy agenttavern

cd ..\frontend
npm run lint
npm run build
```

只修改文档时，至少检查链接、编号、术语和上下游文档是否仍一致；不要用当前测试数量、一次实验结果或阶段性完成度充当长期规则。
