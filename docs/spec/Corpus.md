# 外部资料检索

> **回答**：资料存成什么、怎么建索引、怎么检索、检索结果怎么回给模型。
>
> **不回答**：状态与设定的检索（见 [Retrieval.md](Retrieval.md)——那两条的准入标准
> 和这一条**根本不同**，见下）、工具面全貌（见 [Tools.md](Tools.md)）。

> ⚠ **后端已经全部落地**（切块、检索、rerank、工具、回执、trace、REST、
> 后台建索引、源文件夹对账）。**只差前端的资料管理页**——在那之前用户仍然
> 够不着这个功能。进度见 [TODO.md](../TODO.md)。

## 全貌

一条**尽力而为**的检索链路，只给 director：

```
用户导入资料 → 切块 → BM25 索引 + 向量索引（异步建，进度可见）
                              ↓
director 调 corpus_search(query)
    → BM25 取 N 条 + 向量取 N 条，合并去重
    → rerank（专业 reranker，没配就不 rerank）取前 k
    → 回执只有两种：那几段原文 + 位置，或者「这次没找到」
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

删一份还有人用的资料**只提示，不拦**：先说清是哪几局故事挂着它，删掉之后
**把这个 id 从那几局的 `corpus_ids` 里一并摘掉**——留着悬空 id 的话，
列表会宽容地跳过它，那就成了静默的缺失。

**不做** 做成 worldbook 的第四类条目；**不做**像设定库那样拒绝删除。

**为什么删除不拦（和设定库不对称，这是有意的）** 设定是**故事的地基**——
删掉它，那几局的角色、初始状态、世界全都没了，故事当场失忆；资料是**可选的增援**，
删掉只是少一条检索路，故事照跑。**代价的量级差着一档，闸门的松紧就该不一样。**

更硬的一条理由：资料的删除**多数是从文件夹那一侧发起的**（[S149](#s149)）——
用户已经把源文件删了，再来问他一次"真的要删吗"是在替他做决定。
需要的是**把后果告诉他**，不是拦住他。

**为什么** 它和 worldbook 在**谁写、多大、什么时候进上下文**三点上全都不同：

| | worldbook | 资料 |
|---|---|---|
| 谁写 | 用户 | 别人，只读 |
| 多大 | 一条几百字 | 一份几十万字 |
| 何时进上下文 | 可能每轮在场 | **只在被查到时** |

塞进 worldbook 会撑爆它「条目正文 → worldinfo 每轮注入」那条投影，
**得为它开特例**——而 [S65](Worldbook.md) 刚说过「用户扮演的角色不是第四类 kind」
正是为了避免特例。

**验收** `test_a_corpus_can_be_attached_to_several_sessions` · `test_a_session_can_hold_several_corpora` · `test_deleting_a_corpus_in_use_says_who_is_affected_and_proceeds` · `test_deleting_a_corpus_detaches_it_from_every_session` · `test_detaching_leaves_the_corpus_itself_alone` · `test_attaching_a_corpus_that_does_not_exist_is_refused` · `test_a_dangling_corpus_id_does_not_break_the_run`

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

### S139 · 系统自己判定检索成没成，不给模型免责声明

**服务** [R43](../req/资料检索.md)

**规格** 每次检索**由系统给出一个结论**，只有两种：

| | 回执 |
|---|---|
| **找到了** | 那几段原文 + 各自在资料里的位置 |
| **没找到** | 「这次没有找到足够相关的段落」+ 用的 query |

判定靠 [S148](#s148) 的置信度，**不靠模型自己掂量**。

**不做** 每次回执附一句「这可能不全，仅供参考」。

**为什么** **那是免责声明，不是机制。** 三重代价：

1. **模型会忽略它。** 一句每次都出现、每次都一样的话，和不写没有区别——
   它不改变任何一次具体的判断。
2. **它每次都占上下文。** 一条永远为真的话，每轮都在竞争注意力（[P32](../Principles.md)）。
3. **它把判断推给了模型**——而这正是 [P10](../Principles.md) 说的反面：
   **凡是模型的开放判断，都降级成受约束的选择。** 系统能算出"这次匹配够不够强"，
   就该算完再给结论，不该把原始候选连同一句"你自己看着办"一起端上去。

**措辞上有一条硬线**：没找到时说的是**「这次检索没找到」**，不是**「资料里没有」**。
前者是系统对自己这次动作的陈述，后者是对资料的断言——而这条链路没有资格下那个断言。

**验收** `test_a_hit_carries_where_it_came_from` · `test_a_miss_says_the_search_failed_not_that_the_corpus_lacks_it` · `test_the_query_is_echoed_back` · `test_no_disclaimer_is_appended_to_a_hit` · `test_a_hit_points_back_into_the_original_text` · `test_every_line_of_the_receipt_is_editable`

<a id="s148"></a>

### S148 · 相关度不够就判为没找到

**服务** [R43](../req/资料检索.md)

**规格** 两个信号各有阈值，**都不过就是没找到**：

| 信号 | 判据 | 为什么用它 |
|---|---|---|
| **词覆盖率**（BM25 侧） | query 切出来的词，有多少比例出现在这一块里 | **可移植**——BM25 原始分随语料统计变化，跨资料不可比；覆盖率是比例，到哪儿都一样 |
| **余弦相似度**（向量侧） | 归一化到 [-1, 1]，直接设阈 | 天然可比 |

只有 BM25 一路时（降级模式）只看覆盖率。阈值进 `config.yaml`。

**不做** 把原始 BM25 分数当阈值。

**为什么** **BM25 分数跨资料不可比**：它依赖这份资料的词频分布，同一个 3.7 分
在一本小说里是强命中、在一份术语表里可能是噪声。拿它设阈值，
**换一份资料整条链路的松紧就变了，而没有任何地方会报错**。

**返回勉强沾边的比返回空更糟**：模型拿到几段不相关的资料**会当成相关的用**，
写进剧情的就是一段有出处、但出处对不上的东西。**没找到，它至少知道自己不知道。**

**验收** `test_a_weak_match_is_reported_as_a_miss` · `test_coverage_not_raw_score_decides` · `test_the_threshold_comes_from_config`

### S140 · 混合检索：BM25 + 向量，用 RRF 融合

**服务** [R40](../req/资料检索.md)

**规格** 两路各出一份**排好序的候选**（各自先过自己的阈值，见 [S148](#s148)），
再用 **RRF（Reciprocal Rank Fusion）** 融合：

```
score(块) = Σ  1 / (K + 该路里的名次)      名次从 1 起，K 默认 60
          两路
