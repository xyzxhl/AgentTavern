# 长任务（goal）

> **回答**：设一个目标之后系统怎么自己往下跑、怎么判进度、怎么停。
>
> **不回答**：界面上那条进度条长什么样（见 [UI.md](UI.md)）、
> goal_manager 的工具签名（见 [Tools.md](Tools.md)）。

## 全貌

goal_manager 是第四个 agent，跨整个 goal 生命周期累积一份**产品记录**。
静态前缀不变，每轮只追加一条「第 N 轮发生了什么」；本轮内模型自己决定调哪几个工具，
但 thinking 与工具往返不会跟到下一轮。

它**不读正文之外的东西**：不读世界设定、不读状态——它管的是「推到哪儿了」，
不是「这一轮怎么写」。

---

### S92 · 目标累积产品记录，不保留跨 turn 执行流量

**服务** [R24](../req/长任务.md)

**规格** 四个目标工具：`plan`（首次拆**以及任何一轮就地重拆**）、`check`（更新成立情况）、
`steer`（写下一条输入）、`finish`；历史发生折叠时还可用只读的 `history_read`。
收口条件是「这一次不再调用任何工具」。

静态前缀跨整个 goal 生命周期不变，**每轮只追加一条产品记录**；与 director 使用同一
段落边界，完成段直接折成摘要，并可由 `history_read` 按段展开，不以字符数触发。
当前有效的 plan 与
进度以权威状态单独持久化，并在每次 goal_manager 调用前投影到上下文末尾。当前 turn 的
thinking、tool call/result 只在这次 loop 内存在；turn 结束后不追加到 goal 历史。跨 turn
保留的是工具已经写入的权威 goal 状态，不是调用与回执的 transcript。

**不做** plan-then-execute（三次干净调用）。

**为什么** 那一版**对剧情偏移无解**：「剩下的中间状态还成不成立」只在全部走完之后
才有机会被问到——**偏了之后还要抱着一份垮掉的计划推好几轮**。

累积产品记录真正买到的是**每轮只追加一条**：以前每次重发全量剧情梗概，
梗概越来越长、前缀每轮失效。

**代价写在明处**：模型仍读自己的计划、自己判的进度，
**比干净上下文更容易往「已经达标」的方向读**。实测里它两次把一个逻辑上不可达的
目标判成达成，靠的是给设定编一个解释。

**验收** `test_the_prefix_is_built_once_and_each_turn_only_appends` · `test_a_round_ends_when_it_stops_calling_tools` · `test_a_round_that_never_stops_calling_tools_is_cut_off` · `test_the_current_plan_is_injected_without_being_persisted` · `test_product_records_and_plan_survive_a_round_trip_through_disk`

### S93 · 拆出来的是中间状态，不是有先后的阶段

**服务** [R25](../req/长任务.md)

**规格** 它们描述**状态**不描述步骤，**可以被跳过、以不同顺序实现、同时实现，
也可能因剧情变化而不再必要**。唯一的硬要求是「达成与否能被看出来」。

进度回执**列出全部尚未成立的**，不指定下一个。重拆**清空进度**。

**不做** 挑出一个说「当前这个」。

**为什么** 挑一个等于**凭空造了个顺序，而模型会照着那个顺序推**。

中间状态是**脚手架，不是执行契约**——它们存在的目的是逼模型动手前先想清楚路径，
某一条最终有没有原样实现并不要紧。

**验收** `test_progress_is_reported_as_what_is_left_not_as_a_next_one` · `test_replanning_resets_progress` · `test_checking_without_a_plan_is_recoverable`

### S94 · `finish` 不检查中间状态是否全部成立

**服务** [R25](../req/长任务.md) [R29](../req/长任务.md)

**规格** 宣布完成不需要先把每一条标全。该由提示词担保的事交给提示词。

**为什么** 这里加过一条硬前提，想靠它挡住自我确认倾向。但中间状态无序、可跳过、
可能已经不必要——于是在「目标确实达成、只是某条中间状态已经作废」这个**常见情形**下，
它只是多逼出两次工具调用（先重给一版、再标全），然后原样通过。

**挡不住真问题，只收了个过路费。**

**验收** `test_finish_does_not_check_the_subgoals` · `test_finish_failed_needs_no_such_precondition`

