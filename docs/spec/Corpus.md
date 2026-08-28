# 外部资料检索

> **回答**：资料存成什么、怎么建索引、怎么检索、检索结果怎么回给模型。
>
> **不回答**：状态与设定的检索（见 [Retrieval.md](Retrieval.md)——那两条的准入标准
> 和这一条**根本不同**，见下）、工具面全貌（见 [Tools.md](Tools.md)）。

> ⚠ **这一篇写在代码前面。** 下面的「验收」点的是**还不存在**的测试，
> 它们会先红后绿。进度见 [TODO.md](../TODO.md)。

## 全貌

一条**尽力而为**的检索链路，只给 director：

```
用户导入资料 → 切块 → BM25 索引 + 向量索引（异步建，进度可见）
                              ↓
director 调 corpus_search(query)
    → BM25 取 N 条 + 向量取 N 条，合并去重
    → rerank（独立的模型调用）取前 k
    → 回执带着「从多少字里筛的、没挑中不代表不存在」
```

## 这一层和别的检索的根本差别

系统里另外三条检索路（state / worldinfo / history）都满足 [P13](../Principles.md)：
**规则只做无损降维**，模型始终知道有什么存在。

**这一条做不到，而且是原理上做不到。**

用户给的资料**无法保证有结构**（一本小说、一份扫描件转的文本、一堆散帖），
装不下一份完整的目录；能不能找到要的东西取决于**模型这一次的 query 问得对不对**，
而那保证不了。

所以 P13 在这里不是被违反，是**不适用**——它的前提（全集装得下）不成立。
它此时说的其实是「**那就别假装这是无损的**」。