```

**只用名次，不用分数。** 这是选它的全部理由——BM25 原始分与余弦相似度**尺度完全
不可比**，任何"加权求和"都要先把两者归一化到同一个刻度上，而那个刻度**没有依据**，
换一份资料就变。名次没有这个问题。

两路都命中的块天然得分更高（两个倒数相加），**不需要另设一个「both」加成**——
那种手写的加成是在凭感觉定权重。

BM25 那一路**不依赖任何外部模型**，所以它是**降级路径**：
**embeddings 没配、或调用失败时，整条链路退回纯 BM25**，不是拒绝启用。

**降级报给用户，不报给模型。** 这是 [S139](#s139) 那条的直接推论——
「这次只用了关键词匹配」对模型没有任何可操作性，它改变不了模型下一步做什么，
只会多占一份上下文；而**用户看到它就知道该去配 embeddings 了**。
所以它进 trace 与界面（[S146](#s146)），不进工具回执。

降级对模型可见的后果只有一个，而那已经有出口：**命中更难达标，于是「没找到」变多**
——由 [S148](#s148) 如实报出。

**不做** 只用向量；**不做**把两路分数加权求和。

**为什么两路都要** 中文里 BM25 对**专名、术语、黑话**这类低频词特别有效——
而这恰恰是这个功能要解决的那一类问题。向量补的是「说法不同但意思相近」。
两者的强项互不重叠。

**中文分词用 `jieba`。** 不分词就只能按字符 n-gram，而 BM25 的强项恰恰是
**专名与术语这类低频词**——切成单字之后那个强项就没了。

**embeddings 走独立的一组配置**（`EMBED_BASE_URL` / `EMBED_API_KEY` / `EMBED_MODEL`），
不复用 chat 那一组：多数人的 chat 与 embedding 未必在同一家。

**验收** `test_rrf_ranks_by_position_not_by_score` · `test_a_chunk_found_by_both_paths_ranks_above_one_found_by_one` · `test_both_paths_contribute_and_duplicates_collapse` · `test_it_falls_back_to_bm25_when_embeddings_are_unavailable` · `test_chinese_terms_survive_tokenisation`

### S141 · rerank 只有专业 reranker 一条路，没配就不 rerank

**服务** [R40](../req/资料检索.md)

**规格** 配了 reranker 就 rerank，**没配就按混合检索的融合次序取前 k，
一次模型调用都不发**。

四家走同一个接口（`corpus/rerankers.py`）：`cohere` / `jina` / `voyage` / `qwen`。
它们的结果项形状一致（`index` + `relevance_score`），差别只在 URL、请求体、
响应外层三处。家数名写错**当场报错**，不猜也不回落（[P29](../Principles.md)）。

**分数不可跨家比较**，所以 `Scored` 带着 `provider`：任何阈值都必须绑着它是谁给的。
这是 [S148](#s148) 已经吃过一次的亏（BM25 原始分不能当阈值），换个地方又出现——
Cohere 的 0.5 和 Voyage 的 0.5 不是一回事，而**换一家照抄阈值不会有任何地方报错**。

**没配时的降级写在明处**：那一步不判相关性，`min_relevance` 无从施加，
拦得住的只剩混合检索那一步的阈值（[S148](#s148)）。这件事**只报用户**
（`Lookup.degraded`），**不报模型**——模型知道了也不会换个做法，同 [S140](#s140)。

**不做** 把专业 reranker 定成必需（没配仍然查得到东西）；不做在调用方写
`if provider == ...`；**不做用 chat 模型排序那条退路**。

**为什么**

- **为什么要 reranker**：这一条曾经判的是「不引 cross-encoder 端点」，理由是
  可移植性。**推翻它的是一次实测**——相似度阈值**分不开「答得上」和「同领域但
  答不上」**（重叠：0.5557–0.7433 对 0.4311–0.5812），而后者正是这条链路最危险的
  一类：模型拿到几段同领域但答非所问的原文，**会当成答案用**。
  cross-encoder 在同一批问法上把三档**干净分开**，数据见 [TODO.md](../TODO.md)。
- **为什么删掉 chat 模型那条退路**（它曾经是这条规格的另一半：一份 `rerank.yaml`
  加一个宽容的序号解析器，只能排序、不能否决）：**它买到的只是次序变一变。**
  这条链路最危险的那一类是"同领域但答不上"，而那一类**排第几都还是会被当成答案
  用**——排序解决不了它，只有否决能。留着它的代价是实的：一份要维护的提示词
  （而提示词正文归用户，等于多给用户一件要照看的东西）、一条只在没配 reranker
  时才走的解析路径、以及这条规格里一半的篇幅。
- **收下分数不违反 [P10](../Principles.md)**：那一条禁的是**模型的开放判断**——
  让通用 chat 模型自由打分，分数从哪来无从追究。cross-encoder 的分数是一个专门
  训练来估相关度的模型的输出，是**测量**，不是它临场的意见。
  而分数正是排序做不到的那件事：**它让「一条都不合适」成为可能**。

**否决阈值 `corpus.min_relevance` 绑死在「哪一家 + 哪个模型」上**，换任何一样
都要重量。当前值量的是 **qwen / qwen3-rerank**：真实链路上 20 个 query，
「答得上」0.7561–0.9433、「同领域答不上」0.2561–0.4307，取空档中点 **0.6**。
**偏高是有意的**——漏掉一条真的，回执是「这次没找到」，模型能换个说法再查；
放过一条假的，它会当成事实写进剧情，**而且看不见**（[S148](#s148)）。

**「否决」和「出错」是两件事**：

| | 是什么 | 结果 |
|---|---|---|
| **否决** | 看过了，都不够相关 | 返回空 → 回执说「这次没找到」（[S139](#s139)） |
| **出错** | 根本没看成 | **退回融合排序，一条都不丢** |

把出错当成否决，一次网络抖动就会让模型收到一句**假的结论**——同 S139 那条硬线
（不下没有资格下的断言）。出错时也**不换一个 reranker 重试**：那要多花一次
调用，而且更糟的是**一次网络抖动会悄悄换掉产出这份排序的算法**
（[P28](../Principles.md)），此后任何一次前后对照都不成立。

**验收** `test_every_provider_returns_the_same_shape` ·
`test_every_provider_stamps_who_scored_it` · `test_voyage_says_top_k_not_top_n` ·
`test_qwen_nests_input_and_parameters` · `test_an_unknown_provider_is_refused_at_once` ·
`test_rerank_sees_more_candidates_than_it_returns` ·
`test_everything_below_the_threshold_is_a_miss` ·
`test_only_the_ones_above_the_threshold_come_back` ·
`test_the_threshold_comes_from_config` ·
`test_the_scores_and_who_gave_them_land_in_the_trace` ·
`test_a_rerank_failure_degrades_to_the_fusion_order` ·
`test_a_failure_degrades_to_no_rerank_at_all` ·
`test_without_a_reranker_nothing_is_reranked_and_nothing_is_called` ·
`test_no_provider_configured_means_no_reranker` ·
`test_candidates_from_several_corpora_are_reranked_together`


### S142 · 递归字符切分：先按自然边界，切不动才按字符

**服务** [R40](../req/资料检索.md)

**规格** 依次试一组分隔符，**第一个能把这块切到限长以内的就用它**：

```
段落（

