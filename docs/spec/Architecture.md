# 拓扑与一轮的时序

> **回答**：系统由哪几个 agent 组成、它们按什么顺序跑、一轮产出什么。
>
> **不回答**：为什么这么拆（见 [Generation.md](Generation.md)）、每个工具怎么用
> （见 [Tools.md](Tools.md)）、状态怎么存（见 [State.md](State.md)）。

## 全貌

```
编排层（代码，静态 workflow）
  ├─ director（主 agent，tool loop）
  │     产出：细纲 —— 一篇不受文风约束的文章
  │        ↓  固定、单向的交接
  ├─ writer（纯渲染，单次调用）
  │     产出：正文 + 偏离报告
  ├─ state_manager（tool loop，读刚写完的正文）
  │     产出：状态增量
  └─ goal_manager（跨 run 累积产品记录，仅长任务）
        产出：下一条输入 / 目标终态
```

每 run 三次主调用（director 的 tool 往返算一次、归段追问、goal_manager 另计）。

---

### S1 · 一个顶层输入开启一个 turn，每个版本各有一个 run / node

**服务** [R7](../req/推进剧情.md)

**规格** 一条从队列出队的顶层 user input 开启一个 turn；turn 内的纠正与引导不另开
turn。turn 是逻辑位置，重生成只为它创建兄弟版本。每个完成版本各有一个 run，落成
故事树上恰好一个节点。每个 run 在 turn 内产生一份细纲和一份正文；细纲是交给 writer
的临时制品，完成节点只保存正文、状态增量与偏离报告。

**不做** 一个 run 产出两个节点，或两个 run 合并成一个节点。打断加续跑仍然只是
同一版本、**一个**节点。

**为什么** 这条一一对应是分支、重放、归因三件事共同的地基——树上每个位置有唯一
的一份产物，状态才能沿路径重算。

**验收** `test_full_run_produces_a_node` · `test_a_half_written_turn_becomes_a_draft_not_a_node`

### S2 · 节点是原子的，增量和正文必须同时抵达

**服务** [R20](../req/回头与改写.md) [R22](../req/回头与改写.md)

**规格** state_manager 卡在**落盘之前**跑。正文与状态增量一起写进节点，不存在
中间态；细纲只在当前 turn 内传给 writer，不写入完成节点。

模型调用、协议或存储失败使整个 run 失败，不落一个缺状态增量的节点。工具参数或校验
错误仍作为 tool result 返回 state_manager，让当前 loop 自行修正。

**不做** 先落正文、后补增量。

**为什么** 放到落盘之后会出现一个「有正文没增量」的瞬时节点，那一刻沿路径重放状态
的人（切分支、看历史状态）会算错。代价是这一轮的结束晚一次模型调用，而用户此时
已经看完正文了。

**验收** `test_interrupt_during_state_keeps_outline_and_prose` · `test_staged_delta_is_not_persisted_until_run_ends` · `test_a_completed_node_does_not_persist_the_outline` · `test_a_system_failure_fails_the_atomic_turn` · `test_invalid_tool_arguments_return_to_the_agent_for_repair`

### S166 · provider 没有完整结束的响应使整轮失败

**服务** [R9](../req/推进剧情.md) [R51](../req/开发者视角.md)

**规格** OpenAI 兼容响应只有 `stop`、`tool_calls` 或兼容旧端点的 `function_call` 算完整。
`length`、`content_filter`、其它非正常结束原因，以及流式连接结束却没有最终
`finish_reason`，都抛成模型调用失败，由 runner 走原子整轮失败。

**不做** 把截断的正文、半套工具调用或只有 thinking 的响应交给下游；也不在应用层
自动重试整轮——SDK 对瞬时请求错误的重试与用户主动重试是两层不同语义。

**为什么** thinking 后直接断流此前会被解析成一次合法的空回答，表象可能一路漂成
director 不提交、writer 空正文，甚至只剩「连接关闭」，原始原因反而消失。

