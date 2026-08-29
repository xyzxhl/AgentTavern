# 没落实的

> **开发中允许三层对不齐**，但不允许它藏在正文里——藏起来就等于假装它已经成立。
> 每条写清缺的是哪一层。
>
> 三种缺口的定义见 [Workflow.md](Workflow.md)。

| 缺什么 | 意味着 |
|---|---|
| req 有，spec 没有 | **功能还没做** |
| spec 有，test 没有 | 没人知道它现在还成不成立 |
| spec 有，代码没有 | **写在纸上的规格**——最危险的一种，它读起来和落地了的一模一样 |

---

## spec 有，代码没有

### 外部资料检索：还差 REST 与前端（S136 / S143 / S144）

[`spec/Corpus.md`](spec/Corpus.md) **整篇写在代码前面**（S136–S148）。已经落地的
是**模型这一侧的全部**：切块、BM25、向量与混合、rerank、工具接入 director、
回执、trace。还没有的是**人这一侧**——

| 还差什么 | 谁在等它 |
|---|---|
| 资料的增删改查接口、导入原文 | S136 的 `test_a_corpus_can_be_attached_to_several_sessions` · `test_deleting_a_corpus_in_use_is_refused` |
| 后台建索引，进度可见、失败可见 | S144 的 `test_import_returns_before_the_index_is_built` · `test_a_failed_build_is_visible_not_silent` |
| 索引整体的落盘往返 | S143 的 `test_the_index_survives_a_round_trip_through_disk` · `test_files_are_human_readable` |
| 前端资料管理页、挂载与摘除 | 无（前端零断言，见下） |

**后果是这条链路现在只能靠测试喂进去，用户还导不了资料。** `Session.corpus_ids`
已经在，但没有任何接口能往里写。

**另外：`corpus` 那一组参数一个都没量过**（`min_coverage: 0.5` /
`min_similarity: 0.35` / `rrf_k: 60` / `top_k: 5`）。它们是常规取值，不是这个项目
量出来的——能导入真实资料之后，**按 [P27](Principles.md) 先量方差再动它们**。


### 归因第六类「写入冗余」

规格说归因分**六**类，代码里的 `AttributionClass` 只有**五**个成员，
`WRITE_REDUNDANCY` 不存在，也没有任何测试。

它当初是专门补的：原本五类里写入侧只有"遗漏"没有"多余"，于是"记了不该记的"
无人负责。而**冗余的代价是慢性的**（状态越长越容易看漏要紧的那几条），
不会以单点错误的形式暴露，只能靠专门一类去查。

连带六处仍写着「五类」：`Principles.md` P18、`README.md`、`Optimization.md` ×2、
`models/trace.py`、`bench/grader.py`。`spec/Prose.md` 写的是「六类」——
**同一个库里两种说法并存**。

新的 [`spec/Architecture.md` S12](spec/Architecture.md) 已按六类写，并在条目下
标了「这条规格没有落地」。

> 补它要动代码与测试，不在文档迁移范围内。

---

## spec 有，test 没有

迁移中逐条核对「验收」字段时留下的。

| 规格 | 缺什么 |
|---|---|
| [S56](spec/Retrieval.md) 检索工具的描述给触发条件不给机制说明 | 六条触发条件与三条反面都没有测试守着。**它是提示词内容，容易在下次精简时被整段删掉** |
| [S71](spec/Worldbook.md) 不认识的版本直接拒绝 | 没有测试喂一个 `version: 2` 进去看它拒不拒。这条的失败模式恰恰是「尽力而为地读进来」——**不测就发现不了** |
| [S85](spec/Prompting.md) 消融换一份 YAML 而不是开后门 | `PromptSetup` 被整套测试**用着**，但没有一条测试钉它自己的语义（改的是副本、跑完删掉） |
| [S106](spec/InputHandling.md) 动作是锚点，结果归世界 | 模型实测里选对了，**但那是蒙对的**——设计里从没规定过。现在规定了，仍然没测 |
| [S115](spec/Trace.md) 落盘的 trace 要能读回来 | 没测「没留过痕的轮次回空列表而不是 404」 |
| [S121](spec/Session.md) 触发源留在 session | 没测 |

### 前端行为，一条测试都没有

`spec/UI.md` 的 14 条里有 **8 条没有任何测试**：S122 一件事一个入口 · S123 左栏不做
tab · S124 设定库是页面 · S125 两块过程夹着正文 · S128 目标条进输入框上方 ·
S133 外观归本地 · S134 选取值只有一种交互形态。

原因很直接：**前端没有测试**。整个 `frontend/` 只有 `tsc --noEmit` 和构建，
没有一个断言。而这些恰恰是**最容易在下次改动里悄悄退化**的东西——
类型检查看不出"两个入口"或"时间线倒着走"。

**同一条判据在 [S135](spec/UI.md) 下面已经立着**：一条决策落地与否，
看的是它从用户那一侧够不够得着。而现在没有任何自动化能回答这个问题。

> **一条自我提醒。** 这一节最初列了七条，其中四条是错的——
> `test_the_tool_list_is_not_a_message`、`test_siblings_endpoint`、
> `test_a_bare_key_shadowing_a_namespaced_one_is_flagged`、
> `test_stop_cancels_only_the_run_not_the_loop` 都存在，我按关键词搜没搜到就下了结论。
>
> **搜不到不等于没有。** 报「缺测试」之前要按规格描述的**行为**去找，
> 不是按规格的**措辞**去找——测试名是用它自己的话写的。

---

## 悬着的问题

### 一条坏条目该不该让整份导入失败

现在是**整份失败**：`/worldbooks/import` 先 `bundle.check()`，不过就 400。

而设定库**列表**那条路走的是另一套：好的照数、坏的点出来，因为一条坏数据曾经让
整个设定库列不出来。两条路的判据是「这份数据会不会进上下文」——列表只为显示，
所以宽容；导入产生的是要喂模型的数据，所以严格。

**但导入还有第三种情形没被回答**：手上是别人给的一份、其中一条坏了，用户既拿不到
另外九十九条，也没法只修那一条。目前只能让他去改文件。

> 写 [`spec/Worldbook.md`](spec/Worldbook.md) 时把这条一起定了。

---

---

## 野生测试：65 条钉不到任何 spec

**有断言，但没有任何一条 spec 的「验收」点过它们。** 这不一定是坏事——
其中 13 条在 `test_bench.py`、11 条在 `test_gateway.py`，测的是评测工具与 SDK 封装，
本来就不服务某条产品规格。

但另外那些值得逐条看一眼：**要么补一条 spec，要么这条测试钉的是文案不是不变量。**

| 文件 | 条数 |
|---|---|
| test_bench.py | 13 |
| test_gateway.py | 11 |
| test_server.py | 10 |
| test_run.py | 6 |
| test_state.py · test_state_manager.py | 各 5 |
| test_director.py | 4 |
| test_repository.py · test_schema.py | 各 3 |
| test_runner_lifecycle.py · test_tools.py | 各 2 |
| test_prompts_api.py | 1 |

## test → spec 的反向链接没做

spec 那侧的「验收」字段点了 **211 个测试名**，所以 **spec → test 是通的**。
反过来（每条测试的 docstring 首行写它钉的 `S<n>`）**只做到 141 条**，
其余靠人从 spec 那侧查。

试过一次自动加，脚本在多行签名上匹配跑偏、写坏了一个文件（84 条改动而可推导的只有
79 条），已整个回滚。**这件事要么逐文件手工做，要么不做**——
自动化在这里的风险大于收益，而正向链路已经够用。
