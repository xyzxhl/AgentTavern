# 工具面

> **回答**：谁拿到哪几个工具、每个工具的签名与约束、明确不提供什么。
>
> **不回答**：检索为什么是这个模式（见 [Retrieval.md](Retrieval.md)）、
> 状态怎么存（见 [State.md](State.md)）、goal 怎么推进（见 [Goal.md](Goal.md)）。

## 全貌

| agent | 工具 |
|---|---|
| director | `submit_outline` · `state_query` · `history_*` · `worldinfo_*` · `corpus_search` · `ask_question` |
| writer | **一个都没有**，单次调用 |
| state_manager | `state_write` · `state_query` |
| goal_manager | `plan` · `check` · `steer` · `finish` |

---

### S18 · 工具名必须匹配 `^[a-zA-Z0-9_-]+$`

**服务** —（接口硬约束）

**规格** 名字里不能有点号或其它字符。

**为什么** 点号会让**整个请求**被 400 拒掉，不是某个工具不可用——最初的
`state.query` 这类命名在第一次真实调用就撞上了。错误发生在请求层，看上去与工具无关。

**验收** `test_tool_names_match_the_api_pattern`

### S19 · `submit_outline` 是终止工具，调用即结束这一轮

**服务** [R7](../req/推进剧情.md)

**规格** `submit_outline(outline: str)`。调用即结束 loop 并把细纲交给 writer。
**它只做终止与交付，不兼管状态提交**，也**没有任何前置条件**。

**不做** 要求"提交前必须先调用过 `state_write`"。写侧已经整个搬到 state_manager
手里，这个前置条件失去了立足点——现在"没有变化"就是一次也不调。

**验收** `test_submit_outline_always_exists` · `test_submit_outline_has_no_precondition` · `test_submit_outline_never_has_a_precondition`

### S20 · 状态的读写分属两个 agent

**服务** [R22](../req/回头与改写.md)

**规格** `state_query(path)` 两个 agent 都有；`state_write(set, append, remove)`
**只有 state_manager 有**。写入进 run 内暂存区，run 完整结束时一并提交。

**不做** 给 director 任何写状态的工具。

**验收** `test_the_write_side_belongs_to_the_state_manager_alone` · `test_the_director_cannot_touch_state` · `test_state_writes_accumulate_into_one_delta` · `test_staged_delta_is_not_persisted_until_run_ends`

### S21 · 工具面按开关增减，关掉的工具不注册

**服务** [R14](../req/推进剧情.md)

**规格** 关闭一项能力时，它的工具**根本不注册**，而不是注册了再拒绝。
**没有对象可操作时同样不注册**：世界设定全是"一直在"时 `worldinfo_read` 不注册，
一份外部资料都没挂时 `corpus_search` 不注册（[S145](Corpus.md)）。

**不做** 注册一个只会返回"该功能已关闭"的工具。

**为什么** 后者仍占提示词体量、仍会被尝试、失败还要消耗一轮往返。
没有可展开的东西时留着工具只是白占体量。

