# 提示词层

> **回答**：模型收到的每一个字住在哪、长什么形状、谁能改它。
>
> **不回答**：具体某句话该怎么写（那是提示词内容，归用户）、
> 文风怎么量（见 [Prose.md](Prose.md)）。
>
> 写 YAML 的操作细则在 [`prompting/README.md`](../../backend/agenttavern/prompting/README.md)
> ——那是唯一的说明，这里只定规格。

## 全貌

**这一层曾经叫「配置」，现在没有配置这个概念了。**

session 只装标题、目标、节点树；worldbook 只装名字和条目。
控制维度、能力开关、档位取值**全部住在 agent 的 YAML 里**——
它们都在改「模型收到什么」，那就该和提示词在同一页上。

---

### S74 · 一份文件 = 一个 agent 收到的全部文字

**服务** [R31](../req/调教提示词.md)

**规格** 按**接收方**分文件，不按主题分。一份文件五个顶层键：

| 键 | 装什么 |
|---|---|
| `sections` | 它收到的那一串消息 |
| `tools` | 它拿得到的工具，每个一条描述 + 开关 |
| `specials` | 运行时**另外追加的整条消息**：追加输入、空回催促、归段追问 |
| `rendering` | 嵌在渲染里的短句：`（尚无记录）`、`（{chars} 字，用 state_query 展开）` |
| `enabled` | 这个 agent 跑不跑 |

**不做** 按主题分文件（比如「对白」一个文件）。也不做片段文件。

**为什么** 按接收方分才**看得出隔离有没有被破坏**——文风混进 director、正文混进哪
一侧，翻开对应文件就知道。按主题分会把两侧的内容搅在一起，**而两侧的分工正是整个
拓扑的地基**。

后三个键是补的，判据是 [R31](../req/调教提示词.md)：`sections` 只是"模型读到的字"
的一部分。归段追问以前是一段四百多字的 Python 字符串，每轮都进上下文而界面完全够
不着。

**验收** `test_each_side_gets_its_own_sections_and_none_of_the_others` · `test_the_attached_prompts_live_with_their_agent` · `test_the_specials_and_the_rendering_strings_are_reachable`

### S75 · 一个 section = 一条消息，`role` 写在条目上

**服务** [R34](../req/调教提示词.md)

**规格** 顺序就是列表顺序。想调位置就移动条目，想加一节就插一条，**不用动代码**。

**不做** 把整份提示词拼成一整块 system 文本。

**为什么** 这才让「破限压在历史之后」成为**一次拖动**而不是改代码——
此前那个决定藏在 Python 里，从 YAML 那侧根本看不见还有第二种可能。

实测端点接受 9 条连续 system + user/assistant 交替共 17 条消息。

**验收** `test_each_section_is_one_message_and_carries_its_own_role` · `test_sections_order_comes_from_yaml` · `test_a_new_section_needs_only_yaml` · `test_a_section_without_a_role_raises` · `test_the_jailbreak_is_the_last_system_message`

### S76 · 只有三类条目，`type` 是显式字段

**服务** [R34](../req/调教提示词.md)

**规格**

| `type` | 用户能改什么 | 内容从哪来 |
|---|---|---|
| `text` | 正文，可含 `{{var}}` 单值占位 | 用户写的字 |
| `context` | **只有位置** | 代码整段生成 |
| `choice` | 每一档的正文 **+ 当前选哪一档** | 用户写的字 |

**类型必须写出来，不靠"出现了哪个键"去猜**——拖动排序的界面要先读出这一条是什么
类型才画得出来。

`{{var}}` 与 `context` 的分界是**体量与位置**：前者是嵌在句子里的单值，
后者是独占一条消息的一大块。

**注入进来的内容不当模板解析**——它是数据，用户的世界设定里完全可能出现 `{{`。

**验收** `test_three_kinds_of_content_are_declared_separately` · `test_injected_context_is_not_parsed_as_a_template` · `test_a_context_can_be_a_run_of_messages`

### S77 · 变量为空就删掉它所在的那一行，一行不剩整节不出现

**服务** [R32](../req/调教提示词.md)

**规格** 因此约定**一行一个变量**。正文为空的条目**整条跳过，连标题都不出现**。

**不做** 留下「视角：」这样的半截行；不做空标题。