### S95 · 核对与引导是两个工具

**服务** [R25](../req/长任务.md)

**规格** `check` 更新成立情况（**只观察**），`steer` 写下一条输入（**干预**）。

**不做** 合成一个。

**为什么** 合成一个时每轮必须两件一起做：没有新成立也得报一次进度，
不想干预也得写一条输入。**而这两件事本来就独立**——核对是观察，引导是干预，
触发条件和频率都不一样。重给一版中间状态的那一轮不必核对；
「不进行实质性引导」也是一档合法选择。

**验收** `test_progress_is_reported_as_what_is_left_not_as_a_next_one`

### S96 · 终态只有两种，预算耗尽是暂停

**服务** [R29](../req/长任务.md)

**规格** `done` / `failed`。**预算耗尽不是终态，是暂停**——用户可以把上限调大
接着跑。

因此预算**可改**而目标描述**禁改**（对话是围着它建的），两者可改的时机恰好相反。
goal_manager 的系统失败也暂停 goal：若故事节点已经原子提交，保留该节点，不把后置判定
失败改写成故事 run 失败；首次规划/判定前先建立 `GoalRun`，保证暂停能持久化。

**不做** 把「重新规划」当成一种结局；不做把「完全偏离」单列一种。

**为什么** 完成与完全偏离**都交回控制权，但一个是达成了、一个是已经不可能达成了，
用户的下一步动作完全不同**——所以必须把是哪一种传出去。而「重新规划」是动作不是结局。

**验收** `test_finishing_comes_back_as_an_outcome` · `test_a_finished_goal_says_which_way_it_ended` · `test_a_spent_budget_is_a_pause_not_a_failure` · `test_a_spent_budget_pauses_instead_of_finishing` · `test_raising_the_budget_resumes_a_paused_goal` · `test_a_first_goal_failure_is_persisted_as_paused`

### S97 · 预算留空就是不设上限

**服务** [R28](../req/长任务.md)

**规格** 两条预算留空一路带到底，判定时跳过没设的那条。两条都没设时它永远不喊停。
下界钉死在 1。

协议上有两处**刻意的不对称**：设目标时的空**是在表态**（不设上限）；
改预算时的空**只是没提到它**（这条不动）——所以「改成无上限」另给一个标志。

**不做** 留空时填一个默认值；不做接受 0。

**为什么** 不填**不是「随便给个数」，是「别拦我」**。想止损的人本来就会填一个数；
不填的人却会在第 50 轮被一个自己没见过的数字拦下来，**而那时的症状和故障长得
一模一样**。

`0` 表达不了任何意图（一设上就是「已经跑满」），**而它最可能的来源正是前端把空输入
框转成了数字**。

**验收** `test_an_empty_budget_means_no_budget` · `test_a_zero_budget_is_rejected_rather_than_silently_pausing`

### S98 · 预算算的是这个目标花掉的全部

**服务** [R28](../req/长任务.md)（"目标花的钱要算全"）

**规格** 把它开出来的每一轮生成的开销一并计入，不只是判定那几次调用。
暂停期间不算——那时候是用户自己在写。

**为什么** 钱主要花在它开出来的 run 上（细纲 + 正文 + 状态）。实测一轮全场约
17 万 token，**判定只占零头**——于是一个名义上「一百万 token」的上限
**实际拦不住任何东西**，等于把轮数上限架空。

预算计数还必须**落盘**：曾经它是内存里的实例字段，**进程重启就归零**，
那条成本上限从来没生效过。

**验收** `test_goal_spending_is_counted_and_lands_on_disk` · `test_a_goal_scenario_stops_when_the_budget_runs_out`

### S99 · 设目标就是「去做」，包括空会话

**服务** [R24](../req/长任务.md)

**规格** 设完目标当场开跑。恢复、加完预算解除暂停同理。

实现上要两件事一起做：拨开自动续跑的开关，**并往队列里放一个唤醒信号**——
循环这时多半停在取队列上。信号出队时**再确认一次**（排队期间目标可能已被放弃或跑满）。

**不做** 只在一次 run 正常结束之后才打开自动续跑。

