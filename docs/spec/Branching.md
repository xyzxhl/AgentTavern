# 分支树与打断

> **回答**：故事树长什么样、在哪儿能开新分支、切过去时状态怎么办、
> 打断一轮之后已经跑出来的东西去哪。
>
> **不回答**：状态自己的形状（见 [State.md](State.md)）、界面怎么呈现
> （见 [UI.md](UI.md)）。

## 全貌

树结构与交互**参考常见 AI chat**：重生成产生兄弟节点，`⟨2/3⟩` 切换，
活跃路径是从根到某个叶子的一条链。

与普通 AI chat 的**唯一关键差异**：一个节点的内容不止文本，
还包括后台维护的数据——所以**切分支必须同时切状态**。

---

### S42 · 状态是路径的函数，不是全局单例

**服务** [R18](../req/回头与改写.md)

**规格** **不存在"当前状态"这个全局对象，只存在"沿某条路径重放到某点的状态"。**
切换分支即重放。细纲历史同理。

实现上可以缓存活跃路径的状态，但那是纯优化，不改变语义。

**不做** 维护一份全局的当前状态。

**为什么** 否则会出现**最隐蔽也最致命**的一类 bug：分支 A 里她知道了真相，切到
分支 B 后系统仍认为她知道。**文本看起来是对的，状态悄悄错了**——而这类错误要等
很多轮之后才以"角色行为不合理"的形式暴露，届时根本查不出源头。

**验收** `test_state_of_a_sibling_never_folds_in` · `test_a_sibling_branch_leaks_into_neither_input_nor_history` · `test_a_sibling_branch_leaks_no_tool_traffic` · `test_base_state_uses_parent_path` · `test_empty_path_replays_to_nothing`

### S43 · 这件事不需要新机制

**服务** [R18](../req/回头与改写.md)

**规格** 每个 run 落一份状态增量（可能是空的），于是状态**天然是增量累积的**。
回到任意节点 = 重放该节点之前路径上的全部增量。

**为什么** 早先这条靠的是一条契约——强制模型每轮至少调用一次写入、空增量也算，
用来区分"确实没变化"与"忘了提交"。**那条契约已经作废**：状态维护后置成独立的
agent 之后，那一步就是它的全部工作，跑完必然有一个增量，**没有"忘了提交"这种
失败模式可言**。

结论不变，依据换了：现在是「结构上必然有」，不再是「靠契约强制有」。

**验收** `test_empty_delta_is_legal_and_noop` · `test_merge_accumulates_like_replay` · `test_state_entries_become_the_replay_base`

### S44 · 分支只能在当前节点上开，切换不受限制

**服务** [R17](../req/回头与改写.md) [R18](../req/回头与改写.md) [R19](../req/回头与改写.md)

**规格** 创建点只有两个，都由**出队那一刻的活跃叶**推出来：

| 动作 | 挂在哪 |
|---|---|
| 普通输入 | 活跃叶下面（那是**延长**，不是分支） |
| 重生成 | 活跃叶的**父**下面（开兄弟） |

**切换不受限**：可以切到任意历史节点。所以任意分叉照样可达，只是要
「先切过去、再重生成或推进」两步走。

在**非当前**节点上重生成时，界面自己走这两步，**但要先问过用户**——
后面几轮会移出当前路径。

**不做** 提交时显式指定任意父节点。重生成非当前节点也**不静默改写**，当场报错。

**为什么** 买到的是**「当前节点」从此有唯一含义**：父节点只有一个来源，
也就没有对不上的余地。悄悄重 roll 掉另一轮，比报错难查得多。

**验收** `test_a_branch_can_only_be_opened_on_the_current_node` · `test_switching_to_an_earlier_node_is_not_restricted`

### S45 · 任何重生成都产生新节点

**服务** [R17](../req/回头与改写.md)

**规格** 包括"只重跑 writer 不重跑 director"的情形——正文变了，偏离报告和据此写下的
状态增量都可能跟着变。

"只换文风不换走向"是一个**成本优化**：复用已产出的细纲，只重跑 writer。
它仍然是兄弟节点，只是生成时跳过了一步，不影响树结构。

**验收** `test_a_branch_can_only_be_opened_on_the_current_node`

### S46 · 兄弟关系随路径一起返回

**服务** [R17](../req/回头与改写.md)

**规格** 取会话时顺带返回一张 `siblings` 映射（只列有兄弟的那些）。
客户端只在重生成落地后重拉一次。

**不做** 一个节点一次「有没有兄弟」的请求。

**为什么** 一条 20 轮的路径会因此打 20 个请求，而这份数据**只在重生成之后才变**——
平常推进落的是独苗，兄弟关系没动。

**验收** `test_session_detail_carries_the_sibling_map` · `test_siblings_endpoint` · `test_siblings_are_ordered_deterministically`

### S47 · trace 全局 append-only，不随分支切换

**服务** [R36](../req/开发者视角.md)

**规格**

| | 性质 |
|---|---|
| 状态、细纲 | **路径的函数**，切换分支即切换 |
| trace | **全局 append-only**，包含所有被放弃的分支 |

**为什么** eval 需要看到全部尝试。用户重 roll 五次，trace 里就有五条——
**这本身就是 eval 信号**（重 roll 率反映"模型是否在写用户想要的东西"），
丢掉等于丢掉最有价值的行为数据之一。

**验收** `test_a_sibling_branch_leaks_no_tool_traffic`

### S48 · 打断是挂起，不是作废

**服务** [R11](../req/推进剧情.md)

**规格** 被截断的推演存成草稿（本轮新增的消息 + 暂存增量 + 已用轮数），
下一条输入接着它跑。**一次打断 + 一次续跑 = 一个节点**，不是两个。