**为什么** 半截行**比没有更糟**——模型会以为这里本该有内容而自己补一个。
空标题白占体量，同样会让模型以为这里缺了东西。

**验收** `test_an_empty_variable_takes_its_line_with_it` · `test_empty_section_is_skipped_message_and_all` · `test_empty_nsfw_injects_nothing_at_all` · `test_empty_state_renders_cleanly`

### S78 · 装配器做减法：所见即所得

**服务** [R32](../req/调教提示词.md)

**规格** 三样**已经删掉**：

| 删掉的 | 它的替代 |
|---|---|
| `title` 自动包 `<标题>…</标题>` | 要标签就**自己写进正文**（那时它可见可编） |
| `intro` / `outro` | 在它前面 / 后面**放一条 `text`** |
| `key` 引用顶层命名块 | 正文一律内联进条目 |

**不做** 任何"系统替用户多做一件事"的自动加工。

**为什么** 审查的前提是**编辑器里显示的那段文字必须就是模型收到的那段**。
装配器每替用户多做一件事，两者就差一点。

**取消自动标签有一个要盯的风险**：每个条目本来就是独立消息、边界已经存在，
但**部分供应商会合并连续的同 role 消息**。真出问题时的补法是让用户自己把标签写进
正文，**不是把自动包裹加回来**。

有三处**代码要认那个标签**，它们各自声明自己认的是哪一串（重放时靠它认出用户插话、
两条渲染文案）。

**验收** `test_an_appended_input_is_wrapped_with_an_explanation` · `test_a_good_edit_lands_and_takes_effect_at_once`

### S79 · 名字对不上一律当场报错，不静默跳过

**服务** [R35](../req/调教提示词.md)

**规格** 未知的 `context`、缺值的 `{{var}}`、没写 `role`、`selected` 不在选项里
——一律抛。保存前**先试装配一遍**，装不出来就拒绝并报出是哪一节坏了。
`rendering` 缺条目同样拒绝——**每一条在代码里都有调用点**。

**为什么** 静默跳过会让一整节悄悄消失，**而产出看起来只是"变差了一点"**——
那是最难查的一类问题。

不先试装配的话，改坏一个字下一轮生成直接 500，而错误信息离原因很远。

**验收** `test_names_that_do_not_line_up_raise` · `test_saving_runs_a_dry_assembly_first` · `test_a_missing_rendering_string_is_refused` · `test_empty_choices_raises`

### S80 · 档位的当前取值住在条目自己身上

**服务** [R14](../req/推进剧情.md) [R34](../req/调教提示词.md)

**规格** 「有哪些档」和「选哪一档」在同一个地方：`choice` 条目的 `options` 与
`selected`。**一处定义、一处取值、一处修改**，改完下一轮生效。

**不做** 把取值绑在 session 或 worldbook 上；**不做单轮覆盖**——取值只有一处，
没有被覆盖的对象了。

**为什么** 曾经「有哪些档」在 YAML、「选哪一档」在 session 上，改一档文风要动两个
地方。

**已知代价写在明处**：同一台服务上两局故事不能有不同的文风。这是"层级上调"的定义
本身带来的。要换一套配置就去 agent 管理改——**那是配置层面的动作，不是每轮的动作**，
所以「设一次长期有效」这条判断没被破坏。

**验收** `test_the_current_choice_lives_in_the_yaml_not_the_session` · `test_meta_no_longer_carries_the_control_options` · `test_speaking_rule_is_a_choice_not_a_toggle_in_code`

### S81 · 控制维度四项，各三档

**服务** [R14](../req/推进剧情.md) [R13](../req/推进剧情.md) [R15](../req/推进剧情.md)

**规格** 输入处理 / 模型剧情控制权 / 是否允许抢话 / 铺陈程度。

**输入处理三档**：转述+推进 / 只转述 / 不转述+推进。
故事跨度**不是独立维度**——想快进就写在指令里。

**不做** 「转述二值 × 故事跨度」这样的乘积；不做自由文本的文风字段。

**为什么** 乘积会让取值组合爆炸且有无意义的格子。文风写成自由文本时它散在每个
session 里，改一次只影响一个故事，**而且从提示词那侧完全看不见**。

**验收** `test_input_handling_three_ways` · `test_authority_maps_to_ask_question_eagerness` · `test_speaking_rule_is_a_choice_not_a_toggle_in_code` · `test_density_reaches_both_sides_in_opposite_directions`