**为什么** 那条规则挡的是「带目标的会话一连上就自己开始生成」——
**连接不是指令，而设目标是**。用户按下去的意思就是「去做」。

否则在空会话上设完目标什么都不会发生：界面举着「目标推进中」一动不动，
直到用户随便打句话把它推起来。

**验收** `test_setting_a_goal_plans_first_then_runs` · `test_no_goal_means_no_autocontinue` · `test_goal_drives_another_run_with_a_simulated_input`

### S100 · 开场先规划，再动手

**服务** [R25](../req/长任务.md)

**规格** 目标驱动的那一轮**在 director 之前**先跑一次 goal_manager：
拆中间状态、写出开场那条输入，然后才进生成。

**开场不计一轮**（它还没开出任何 run 来），token 照算。
开场的记录**挂在它开出来的那一轮名下**。

**不做** 让第一轮空手推。

**为什么** director **从来不读 goal**（模拟输入和真输入在它眼里必须一模一样），
所以最该定方向的第一轮完全不知道有目标这回事；goal_manager 要等它写完才第一次开口，
然后**照着一段随机开的剧情去拆阶段**。

恢复走同一条路，好处更明显——暂停期间用户自己写的那几轮**在恢复的那一刻就交接
过去了**，而不是先空手推一轮。

**验收** `test_setting_a_goal_plans_first_then_runs` · `test_a_goal_set_mid_story_also_plans_before_it_runs` · `test_a_goal_set_at_session_start_treats_everything_as_during`

### S101 · 设目标那一刻正在跑的那一轮，不算目标的

**服务** [R28](../req/长任务.md)

**规格** 起点直接划到它身上：它和它之前的剧情一样进静态前缀、**给正文**，
目标从**下一轮**开始。开场那次留给下一轮去跑。

**不做** 判它、把它的开销记进预算、按正文逐条追加给 goal_manager。

**为什么** 三样都是在**为一件目标没参与的事结账**——那一轮的输入是用户自己打的，
正文是目标存在之前写成的，判它「有没有推进某个中间状态」仍是在为目标未参与的剧情
结账，
而预算兜的是自主运行成本，**它一分钱都不是自主跑出来的**。

**验收** `test_a_goal_set_while_a_run_is_in_flight_survives_that_run`

### S102 · 起点前后都只给正文，分段只决定缓存位置

**服务** [R25](../req/长任务.md)

**规格** 起点之前的剧情进静态前缀，只给正文；起点之后的每一轮逐条追加最终正文。
用户亲自提交的顶层输入与 turn 内纠正/引导，是用户接管方向的证据，也随目标期间的
正文保留；goal_manager 自己写出的模拟输入已经落实成正文，不重复回灌。

