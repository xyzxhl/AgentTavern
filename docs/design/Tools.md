# director 的工具清单

> 对应 [Decisions.md](../Decisions.md) Q22。术语见 [Retrieval.md](Retrieval.md) §1。
>
> **前提**：`director → writer` 每 run 有且仅有一次（D53），因此 director 的 loop 必须有唯一、明确的终止点。

## 1. 产出与终止

```
submit_outline(outline: str) -> void
```

**终止工具，每 run 有且仅有一次。** 它是特殊的：**调用即结束 loop，并把细纲交付给 writer。**

**它只做终止与交付，不兼管状态提交**——职责单一。状态更新走独立工具（§2），因为 director 常在推演途中就得知某个事实变了，强制攒到最后反而不自然。

## 2. state

```
state_query(keys: [str]) -> {key: value}
state_write(set: {key: value}, append: {key: [item]}) -> void
```

**不预设任何维度**（D74）：键自由命名，`.` 表达从属但仅用于渲染分组。写入语义只有两种——`set`（覆盖，`null` 删除）与 `append`（追加去重）。

`state_query` 用于展开体量大的值：上下文里已给出**全部键名 + 体量小的值**（D77，与 history / worldinfo 同一模式），模型据此知道有什么可查。

键名漂移的缓解手段不是闭集枚举（枚举不出来），而是**把已有键列表回给模型让它复用**——这也是 P9 在这里的正确形态：模型做的是"从已有键里挑"，不是"发明 schema"。

`state_write` **可在 loop 中多次调用，写入进 run 内暂存区，run 完整结束时一并 commit**（D56）。否则 director 先写、writer 后跑，中途失败会造成"增量已落盘但正文不存在"。这与 D48（节点原子）一致。

**契约（E0）**：`submit_outline` 之前必须**至少调用过一次** `state_write`，**空 delta 合法**。强制这一次调用是为了区分"确实没有变化"与"模型忘了提交"——后者才是 D19 要防的失败模式。这仍是纯形状检查，成本≈0。

**schema 键名列表走注入不走工具**——它体量小，且 director 每 run 都需要知道有哪些维度可写可查。Q17 的 schema 自描述机制在此落地为默认包的一部分。

## 3. history（未验证）

```
history_search(query: str, limit: int) -> [{run_id, summary, chars}]
history_read(run_ids: [str]) -> [{run_id, outline}]
```

模式：**全量索引 + 按需展开**（D63）。近期 run 的细纲全文注入；更早的以摘要形式全部在场，模型据摘要决定展开哪些。

**这符合 D62 的准入标准**：规则只做"压缩率随距离递减"，不做相关性判断，因此在所有情况下都不会出错——全部历史都在，只是精度不同，模型知道自己能展开什么。

## 4. worldinfo（未验证）

```
worldinfo_list() -> [{ref, name, summary, chars}]
worldinfo_read(refs: [{ref, start?, max_chars?}]) -> [{ref, content}]
```

与 history **同一模式**，不是两套方案。

**索引必须覆盖全部条目**，不做任何预筛选。TauriTavern 的 `worldinfo_read_activated` 只索引"已被关键词激活的"条目，底层仍是筛选子集，按 D62 不合格。

## 5. summarize

```
summarize(source: str, max_chars?: int) -> str      # source = "run:<run_id>" 或 "wi:<ref>"
```

压缩一个**已知对象**：某轮细纲或某条设定。

签名从最初设想的 `summarize(content: str)` 改为按引用寻址，是为了让缓存真正成立——传裸文本时系统不知道该把摘要挂到哪里，"缓存复用"就落不了地。

**摘要是懒惰生成的，不预先批量产生。** 只有真正需要压缩时才付这份成本，且**结果由系统缓存复用**：对某条 worldinfo 或某个 run 的细纲生成过摘要后，该摘要即成为 §3/§4 索引中的 summary 字段，之后不必重算（同一 source 再次调用直接返回缓存，不打模型）。

**索引中尚无摘要的条目回退到"前 N 字 + 字数"。** 这仍满足 D62——模型知道有这个东西存在、知道它的体量，只是精度低一点，随时可以展开或调 `summarize`。

这条同时回答了"索引里的 summary 从哪来"：不是系统预先生成，是模型在实际需要时产出、系统顺手缓存。

## 6. 用户交互

```
ask_question(question: str, options: [str]) -> {selected: str, note?: str}
```

调用积极性由模型剧情控制权决定（D58/D39）。**只考虑逻辑时间，不设超时**（D66）——等待期间逻辑上什么都没发生，director 上下文不会过期，从中断点续跑即可（suspend/resume，非 timeout/retry）。

**多候选不需要新工具**：director 在上下文中推演出多个走向 → `ask_question` 给选项 → 用户选 → `submit_outline` 提交选中那份。"submit_outline 有且仅有一次"因此仍然成立。

注意 `ask_question` 与 `submit_outline` 的区别：前者是普通 tool call（结果回到 loop 继续），后者终止 loop。

## 7. 明确不提供

| 不提供 | 理由 |
|---|---|
| **任何读正文的工具** | D54 的基础。director 能覆盖五倍历史跨度，正是因为它从不读正文 |
| **调用 writer 的工具** | D54。工具返回值会回流，正文回流即摧毁隔离 |
| **生成子 agent** | [../Principles.md](../Principles.md) P16–P17，在编排层强制禁止，不靠 prompt 恳求 |
| **修改元指令 / 控制维度** | 那是用户的（D15 显式优先），模型不得自行调整 |
| **写文件 / 执行代码** | 无此需求，且是攻击面 |

## 8. 横切约束

- **返回体量上限**：每个工具单次返回设硬上限，防止一次调用撑爆上下文。
- **调用轮数上限**：软上限（提示模型收敛）+ 硬上限（编排层强制 `submit_outline`）。D7 已放弃流式，tool 往返直接加在用户等待上。
- **重资源一律全量索引 + 按需展开**（D63），索引中**必须带体量**，让模型知道拉全文的代价。

## 9. 已知缺口：director 拿不到角色的原话 —— 接受限制

细纲是"无文风约束的文章"，措辞由 writer 定，因此**原话只存在于正文里**。director 无法回答"她上次说的是'永远'还是'暂时'"这类需要精确引用的问题。

**结论：接受限制，不开机制口子。** 两个被否的选项：

- 开窄查询 `text.quote(run_id, hint)`——D54 禁的是正文回流上下文，几十字窄查询严格说不违反，但口子一开 director 会频繁调用，逐渐等价于读正文。
- 让 writer 把关键台词回写进细纲——加重 writer 职责，与"纯渲染"定位冲突。

**缓解手段是 prompt，不是机制**，两个方向同时用力：

1. 提高 **writer 对细纲的遵循度**——少自由发挥，少产生细纲里没有的措辞。
2. 提高 **director 生成细纲的详细度**——关键台词直接写进细纲，措辞就不再只存在于正文。

实在出错则**依赖用户手动纠正**（D65）。这与写入侧的处置态度一致（D64 乐观处理 + 用户兜底）——不为不可自愈的边缘情况增加结构复杂度。