### S82 · 显式设置优先于输入内容

**服务** [R14](../req/推进剧情.md)

**规格** 控制维度与输入内容冲突时，**以显式设置为准**。乐观假设用户尊重自己的设置。

**不做** "内容优先"——那会把推断请回来。

**验收** `test_input_handling_three_ways`

### S83 · 一个维度喂多处时，代码不再保证一致

**服务** [R14](../req/推进剧情.md)

**规格** 铺陈程度现在喂三处（director 的事件密度、writer 的篇幅、goal 的推进节奏），
拆成三个独立的 `selected` 之后**代码不保证它们一致**。

**为什么** 这既是代价也是收益：现在做不到「director 紧凑、writer 铺陈」，
之后可以——**降级的是保证，不是能力**。

**验收** `test_density_reaches_both_sides_in_opposite_directions`

### S84 · 工具描述可编，参数 schema 只读并排显示

**服务** [R31](../req/调教提示词.md)

**规格** 描述搬进 YAML；参数 schema 仍由 Python 生成，在页面上与描述**并排只读**。
错误文案留在代码里（它不进上下文）。

**同一个工具在两个 agent 手上是两份描述、两个开关**。

**为什么** 曾经的顾虑是「一个工具怎么描述自己，跟它的参数、错误信息、注册条件是
同一件事，拆开写两处必然对不上」——**那个风险是真的**，所以并排显示：
**漂移摆在人眼前，而不是藏在两个文件里。**

**验收** `test_a_tool_carries_its_description_and_its_schema` · `test_the_list_comes_from_the_yaml_files`

### S85 · 消融换的是一份 YAML，不是开一个后门

**服务** —（开发期）

**规格** 把 `prompting/` 复制到临时目录、在**副本**上改那几档、让装配器读副本。
测试也走这一条。

**不做** 加一个 `overrides` 参数。

**为什么** **一个"只有程序能用"的第二取值入口，迟早会有第二个调用者。**
换 YAML 与"取值只有一处"不冲突——取值仍然只住在 YAML 里，只是用了另一份 YAML。

**代价照单收下**：一次 bench 只能跑一种文风，消融改成换 YAML。

**验收** —（`lab.py` 的 `PromptSetup` 被整套测试用着，但没有一条测试钉它自己的语义。见 [TODO.md](../TODO.md)）

### S86 · 配置分三处，判据是「换台机器要不要改」

**服务** —（工程约定）

**规格**

| 住在 | 装什么 | 进版本控制 |
|---|---|---|
| `.env` | 端点、key、模型、超时、数据目录 | ✗ 秘密与机器相关 |
| `config.yaml` | 各种上限、预算、窗口 | ✓ 跟机器无关 |
| agent YAML | 一切改变「模型收到什么」的 | ✓ |

`config.yaml` 里**每个值旁边都写着当初为什么是它、调大调小各会坏成什么样**。
模型里再写一份默认值是有意的——**文件缺失或损坏时进程仍能起来**。

**不做** 把上限散在各模块的模块级常量里；不做"配置文件读不到就在 import 阶段炸掉"。

**为什么** 十六个旋钮曾散在八个模块里：想知道「director 到底几轮上限」得先猜它在
哪个文件，想调一个数得改代码。那些理由跟着常量走反而找不着。

**在 import 阶段炸掉是最糟的失败方式**：什么都跑不了，而错误看起来跟配置无关。

**验收** `test_config_yaml_covers_every_knob` · `test_a_missing_config_file_falls_back_instead_of_crashing`

### S87 · 服务级的三项只读

**服务** [R36](../req/开发者视角.md)

**规格** 流式、trace 落盘、开发者模式在 `.env` 里，启动服务时就定了，
界面上**只读**。

**不做** 做成运行时开关。

**为什么** 「这台服务是拿来查问题的还是拿来玩的」不该每个故事各答一遍。
做成可写的等于**把一个部署决定降级成一个运行时开关**，而切来切去会让
「这一轮到底记了没有」变成要翻日志才知道的问题。

这三项**不是 agent 行为**：流式关于交付、trace 关于运维，开关它们不改变模型收到什么。

**验收** `test_meta_no_longer_carries_the_control_options`