**补法只有一条：说出来**（[P34](../Principles.md)）。见 [S139](#s139)。

---

### S136 · 资料是独立的一级资产

**服务** [R41](../req/资料检索.md)

**规格** 存在 `data/corpora/<id>/`，不在任何 session 目录里。
一局故事可以挂零到多份；一份资料可以被很多局故事挂。
删一份还有人用的资料**当场拒绝**，同设定库。

**不做** 做成 worldbook 的第四类条目。

**为什么** 它和 worldbook 在**谁写、多大、什么时候进上下文**三点上全都不同：

| | worldbook | 资料 |
|---|---|---|
| 谁写 | 用户 | 别人，只读 |
| 多大 | 一条几百字 | 一份几十万字 |
| 何时进上下文 | 可能每轮在场 | **只在被查到时** |

塞进 worldbook 会撑爆它「条目正文 → worldinfo 每轮注入」那条投影，
**得为它开特例**——而 [S65](Worldbook.md) 刚说过「用户扮演的角色不是第四类 kind」
正是为了避免特例。

**验收** `test_a_corpus_can_be_attached_to_several_sessions` · `test_deleting_a_corpus_in_use_is_refused`

### S137 · 检索工具只给 director，writer 一个工具都没有

**服务** [R40](../req/资料检索.md)

**规格** `corpus_search` 只出现在 director 的工具面里。
**writer 的 context 不增加任何一条**——它仍然只有角色语言习惯、正文上文、本轮细纲。

**不做** 给 writer 一条 `corpus_hits` 的 context；不做给 writer 工具。

**为什么** 两条理由，第二条更硬——

1. **已有的路够用**：[S89](Prose.md) 已经规定「数字、价钱、名字、期限细纲必须写死」，
   **术语和黑话是同一类东西**。走已有的路不需要新机制。
2. **资料原文是一份现成的文风范本。** [S15](Generation.md) 说「正文跟着细纲腔调走是
   writer 的缺陷」——那时细纲只是干巴的事实陈述。换成一本小说的原文段落摆在 writer
   眼前，它很可能跟着那个腔调走，而那**不是**用户选的文风。

顺带一条事实澄清：**worldinfo 也从来不到 writer**。检索到的东西到达 writer
只有一条路——**director 把它写进细纲**。这不是疏漏，是 [S3](Architecture.md) 的直接后果。

**验收** `test_corpus_search_is_only_in_the_director_tools` · `test_writer_never_sees_corpus_hits`

### S138 · state_manager 与 goal_manager 都不碰资料

**服务** —（能力边界）

**规格** 两个都不给。

**为什么** state_manager 的活是**照着刚写出来的正文记账**，不是了解世界——
术语在正文里怎么写，它就怎么记。这和它不读 worldinfo 是同一条理由。

goal_manager 管的是「推到哪儿了」，一条检索路都没有，这一条也不例外。

**验收** `test_the_state_manager_has_no_corpus_tool` · `test_the_goal_manager_has_no_corpus_tool`

<a id="s139"></a>

### S139 · 每次返回都写明它是筛出来的

**服务** [R43](../req/资料检索.md)

**规格** 回执必须带四样：

| | |
|---|---|
| **用的 query** | 原样回显——模型据此判断是不是自己问错了 |
| **资料总量** | 「从 32 万字里」 |
| **返回几段** | 「挑出这 5 段」 |
| **一句明话** | **「没挑中不代表资料里没有，换个说法可以再查」** |

**不做** 只返回几段就完事；不做查不中返回空列表。

**为什么** 只给几段，模型会读成「资料里就这些」——**那正是这个系统当初判定关键词触发
不合格的同一条罪**：拿到一份看起来完整的结果，却不知道自己丢了什么。

区别只在一点：那时**可以**不那么做，这里**做不到**。做不到的时候，
把静默的缺失变成**已知的不完整**，是唯一的补法。

查不中同理：空列表会被读成「没这回事」，必须明写「这次没命中，换个说法再试」。
**这个错这个项目犯过一次**（历史检索的 query 曾是子串过滤器），不再犯第二次。

**验收** `test_a_hit_list_says_how_much_it_was_picked_from` · `test_a_miss_says_it_is_a_miss_not_an_absence` · `test_the_query_is_echoed_back`

### S140 · 混合检索：BM25 + 向量，合并去重后 rerank

**服务** [R40](../req/资料检索.md)

**规格** 两路各取 N 条 → 按块 id 合并去重 → rerank 取前 k。

BM25 那一路**不依赖任何外部模型**，所以它是**降级路径**：
**embeddings 没配、或调用失败时，整条链路退回纯 BM25 并在回执里说明**，
不是拒绝启用。

**为什么降级而不是拒绝**：拒绝的话，一个只配了 chat 端点的部署**整个功能用不了**——
而纯 BM25 对这个场景（专名、术语、黑话）本来就有相当一部分价值。
降级是有损的，所以它必须**说出来**（同 [S139](#s139)）。

**不做** 只用向量。

**为什么** 中文里 BM25 对**专名、术语、黑话**这类低频词特别有效——
而这恰恰是这个功能要解决的那一类问题（同人黑话、朝代官职、专业术语）。
向量补的是「说法不同但意思相近」。两者的强项互不重叠。

**中文分词用 `jieba`。** 不分词就只能按字符 n-gram，而 BM25 的强项恰恰是
**专名与术语这类低频词**——切成单字之后那个强项就没了。

**embeddings 走独立的一组配置**（`EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL`）,
不复用 chat 那一组：多数人的 chat 与 embedding 未必在同一家。

**验收** `test_both_paths_contribute_and_duplicates_collapse` · `test_it_falls_back_to_bm25_when_embeddings_are_unavailable` · `test_chinese_terms_survive_tokenisation`

### S141 · rerank 复用 chat 模型，但它是一次**独立调用**

**服务** [R40](../req/资料检索.md)

**规格** 三条，缺一不可：

1. **独立调用。** 不接在 director 的对话后面，不进它的 tool loop——
   它是一次干净的、只看 [query + 候选段落] 的调用。
2. **独立的缓存空间。** 不传 session 的 id，用它自己的。
3. **提示词归「其他调用」**，不归任何 agent（见 [S147](#s147)）。

排序而不是逐段打分：给它全部候选让它**排个序**，不问「这一段几分」。

**不做** 引入 cross-encoder 端点。

**为什么**

- **独立调用**：接在 director 对话后面的话，它会读到整轮推演，
  于是"哪一段最相关"变成"哪一段最符合我已经想好的走向"——那是自我确认（同 [P20](../Principles.md)）。
- **独立缓存**：它的静态前缀（排序指令）跨会话完全相同，独占一个空间能最大化复用；
  而混进 session 的空间只会**污染那边的缓存命中率**——
  这个项目已经栽过一次「所有会话挤在同一个缓存空间里」（[P29](../Principles.md)）。
- **排序不打分**：打分是开放判断，排序是受约束的选择（[P10](../Principles.md)）。
- **不引 cross-encoder**：整个项目只用 `chat completions` 一个面，任何兼容端点都能接。
  rerank 端点是三件里**可移植性最差的**——自建与本地部署常常没有。
  代价照单收下：慢一些、多花 token。

**验收** `test_rerank_is_its_own_call_not_appended_to_the_director` · `test_rerank_uses_its_own_cache_space` · `test_rerank_orders_rather_than_scores`

### S142 · 定长滑窗切块，带重叠，不做语义切分

**服务** [R40](../req/资料检索.md)

**规格** 块长与重叠写进 `config.yaml`，**每个值旁边写着调大调小各会坏成什么样**。
每块记住它在原文里的字符区间。

**不做** 用模型做语义切分。

**为什么** 语义切分要模型参与——**又一次调用、又一层不确定性**，
而这条链路的检索质量还一次都没量过。[P27](../Principles.md)：先量方差，再改东西。
定长切块是那个基线。

重叠是为了不让一句话正好被切断——**被切断的那句谁也检索不到**。

**验收** `test_chunks_overlap_so_nothing_falls_between_them` · `test_a_chunk_remembers_where_it_came_from`

### S143 · 索引落盘，不引数据库

**服务** —（技术约束）

**规格** `data/corpora/<id>/` 下：原文、块表（JSON）、BM25 索引、向量（`.npy`）。

**不做** 引入向量数据库。

**为什么** 同「不用 ORM」那条理由：**用户可直接编辑**是这个项目的取向，
而数据库会把这层能力藏起来。规模上也够——几十万字切成几千块，
全量点积毫秒级。真到瓶颈再说。

**验收** `test_the_index_survives_a_round_trip_through_disk` · `test_files_are_human_readable`

### S144 · 建索引是异步的，进度看得见，没建好时明说

**服务** [R45](../req/资料检索.md)

**规格** 导入立刻返回，索引在后台建。状态有三种：**建索引中 / 可用 / 失败**，
带进度。

查一份还没建好的资料，回执明说**「这份资料还在建索引，暂时查不了」**。

**不做** 导入时同步阻塞；**不做**没建好时返回空列表。

**为什么** 一本三十万字的小说切几千块、每块过一次 embedding，要花分钟级的时间。
同步阻塞会让导入看起来卡死。

返回空列表会被读成「资料里没有」——**和 [S139](#s139) 是同一条罪**。

**验收** `test_import_returns_before_the_index_is_built` · `test_searching_an_unbuilt_corpus_says_so_instead_of_returning_nothing` · `test_a_failed_build_is_visible_not_silent`

### S145 · 关掉时工具不注册

**服务** [R44](../req/资料检索.md)

**规格** 开关在 `director.yaml` 的 `tools.corpus_search.enabled`，同其它工具。
关掉时**根本不注册**，提示词里也不提。没挂任何资料时同样不注册。

**为什么** 同 [S21](Tools.md)：注册了但没东西可查的工具，
白占提示词体量、还会被试着调一次、失败还要消耗一轮往返。

**验收** `test_the_corpus_tool_disappears_when_switched_off` · `test_no_corpus_attached_means_no_tool`

### S146 · 每次检索都进 trace

**服务** [R42](../req/资料检索.md)

**规格** 记下：query、两路各自的候选、rerank 之后的次序、最终返回的几段
及其在原文里的位置。

**为什么** 这条链路查不查得准，**用户是唯一判得出来的人**——他知道自己那份资料里
有什么。看不见的话，写错了他只会觉得"这模型不懂"，而不知道是没查到还是查错了。

rerank 前后的次序都要记：**它到底有没有起作用，是这条链路第一个要量的东西。**

**验收** `test_a_search_lands_in_the_trace_with_both_stages`

<a id="s147"></a>

### S147 · 提示词分两组：agent 与「其他调用」

**服务** [R31](../req/调教提示词.md)

**规格** 提示词管理页分两组：

| | |
|---|---|
| **agent** | director · writer · state_manager · goal_manager |
| **其他调用** | summarizer · rerank |

两组都可编辑（[R31](../req/调教提示词.md) 管的是「模型会读到的字」，不分是谁在读）。

**不做** 把 rerank 塞进 director 的附属提示词；不做继续把 summarizer 当成第五个 agent。

**为什么** agent 会推演、会产出剧情、有自己的上下文与工具面。
summarizer 和 rerank 都是**机械的辅助调用**：输入固定、输出固定、不参与剧情。
把它们并排列成"agent"会让人以为它们和 director 是同一类东西。

**验收** `test_the_prompt_page_separates_agents_from_helpers` · `test_a_helper_is_still_editable`

---

## 未定

- **要不要做 query 改写 / 扩展。** 检索成不成取决于 query 问得对不对，
  而改写能提高命中——但那是**又一次模型调用、又一层不确定性**。
  **要什么才能定**：先把基线跑通，量出「模型自己写的 query」的命中率有多低。
- **rerank 到底有没有用。** 复用 chat 模型做排序，慢且费 token。
  **要什么才能定**：同一批 query 比较「BM25+向量直接取 top-k」与「rerank 之后取 top-k」，
  按 [P27](../Principles.md) 先量方差。
- **多份资料之间要不要分权重。** 一局故事挂三份资料时，命中的段落来自哪一份要不要
  影响排序。**先不做**——没有数据支撑任何一种权重。