草稿按父节点认领，**不一致就丢弃**（切了分支就接不上）。

**不做** 把打断当成重生成。

**为什么** 三者的差别只在**要不要已完成的部分**与**什么时候插话**：

| | 用户想要 | 已完成的部分 |
|---|---|---|
| 重生成 | 换一个结果 | 不要了 |
| 引导 | 在这个结果之后加一句 | 等它写完，再作为新的一轮 |
| **打断** | 现在就插一句 | **要**——它只是被截在了半路 |

**"用户否决"是替用户加的假设**：按下停止只说明"别再往下跑了"，
没说"刚才那些都不算"。

两处必须做对：尾部那个没收到结果的工具调用要**整条丢掉**（`tool_calls` 没有对应
结果会让端点拒绝**整份**请求，而错误发生在下一轮、看上去与打断无关）；
续跑时注入的状态要用**当前值**而不是被打断那一刻的基线值。

**验收** `test_an_interrupted_deduction_is_suspended_not_discarded` · `test_a_dangling_tool_call_is_trimmed_before_resuming` · `test_the_trimmer_drops_a_call_whose_result_never_arrived` · `test_a_draft_from_another_branch_is_dropped`

### S49 · 打断的单位是阶段，恢复点由下一次输入是不是空的决定

**服务** [R11](../req/推进剧情.md)

**规格** 一个 run 是 `输入 → 细纲 → 正文 → 状态` 三段串行。
**掐到哪一段，哪一段就判为失败、产出作废**，已完成的上游存进草稿。

| 打断在 | 空输入 | 非空输入 |
|---|---|---|
| director | 接着草稿续跑 | 追加进草稿续跑 |
| writer | 只重跑 writer | **回 director** |
| state | 只重跑 state | **回 director** |

**半截正文一律不留**，没有"部分完成"这种节点状态。回 director 时**轮数归零**。

**不做** 保留已生成的正文片段。

**为什么** 判据是**生成内容的依赖关系**：状态依赖正文，正文依赖细纲，细纲依赖输入。
用户又说了话，细纲就可能要改，那下游全都不作数。

留半截正文看似省事，实则是**把一个失败的阶段当成了产出**——下一轮会接着那半句
往下写，而用户按停止恰恰是因为不想要它。原设想的理由「"话说了一半被打断"是真实感的
来源」不成立。

轮数归零是因为：正文阶段被打断后回来交的是**另一份细纲**，那是一次新的推演；
算在原来那一轮头上的话，反复打断几次就能把硬上限提前吃光。

**验收** `test_interrupt_during_director_lands_no_node` · `test_interrupt_during_writer_drops_the_half_written_prose` · `test_interrupt_during_state_keeps_outline_and_prose` · `test_an_empty_input_reruns_only_the_writer` · `test_a_new_input_goes_back_to_the_director` · `test_going_back_to_the_director_starts_the_round_count_over` · `test_a_half_written_turn_becomes_a_draft_not_a_node`

### S50 · 引导与打断的单位是一次操作，不是一轮

**服务** [R10](../req/推进剧情.md)

**规格** 插入点是**操作边界**：一次模型调用或一次工具调用做完就是一个边界。
两个动作只差在飞的那次操作怎么处理——**引导让它做完，打断掐掉它**。
两者都不开新的一轮，**都只产出一个节点**。

边界只有三处：一轮开始前、纯文本回复之后、**一次响应的全部工具结果都补齐之后**
（`tool_calls` 和它的结果之间塞不进用户消息）。

掐掉一次工具调用时要**补一条"被打断"的工具结果**，否则消息序不合法。

判"用户喊没喊停"**不能只看有没有掐到东西**——信号和操作在赛跑，操作先跑完时没
东西可掐，但用户明明按了停止。

**不做** 把引导理解成"排队，等这一轮写完"。

**为什么** 那样的话，用户看着它往错方向查了六次资料，也得等整段正文写完才说得上话
——**那不叫引导，那是事后补救**。

**验收** `test_guidance_lands_between_two_operations_not_after_the_turn` · `test_an_interrupt_kills_only_the_operation_in_flight` · `test_an_interrupt_with_nothing_to_say_suspends_the_run` · `test_an_aborted_tool_call_still_gets_a_result`

### S51 · 会话循环等 run 用 `asyncio.wait`，不 `await` 它

**服务** —（内部契约）

**规格** 取消当前 run 时，`CancelledError` 不能打到会话循环身上——循环必须活下来
等下一条输入。用 `asyncio.wait`：它不传播子任务的取消，而自身的取消照常抛出。

**不做** `await task` 再 `except CancelledError: pass`。

**为什么** 吞掉之后**循环就再也取消不掉了**——断连时清理永远等不到，
WebSocket handler 挂死。加旗标区分"是谁在取消"只是把不死性从所有人缩小到一个人，
仍然错。

**验收** `test_stop_cancels_only_the_run_not_the_loop` · `test_the_loop_is_cancellable_by_anyone_not_only_by_aclose` · `test_aclose_returns_while_a_run_is_in_flight` · `test_aclose_returns_when_idle`

---

## 未定

- **分支清理策略**：无限保留会膨胀，但用户可能想长期保留对比线。
  倾向不自动删除，交用户管理并给出容量提示；**不采用"只留最近 N 个"的自动淘汰**。
  **要什么才能定**：真实使用下的分支数量分布。
- **快照的频率与保留策略**。路径长时逐条重放代价高，标准做法是定期快照 + 增量补齐。
  **要什么才能定**：一条几百轮的路径重放实测耗时。