**验收** `test_truncated_response_fails_instead_of_becoming_partial_output` · `test_stream_ending_without_finish_reason_is_reported_as_interrupted`

### S3 · director 是主 agent，writer 是从它身上拆下来的渲染器

**服务** [R7](../req/推进剧情.md)

**规格** director 拿走全部推演职责：读正文历史、世界设定、状态、用户指令，产出细纲。
writer **只承担文风**，不做任何规划。

**不做** 把 writer 当成流水线下游的规划环节，给它剧情决定权。

**验收** `test_director_never_sees_style` · `test_director_replays_previous_prose` · `test_each_side_gets_its_own_sections_and_none_of_the_others`

### S4 · 正常交接是静态 workflow；用户插话可使下游作废并回 director

**服务** [R7](../req/推进剧情.md)

**规格** 没有用户插话时，编排层按固定顺序调用，没有 agent 自发回流、协商或重试循环。
writer / state 运行期间收到 turn 内插话时，当前下游产物作废，携带原输入、既有推演与
插话回 director；这是用户控制流，不是 agent 动态 handoff。

**不做** 让 writer / state_manager 自己把结果退回 director 重议。

**验收** `test_each_side_gets_its_own_sections_and_none_of_the_others` · `test_guidance_after_director_returns_to_director_in_the_same_turn`

### S5 · writer 绝不能做成 director 的一个工具

**服务** [R7](../req/推进剧情.md)

**规格** writer 由编排层调用，不出现在 director 的工具面里。

**不做** `render(细纲) -> 正文` 这样一个看起来很自然的工具。

**为什么** writer 是 director 完成后必经的单次渲染阶段，不是可选能力。把它做成工具
会让 director 决定是否、何时、调用几次，还会把同一 run 的成文结果带回规划 loop，
混淆两侧职责并引入递归改稿路径。

**验收** `test_writer_is_not_in_the_director_tool_registry` · `test_each_side_gets_its_own_sections_and_none_of_the_others`

### S6 · 正文是跨轮唯一叙事权威，细纲只活在 turn 内

**服务** [R7](../req/推进剧情.md)

**规格** 完成节点不保存细纲。director 的跨 turn history 每轮只重放「顶层 user input
→ 按顺序的插话 → 正文」；state_manager、goal_manager 与归段摘要的叙事输入也只取
正文。细纲只在当前 turn 内从 director 交给 writer；挂起时可随草稿持久化供同一 turn
恢复，开发者 trace 可留作审计，但两者都不成为节点或后续 context。

**不做** 把细纲与正文并列成两份跨轮故事记录；不在正文缺失时拿细纲冒充已发生剧情。

**为什么** 计划与成文发生冲突时，只有正文是用户实际读到、也是世界实际发生的版本。
让下游或下一轮读取细纲会把未落成的计划重新当成事实，形成两套叙事真相。

**验收** `test_replay_keeps_only_input_interjections_and_prose` · `test_state_manager_reads_only_prose_as_narrative` · `test_goal_manager_reads_prose_before_and_during_the_goal` · `test_a_completed_node_does_not_persist_the_outline`

### S7 · state_manager 后置到 writer 之后，读着正文去记

**服务** [R22](../req/回头与改写.md)

**规格** 状态维护是 writer 之后的一次独立调用。它拿到当前状态全量、前 k 轮正文与
本轮正文。工具只有
`state_write` 和 `state_query`，**没有终止工具**——收口条件是「这一次不再调用任何工具」。

工具调用参数不合法属于可恢复错误，回给 agent；agent 本身的调用、协议或存储故障属于
整轮失败，不降级成空增量。

**不做** 让 director 边推演边记状态。

**为什么** 细纲常常留白或在渲染时发生偏离，不能证明某件事已经写进故事。当前状态
提供既有键形状，正文提供本轮唯一事实来源；把旧增量或细纲再塞进去只会引入第二份
可能冲突的叙事依据。