**三个展开工具各自按自己的开关注册**（`state_query` / `worldinfo_read` /
`history_read`）。它们共用一份实现（[S157](Retrieval.md#s157)），但**工具面上
是三个**——这正是不做成一个 `expand(域, 路径)` 的理由：历史关掉时它的展开工具
要跟着消失，而一个统管三个域的大工具只能"注册着但有一半用不了"。

**验收** `test_each_switch_removes_its_tools` · `test_worldinfo_tools_only_when_something_is_expandable`

### S22 · 工具分「生成中调用」与「生成后调用」，后者绝不进 loop

**服务** [R16](../req/推进剧情.md)

**规格** 判据是**影不影响本轮产出**。生成中：`state_query`、`worldinfo_read`、
`history_read`、`ask_question`。生成后：归段追问（`close_segment`）。

生成后的动作**必须问 director 本人**，接在它自己的对话后面。
`close_segment` 因此**不在任何一个 agent 的工具面里**——它只挂在追问那一次
独立调用上，`build_tools()` 一次都不注册它。

**答案是一次工具调用**：`close_segment(after)` 收一个 typed int，
**不调用就是「先别断」**。

**不做** 把归段追问塞进 tool loop；**不做**让模型回一个数字。

**为什么** 它对本轮产出毫无帮助，却要占往返、分散注意力，还和 `submit_outline`
的终止语义打架。而另起一个只看得见几段细纲的冷调用，判据全靠猜——判的是"这场戏
完了没有"，需要状态、前情和故事走向。

**答案不走自由文本**，是因为原来那一版从回复里抓第一串数字：模型多写一句
「我认为断在第 2 条」，抓到的就是别的数。而抓错的后果**不可逆且不出声**——
标记打在没人指的那一条上，那几轮被压成一段，没有任何地方会报错。
这是 [P10](../Principles.md) 的同一条：**受约束的选择胜过在自由文本里找结构。**

**验收** `test_segment_closes_when_the_director_says_it_closed` · `test_open_segment_waits_another_run` · `test_the_cut_comes_from_a_tool_call_not_from_free_text` · `test_close_segment_is_in_no_tool_face` · `test_the_followup_prompt_comes_from_the_yaml`

### S23 · `ask_question` 是「题干 + 恰好三个选项」

**服务** [R12](../req/推进剧情.md)

**规格** `ask_question(question, options[3])`。三个选项必须是**三条不同的发展方向**，
不是同一条路的三种措辞。

用户可以都不选、自己写一条，**服务端不校验**回答在不在选项里。但工具回执必须
**分开写**：「用户选择：…」vs「用户没有选给出的选项，自己回答：…」。

**不做** 留一个「其他」选项位；不做 2–4 的区间。

**为什么** 两个把岔路压成二选一，四个开始出现凑数的那条。校验等于把"模型没想到的
第四条路"判为非法，而那恰恰是最该传下去的一种回答。合成一句回执会让模型照着
"这是我列过的选项"去理解一句它根本没列过的话。

**验收** `test_a_question_takes_exactly_three_options` · `test_an_answer_outside_the_options_reads_differently` · `test_ask_question_awaits_user`

### S24 · 它问得多不多由控制权决定，不设超时

**服务** [R13](../req/推进剧情.md)

**规格** 模型剧情控制权三档，直接写进 `ask_question` **自己的描述**里
（"这一档不要调用它"这种话就该由工具自己说）。**是倾向不是硬计数**，
但调用频率在 trace 里可观测、各档可校准。

等待用户回答**不设超时**：等待期间逻辑上什么都没发生，上下文不会过期，
从中断点续跑是安全的。

**为什么** 长循环、多候选、交互式创作本来就是同一机制的三个频率档位——
一套机制加一个参数，不是三种模式。

**验收** `test_ask_question_awaits_user`

### S25 · 压缩归编排层，模型没有触发它的工具

**服务** [R16](../req/推进剧情.md)

**规格** 摘要**只由编排层生成**：轮次滑出全文窗口、且那一段已经标了结尾时，
`close_segment()` 调一次 `summarize_text`（`prompting/summarize.yaml`，
属于「单次调用」那一类，[S147](Corpus.md)）。

**没有 `max_chars`**，只统计：归段事件带 `chars_in` / `chars_out`，
**看压缩比不看绝对字数**。

**摘要只有一种**：history 压缩出来的那一段（`Node.outline_summary`）。
设定条目**没有摘要**——它是 title → content，索引里给名字和体量。

**不做** 给 director 一个 `summarize` 工具；不做预先批量生成；不做字数上限；
不做在设定条目上缓存摘要。

**为什么** 这一条原先是一个工具（`summarize(source)`，`source` 是 `run:<id>`
或 `wi:<ref>`，懒惰生成 + 系统缓存）。**它删掉了**：判定与压缩本来就不同步
（[S58](Retrieval.md)：判定在生成时，压缩在需要时），而一个能主动触发压缩的
工具等于请模型去动一件**它管不着、也看不见后果的事**——压缩不可逆，而它
判断不了这一段将来还会不会被读到。实测 12 轮里它一次都没被调用过。

条目那侧的缓存跟着一起删：`summary` 字段的唯一写者就是那个工具，留着它就是
一个永远为 `None` 的字段，读代码的人会以为它有值。**而且一个词不能指两样
东西**——「摘要由谁生成」这个问题在两种含义之间会一直错位。

字数上限实测**说了不算而且没有任何地方会发现**：48 条落盘摘要里 16 条超标，
最坏 779 字 / 3.9 倍，且超出幅度随输入长度增长。**一个说了不算的数比没有更糟**——
它让"压缩到 200 字"看起来是既成事实，于是整套预算账全是虚的。

**验收** `test_every_segment_goes_through_summarize` · `test_close_segment_is_in_no_tool_face` · `test_export_drops_the_id`

### S26 · 真截断必须出声，隐藏不算截断

**服务** [R31](../req/调教提示词.md)

**规格** 两件事要分开——

| | 是什么 | 处置 |
|---|---|---|
| **隐藏** | "这个值太长，用 `state_query` 展开" | 无损降维，留着 |
| **真截断** | 砍掉尾巴或整条不返回 | **必须告诉模型丢了什么** |

**真截断现在只剩一处**：前情索引里没归过段的那几轮给的是"细纲前 N 字"，
那要写出**全文字数**，不能只加一个省略号——那是模型决定要不要展开的唯一依据。

**读取预算和分页那两条已经撤销**（[S157](Retrieval.md#s157)）：展开就是**全给**。
那两套话术（"这几条一个字都没返回""已读到第几字、接着读传什么"）是在这一层猜
"多少算多"的补丁，而猜出来的预算本身就是一次静默降级——模型不知道它本来能看到
更多。真的超了上下文是**整轮失败**，那是一声响，不是这一层能补救的事。

**为什么** 模型拿到一份看起来完整的结果却不知道自己丢了什么，
那和按相关性筛选子集是同一条罪。**静默的降级比缺失更糟**——读的人不知道拿到的是次品。

**验收** `test_an_index_entry_says_how_much_it_left_out` · `test_expanding_gives_everything_no_budget_no_paging`

### S27 · 工具描述只说「作用 + 参数格式」

**服务** [R31](../req/调教提示词.md)

**规格** 判据（该记什么、判定从严到什么程度、怎么拆中间状态）**全在 YAML 提示词里**，
描述里一个字都不重复。

**唯一的例外**是 `ask_question` 的调用积极性——它讲的是"这个工具该多积极地被调用"，
那就该由工具自己说。

**为什么** 两份必然对不上，**而对不上的时候没人知道该信哪份**。曾经 `state_write`
一个人的描述就有 1906 字，`plan`/`check`/`steer` 三个把 `goal_manager.yaml` 里三节
逐句重写了一遍。收成 4625 → 1090 字。

**验收** `test_a_tool_carries_its_description_and_its_schema` · `test_the_criteria_are_not_in_the_tool_description`

### S28 · 工具调用失败一律可恢复，不抛出

**服务** —（内部契约）

**规格** 未知工具名、JSON 参数解析失败、参数不合法——三种都返回一条模型读得懂的
错误结果，让它下一轮改。

**不做** 抛异常终止这一轮。

**为什么** 这些是**模型可恢复的错误**，不是系统故障。

**验收** `test_unknown_tool_is_recoverable` · `test_malformed_json_arguments_are_recoverable` · `test_invalid_params_are_recoverable` · `test_unknown_state_path_returns_what_is_available`

### S29 · 明确不提供的

**服务** —（能力边界）

| 不提供 | 为什么 |
|---|---|
| **任何读正文的工具** | [S5](Architecture.md) 的基础。director 能覆盖五倍历史跨度，正是因为它从不读正文 |
| **调用 writer 的工具** | 同上。工具返回值会回流，正文回流即摧毁隔离 |
| **生成子 agent** | P16–P17。在编排层强制禁止，不靠提示词恳求 |
| **修改控制维度** | 那是用户的。模型不得自行调整 |
| **写文件 / 执行代码** | 无此需求，且是攻击面 |

**验收** `test_history_never_leaks_prose` · `test_the_director_cannot_touch_state`

---

## 横切约束

- **返回体量上限**：每个工具单次返回设硬上限，防止一次调用撑爆上下文。
- **调用轮数上限**：软上限（提示模型收敛）+ 硬上限（编排层强制收尾）。
- **重资源一律全量索引 + 按需展开**，索引里**必须带体量**，让模型知道拉全文的代价。