） → 换行 → 。！？ → ；  → ，  → 空格 → 单个字符
```

切完再把碎片**贪心合并**回接近限长的块。分隔符跟着前一片走——
句子要以句号结尾，不能把句号甩到下一块开头。

每块记住它在原文里的字符区间，`text[start:end]` 必须还原出该块。
限长与重叠进 `config.yaml`。

**不做** 定长硬切；**不做**用模型做语义切分。

**为什么** 定长硬切会**从句子中间下刀**——切出来的两半各自都不成话，
BM25 那一路尤其吃亏：一个专名被劈成两半，两边都命中不了。

递归字符切分不需要模型、不引入不确定性，却能让绝大多数块**落在自然边界上**。
它是"不花模型的钱就能拿到的那部分语义切分"。

语义切分仍然不做：那要模型参与——又一次调用、又一层不确定性，
而这条链路的检索质量还一次都没量过（[P27](../Principles.md)：先量方差，再改东西）。

重叠仍然要：自然边界也会切断**跨句的指代**（"他把它放回原处"里的"它"在上一句）。

**验收** `test_it_splits_on_paragraph_before_sentence` · `test_it_falls_back_to_characters_when_nothing_else_fits` · `test_a_chunk_remembers_where_it_came_from` · `test_chunks_overlap_so_nothing_falls_between_them` · `test_no_chunk_exceeds_the_limit`

### S143 · 索引落盘，不引数据库

**服务** —（技术约束）

**规格** `data/corpora/<id>/` 下：原文、块表（JSON）、BM25 索引、向量（`.npy`）。

**不做** 引入向量数据库。

**为什么** 同「不用 ORM」那条理由：**用户可直接编辑**是这个项目的取向，
而数据库会把这层能力藏起来。

**规模上够，这是量过的**（1024 维，本机）：

| 块数 | 相当于 | 内存 | 单次查询 |
|---|---|---|---|
| 1,000 | 一本 30 万字小说 | 4 MB | **0.3 ms** |
| 5,000 | 五本 | 20 MB | 0.4 ms |
| 20,000 | 六百万字 | 78 MB | 1.9 ms |
| 100,000 | 三千万字（约五十本） | **391 MB** | 11.6 ms |

三千万字全量扫描 11.6 毫秒，而它旁边是一次几秒的模型调用。
**所以不引 faiss**：它的 flat 索引同样把向量放在内存里，换不来什么；
近似索引在这个量级上省下的时间会被它自己的构建与依赖成本吃掉，
而 `faiss-cpu` 在 Windows 上还常常装不上。

**先撞墙的是内存，不是速度。** 到那一天的出路按代价从小到大是：
`float16`（内存减半，余弦的召回损失极小）→ `np.load(mmap_mode="r")`
（挂了三份资料只查一份时不必全驻留）→ 才轮到近似索引。

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

**验收** `test_import_returns_before_the_index_is_built` · `test_progress_is_visible_while_building` · `test_searching_an_unbuilt_corpus_says_so_instead_of_returning_nothing` · `test_an_unbuilt_corpus_alone_is_not_reported_as_a_miss` · `test_a_failed_build_is_visible_not_silent` · `test_a_build_orphaned_by_a_restart_is_marked_failed` · `test_embeddings_failing_still_leaves_a_searchable_corpus`

### S145 · 关掉时工具不注册

**服务** [R44](../req/资料检索.md)

**规格** 开关在 `director.yaml` 的 `tools.corpus_search.enabled`，同其它工具。
关掉时**根本不注册**，提示词里也不提。没挂任何资料时同样不注册。

**为什么** 同 [S21](Tools.md)：注册了但没东西可查的工具，
白占提示词体量、还会被试着调一次、失败还要消耗一轮往返。

**验收** `test_the_corpus_tool_disappears_when_switched_off` · `test_no_corpus_attached_means_no_tool` · `test_the_tool_stays_while_a_corpus_is_still_building`

### S146 · 每次检索都进 trace

**服务** [R42](../req/资料检索.md)

**规格** 记下：query、两路各自的候选、rerank 之后的次序、最终返回的几段
及其在原文里的位置。

**为什么** 这条链路查不查得准，**用户是唯一判得出来的人**——他知道自己那份资料里
有什么。看不见的话，写错了他只会觉得"这模型不懂"，而不知道是没查到还是查错了。

rerank 前后的次序都要记：**它到底有没有起作用，是这条链路第一个要量的东西。**

**验收** `test_a_search_lands_in_the_trace_with_both_stages`

<a id="s147"></a>

### S147 · 提示词分两类：agent 与单次调用，并列不主从

**服务** [R31](../req/调教提示词.md)

**规格** 三条：

| | 是什么 | 怎么命名 | 有哪几份 |
|---|---|---|---|
| **agent** | 一个**角色**——会推演、会产出剧情、有自己的上下文与工具面，跨轮次一直在那儿 | **名词** | director · writer · state_manager · goal_manager |
| **单次调用** | 一次**动作**——输入固定、输出固定、不参与剧情，做完就没了 | **动词** | summarize |

1. **命名按词性分。** agent 用名词，单次调用用动词。
2. **管理界面上各占一个入口**，不是一页里的两个小标题。
3. **读写走同一套接口，路径是 `/prompts`**——两类的编辑方式完全相同，
   而挂在 `/agents` 底下会让 `summarize` 看起来是 agent 的附属。

两类都可编辑（[R31](../req/调教提示词.md) 管的是「模型会读到的字」，不分是谁在读）。

**不做** 把单次调用塞进 director 的附属提示词；不做把它们列在 agent 页的一个
小标题底下；不做给它们起名词名字。

**为什么** `summarize` 曾经叫 `summarizer`，列在 agent 名单的第五位——于是它
一直被当成第五个 agent。**那个误解会影响判断**：比如以为该给它工具面、以为它该读
世界设定。名字和入口是这条分界线唯一看得见的地方，两处都摆对，误解才不会重新长出来。

**验收** `test_an_agent_is_a_noun_and_a_single_call_is_a_verb` ·
`test_a_single_call_is_still_editable` ·
`test_the_prompt_page_separates_agents_from_single_calls`

<a id="s149"></a>

### S149 · 源文件夹是暂存区，知识库是快照，两层

**服务** [R46](../req/资料检索.md) [R47](../req/资料检索.md)

**规格** 两个位置，职责不同：

| | 谁写 | 是什么 |
|---|---|---|
| `data/corpus_files/` | **用户**，随便增删改 | 源文件。**位置固定，不给配** |
| `data/corpora/<id>/` | 系统 | 处理后的：快照 `text.txt` + 块表 + 两路索引 |

`corpus.json` 记着这份资料是从哪个源文件、哪一次内容处理来的：
`source_name` · `source_hash` · `encoding` · `processed_at`。

**块的区间指向快照，不指向活文件**（[S142](#s142) 那条"记着它在原文里的位置"，
"原文"就是这份快照）。

**不做** 让用户指定源文件夹的位置；**不做**让检索跟着活文件走。

**为什么分两层** [S143](#s143) 写着「用户可直接编辑是这个项目的取向」，而
[S142](#s142) 要求每块记住它在原文里的字符区间——**这两条本来是拧着的**：
用户改一个字，之前所有区间就整体错位，而**检索照跑，引文静默地指错位置**。

快照把它们解开：**它是一致性装置，不是归档**。区间永远指得准，
而用户那一侧仍然是几个可以随便动的普通文件。

**为什么位置不给配** 多一个可配的路径就多一类「我明明放了它怎么没看见」，
而这个功能的全部卖点就是"丢进去就行"。

**验收** `test_the_source_folder_is_created_on_startup` · `test_an_edited_file_is_flagged_but_the_old_index_still_serves` · `test_rebuilding_pulls_the_edited_file_into_the_snapshot`

<a id="s150"></a>

### S150 · 状态靠指纹对账，不靠文件系统事件

**服务** [R47](../req/资料检索.md)

**规格** 不装文件监视器。差异是**每次对账时算出来的**：列出源文件夹，
**对每个文件算一遍 sha256**，与 `corpus.json` 里记的 `source_hash` 比对。

**不做** 引入 `watchdog` 之类的文件系统监视；**不做**用 mtime 单独下结论。

**为什么** **事件会丢，指纹不会。** 进程没跑的时候用户改了文件，事件就永远
没有了——而丢了事件的后果不是"晚一点更新"，是索引**永久地**对应着一份
已经不存在的文本，且没有任何地方会报错。

这和 [S144](#s144) 的 `sweep_stale` 是同一条思路：**启动时对账，不靠通知**。
差异是个**可验证的不变量**，不是一个要靠事件流维持的状态。

**mtime 连初筛都不做。** 某些同步盘与容器里它不可靠，而同一秒内的两次同长度
修改会让 mtime 与 size 都不变——那时抄近路就是漏报。代价是每次对账把源文件
读一遍：一本一兆的小说约 5 毫秒。**真慢了再按 `(size, mtime)` 加一层进程内
缓存**（[P27](../Principles.md)：先量再改）。

**验收** `test_a_change_made_while_the_server_was_down_is_still_noticed` · `test_the_verdict_comes_from_the_hash_not_from_mtime` · `test_no_filesystem_watcher_is_installed` · `test_the_hash_follows_the_bytes_not_the_name`

<a id="s151"></a>

### S151 · 四种差异各有各的说法，更新由用户按下才发生

**服务** [R47](../req/资料检索.md)

**规格** 对账的结果是四种状态之一，**界面各标各的**：

| | 源文件 | hash | 按「更新」之后 |
|---|---|---|---|
| **同步** | 在 | 一致 | — |
| **新文件** | 在 | 没有对应条目 | 建一份新的，处理 |
| **已修改** | 在 | 不一致 | 重新处理（快照、块表、两路索引全换） |
| **源已删除** | 不在 | — | **删掉这份资料**，并从每一局的 `corpus_ids` 里摘掉 |

**系统自己不动手。**

**不做** 自动同步；**不做**发现「已修改」就拒绝检索。

**为什么不自动** 处理一份大资料要花时间也要花钱（实测 68 块 4.8 秒，
外推一本三十万字的小说约 45 秒），而**编辑器的每一次保存都会改 mtime**——
自动同步等于让保存动作触发重建。

**为什么「已修改」时旧索引仍然可用** 它对应的快照还在，区间仍然指得准，
**它只是旧，不是错**。拒绝检索等于因为用户在文件夹里改了个错别字，
就让这局故事当场少一条检索路。

**「源已删除」更新之后必须真的查不到**（[P36](../Principles.md)）：
索引是源的缓存，不是归档。这也是**唯一一处删除不需要用户在资料库里再点一次**——
他删文件那一下就是他的意思，界面要做的是把「哪几局故事挂着它」摆出来，
不是拦住他（见 [S136](#s136)）。

**验收** `test_a_new_file_shows_up_as_pending_not_as_a_corpus` · `test_an_edited_file_is_flagged_but_the_old_index_still_serves` · `test_applying_a_deletion_removes_it_from_every_session` · `test_nothing_is_processed_until_the_user_asks` · `test_a_renamed_file_only_updates_the_record` · `test_renaming_refuses_when_the_content_changed_too` · `test_renamed_and_edited_at_once_reads_as_delete_plus_add`

<a id="s152"></a>

### S152 · 处理后的文件假定没人动；对不上就报错，不将就

**服务** —（数据完整性）

**规格** `data/corpora/<id>/` 底下的东西**假定用户不改**。发现改了就
**当场报错、要求重新处理**，不试图修复、不静默降级。

判据是**数量对账**，不是逐个文件算 hash：

```
len(chunks) == bm25.total == vectors.count（有向量时）
```

**不做** 尝试从被改坏的索引里抢救；**不做**在检索路径上给这几份文件算 hash。

**为什么用数量对账** 它**免费**——这三个数在加载 `Source` 时已经在手上，
而检索路径上多算一次 600KB 的 hash 是每次都要付的钱。

而且它恰好抓得住最危险的那种不一致：**块表和向量对不上时，
向量那一路给出的 `chunk_index` 会索引到不相干的段落，而且不会报错**——
检索结果只是"变得莫名其妙"。

**为什么报错而不是修** 这一层是**可以从源文件完全重建的派生物**。
花力气去猜"用户想保留哪一半"，不如让他按一下重新处理。

**验收** `test_a_tampered_index_is_refused_not_repaired` · `test_the_counts_must_agree_across_chunks_bm25_and_vectors`

<a id="s153"></a>

### S153 · 编码探测：UTF-8 → GBK，用了哪种记下来并显示

**服务** [R46](../req/资料检索.md)

**规格** 按 UTF-8 读；解不开就按 GBK（`gb18030`）读。
**用了哪一种写进 `corpus.json` 的 `encoding`，界面上显示。**
两种都解不开：标成失败，说清是编码问题。

**不做** 猜完不说；**不做**用 `errors="replace"` 把解不开的字节替换掉。

**为什么** 中文用户的旧 txt 有相当概率是 GBK。而**乱码是不抛异常的**：
`errors="replace"` 会安静地产出一串 `�`，然后它被切块、被 embed、进索引——
**全程没有任何地方报错**，症状只是"这份资料怎么查都查不到"，
和"资料里确实没有"看起来一模一样（同 [S139](#s139) 那条罪）。

**用 `gb18030` 不用 `gbk`**：前者是后者的超集，能多认一批生僻字，而代价为零。

**验收** `test_a_gbk_file_is_read_and_the_encoding_is_recorded` · `test_no_replacement_characters_are_ever_indexed` · `test_gb18030_not_gbk_so_rare_characters_survive` · `test_a_gbk_file_is_imported_and_its_encoding_is_shown` · `test_an_unreadable_file_is_refused_with_a_sentence_for_the_user`

<a id="s154"></a>

### S154 · Word 与 PDF 解析后处理；抽不出字当场失败

**服务** [R48](../req/资料检索.md)

**规格** 纯文本一族（`.txt` `.md` `.json` `.yaml` `.csv`）直接读；
`.docx` 与 `.pdf` **解析成纯文本再走同一条路**——解析结果就是那份快照，
**检索报的位置指的是它**。

**抽出来的字太少就标成失败**，并说清"这看起来是扫描件，没有文字层"。
认不得的后缀标成「不支持」，不当二进制切块。

**不做** OCR；**不做**在解析文本里插页码标记。

**为什么扫描件要当场失败** 一份扫描的 PDF 没有文字层，抽出来是空的或者
几行页码。不拦住的话它会变成一份**"建好了但怎么查都查不到"**的资料——
而那和"这资料里确实没有"看起来完全一样。**这是乱码那条的同一种病。**

**为什么位置指解析后的文本，不插页码** 页码标记会**混进被检索的正文**：
它们会被切块、被分词、被 embed，成为噪声，而 BM25 那一路对这种高频短串
尤其敏感。代价是用户不能按页回查——补法是**界面上直接给他看解析后的那份文字**，
位置对着它数得到。

**为什么不做 OCR** 它是另一个量级的依赖（模型、二进制、平台差异），
而这条链路连基线质量都还没量过（[P27](../Principles.md)）。

**验收** `test_a_docx_becomes_plain_text` · `test_a_pdf_without_a_text_layer_fails_loudly` · `test_a_short_but_real_pdf_is_not_mistaken_for_a_scan` · `test_an_unsupported_extension_is_reported_not_indexed` · `test_an_unsupported_file_is_listed_as_such_not_hidden` · `test_no_page_markers_leak_into_the_indexed_text` · `test_an_empty_file_fails_loudly` · `test_plain_text_family_is_read_directly`

---

## 未定

- **要不要做 query 改写 / 扩展。** 检索成不成取决于 query 问得对不对，
  而改写能提高命中——但那是**又一次模型调用、又一层不确定性**。
  **要什么才能定**：先把基线跑通，量出「模型自己写的 query」的命中率有多低。
- **「多少分算不合适」每家一个数。** 专业 reranker 的分数不可跨家比较，
  所以否决阈值**不能有一个全局默认值**——那正是 [S148](#s148) 反对拿 BM25 原始分
  当阈值的同一条罪。qwen/`gte-rerank-v2` 量过一次（分界在 0.16–0.28 之间），
  另外三家一个数都没有。
  **要什么才能定**：每接一家，用同一批分三类的问法各量一次。
- **RRF 的 `k` 该取多少。** 60 是通用值，**这个项目一次都没量过**。
  **要什么才能定**：一批有标准答案的 query，比较不同 `k` 下的命中位次——
  而按 P27，先得量出同一份配置下这个指标自己的抖动有多大。
- **多份资料之间要不要分权重。** 一局故事挂三份资料时，命中的段落来自哪一份要不要
  影响排序。**先不做**——没有数据支撑任何一种权重。