**验收** `test_it_reads_the_prose_it_just_wrote` · `test_the_write_side_belongs_to_the_state_manager_alone` · `test_the_director_cannot_touch_state` · `test_invalid_tool_arguments_return_to_the_agent_for_repair` · `test_a_system_failure_fails_the_atomic_turn`

### S8 · writer 输出偏离报告，不改写细纲

**服务** [R20](../req/回头与改写.md)

**规格** writer 若为通顺而增删了实质内容，单出一份**偏离报告**（通常为空）。
编排层把它记在节点上供开发者审计，不注入后续模型 context。

**不做** 让 writer 回头改写细纲。

**为什么** 后续 agent 直接以正文为准，不需要用偏离报告修补事实；报告仍用于把
「规划错了」与「writer 没按规划落成」区分开。让 writer 报告而不是改写细纲，是为了
保住它「纯渲染」的定位。

**验收** `test_deviation_report_is_extracted`

### S9 · 提示词装配器是独立组件，管的是「每轮第一次给模型的输入」

**服务** [R34](../req/调教提示词.md)

**规格** 边界按**时机**划：**管**这一轮发给模型的第一份消息列表；**不管**之后
loop 边跑边追加的东西（模型回复、工具调用、工具结果、编排层催促、用户插话）。

产物是**一串消息**而不是一整块 system 文本，`sections` 的一个条目就是一条消息，
`role` 写在条目上。

**不做** 把轮内追加的内容也交给它编排——那些的位置由协议决定（工具结果必须紧跟
它的调用），编排不了也不该编排。

**验收** `test_sections_order_comes_from_yaml` · `test_a_new_section_needs_only_yaml` · `test_each_section_is_one_message_and_carries_its_own_role` · `test_a_section_without_a_role_raises`

### S10 · 可编排的产品文案归装配器，执行流量不归

**服务** [R31](../req/调教提示词.md)

**规格** 当前 turn 内动态产生的工具调用、工具结果与模型推理段属于执行记录和协议流量，
不归装配器，也不是用户可编辑的产品内容。它们只在当前 run 中存在，挂起时进草稿以便
恢复；run 完成后不进入节点或后续 context。其余会进入后续 context 的产品文案归装配器。

工具的返回文案和错误消息**不进 YAML**——它们是工具实现的一部分。

**为什么** 这条线一划，「哪些提示词该进 YAML」就不再需要逐个争论。

**验收** `test_reasoning_and_tools_only_persist_in_a_draft` · `test_empty_section_is_skipped_message_and_all`

### S11 · 工具清单不进消息

**服务** [R31](../req/调教提示词.md)

**规格** 提示词里不该有一节「可用的工具」。工具走 API 请求的 `tools` 字段，
模型本来就看得见完整的名字、描述和参数 schema。

**为什么** 曾经有这么一节，理由是「让『有哪些工具』在顺序里看得见、位置可编排」。
**那条理由建立在一个错误前提上**：工具根本不在 `messages` 里。于是那一节是同一份
清单发了两遍——多花一份体量，还多一处会和真工具面对不上的地方。

这类错误的形状值得记：**一个决定的理由是对的，但它要解决的问题不存在。**

**验收** `test_the_tool_list_is_not_a_message`

### S12 · 归因分六类

**服务** —（内部诊断用，不直接服务某条 req）

**规格** director 检索不足 / director 推演错 / writer 偏离细纲 / writer 文风失败 /
写入遗漏 / **写入冗余**。六类都能从 trace 直接判定，因为每一步的输入输出都物化了。

**为什么** 归因的可行性是 director/writer 拆分的副产品——中间量物化了才分得清
「规划错了」和「执行错了」。合并方案下无法区分。

第六类是补的：原本五类里写入侧只有「遗漏」没有「多余」，于是"记了不该记的"无人负责。
**冗余的代价是慢性的**（状态越长越容易看漏要紧的几条），不会以单点错误的形式暴露。

**验收** `test_attribution_classes_cover_all_six` ·
`test_attribution_table_excludes_the_class_with_no_referent`