两段都按 [S157](Retrieval.md#s157) 与 director 共用同一上下文预算、段落摘要和
`history_read` 地址。**正文没写成时不回退到细纲**，明说「这一轮没有写出正文」。

**为什么** 无论是在 goal 之前交代背景，还是在 goal 期间判断进度，都必须以实际成文
为准。细纲只是当时计划写什么，不能证明最终发生了什么。

用户介入是它**最重要的信号**：剧情偏了，是自己推错了还是用户主动把剧情带走了？
**后者强得多，用户的意图优先于目标。** director 仍把普通输入与模拟输入按同一种
剧情输入处理；这个来源差异只服务于 goal 恢复后的判断。

回退看起来只是「降级用一份更粗的记录」，实际上**把一个失败悄悄补齐了**：
那一轮什么都没写出来，而 goal_manager 读到一段像模像样的剧情，
会照着它判某个中间状态已经成立。**静默的降级比缺失更糟。**

**验收** `test_before_and_during_the_goal_both_get_prose` · `test_user_interventions_survive_but_simulated_inputs_do_not_echo` · `test_a_turn_without_prose_says_so_instead_of_falling_back` · `test_a_goal_set_at_session_start_treats_everything_as_during` · `test_goal_history_compacts_like_director_and_can_expand`

### S103 · 恢复时能续就续，续不上就整份重建

**服务** [R30](../req/长任务.md)

**规格** 权威在**投影**不在对话。暂停期间用户可能自己跑几轮、还可能切分支——
**切了分支就整份重建**（「以最终选择的分支为准」只有重建做得到，增量追加撤不回来）。

恢复时，暂停期间最终选中路径上的正文与用户亲自提交的顶层输入、纠正和引导一起补入；
模拟输入不补。正文若已折叠，用户方向仍保留，具体剧情可用 `history_read` 展开。

重建丢掉工具调用的历史，但计划与进度以投影的形式留着。

**不做** 无条件续接。

**验收** `test_switching_branches_rebuilds_instead_of_extending` · `test_product_records_and_plan_survive_a_round_trip_through_disk`

### S104 · goal 跑着时不允许切分支；进度挂 session 不挂节点

**服务** [R26](../req/长任务.md)

**规格** 对话与进度都挂在 session 上。未暂停的 goal 存在时拒绝切换分支；暂停后可以
切换，恢复时按 [S103](#s103) 从最终选择的路径重建产品记录。

**为什么** 曾经让它跟着节点走是为了分支隔离，**而 goal 跑着的时候本来就不允许切分支**
——那份隔离没有用武之地。

**验收** `test_an_active_goal_blocks_branch_switching` · `test_a_paused_goal_neither_judges_nor_continues` · `test_abandoning_is_the_only_way_to_swap_a_goal` · `test_abandoning_during_judgement_leaves_nothing_behind`

### S105 · goal_manager 要知道用户扮演谁

**服务** [R24](../req/长任务.md)

**规格** 「用户扮演谁」也注入 goal_manager，没设时整节不出现。

**为什么** 实测它写出了「我从外套内袋里摸出那张照片」，第一人称写对了——
**但那是从种子输入的措辞里猜的**，种子恰好是第一人称才蒙对。

没设时整节不出现是为了**退回去猜，而不是凭空冒出一个名字**。

**验收** `test_it_is_told_who_the_user_plays` · `test_it_does_not_read_the_world_or_the_goal`

### S160 · 单独暂停 goal 从下一轮生效，不截断已经开始的轮次

**服务** [R30](../req/长任务.md)

**规格** goal 处于运行态时，一轮从目标开场开始就归当前 goal。用户只按 goal 的暂停时，
当前轮仍完整跑完：故事节点照常提交，run 末的目标核对照常完成，这轮的产品记录、轮数与
token 照常记账；核对没有给出终态时保留“用户暂停”，并且不自动开启下一轮。

用户按「停止生成」或发送纠正时，当前故事 run 分别按 S48 / S50 处理，同时立即把 goal
置为暂停并关闭自动续跑；这些动作不是本条所说的“只暂停 goal”。

目标在一轮开始前已经暂停时，用户手写的轮次仍按 S104 处理：不判目标、不计入目标预算。

**不做** 取消当前模型调用；不做因为暂停而跳过当前轮的目标核对；不让 goal_manager
用开始调用时拿到的旧副本覆盖用户刚写入的暂停状态。

**为什么** 暂停表达的是“这一轮之后先别继续”，不是“把正在写的节点撕掉”。只保留故事
节点却跳过核对与记账，会出现界面已经多了一轮、goal 仍显示 `0/N` 的分裂状态。

**验收** `test_pausing_during_goal_opening_finishes_and_accounts_for_that_run` · `test_a_paused_goal_neither_judges_nor_continues` · `test_stop_and_correction_pause_an_active_goal`

### S176 · 节点提交后的中断仍须结算并暂停 goal

**服务** [R28](../req/长任务.md) [R30](../req/长任务.md)

**规格** 若故事节点已经原子提交，随后在归段、摘要或其它后处理阶段收到中断，本轮不再
回滚或伪装成未完成。编排层仍把本轮 run 数与 token 记入当前 goal，但不再发起新的模型
判定；随后暂停 goal、关闭自动续跑，并把新节点设为当前可编辑节点。

**不做** 恢复上一节点的编辑窗口；不让一个已经存在的故事节点从 goal 预算中消失；
不在连接已断时继续反复尝试发送同一批通知。

**为什么** 节点是故事侧的提交边界。越过这条边界后再把 run 当成失败，会出现正文已经
可见、goal 轮数却没增加的分裂状态；继续判定又可能需要已断开的交互链路，暂停后交还
控制权更明确。

**验收** `test_post_commit_cancellation_accounts_and_pauses_the_goal`
