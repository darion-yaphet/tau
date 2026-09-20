# 第五篇：Agent 为什么能持续工作——上下文、记忆与会话树

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

Agent 做得越多，给模型的历史就越长：用户目标、代码片段、工具输出、失败原因、文件改动和后续指令都会占用上下文窗口。把所有内容永远原样塞回下一次请求，迟早会超出模型限制，或者让最近的任务淹死在旧细节里；直接丢掉旧内容，又会让 Agent 忘记自己已经干完的活。

Tau 的答案不是泛化的“无限记忆”，而是把三件事分开：**当前上下文**是下一次模型请求携带的消息；**持久历史**是可审计、可恢复的 JSONL entry；**会话树**决定从哪条历史路径重建当前状态。压缩只改变第一件事在重放时的形状，不覆写第二件事。本篇的原则可以记成一句：**写摘要，不改原始记录。**

## 先分清：上下文不是会话，也不是长期记忆

`AgentHarness.messages` 是本轮 Agent 要继续使用的消息序列。它包含用户消息、助手内容、工具调用与工具结果；Provider 在下一轮请求时会把其中可重放的部分编译为模型上下文。这是一份工作记忆，受模型窗口和工具 schema 体积的限制。工作记忆挂多了，模型就看不见当前任务。

持久会话则由 `src/tau_agent/session/` 的 `SessionEntry` 组成。除了消息，entry 还记录模型与 thinking level 的变更、标签、会话元数据、压缩、分支摘要、活跃叶节点和扩展自定义数据。这些条目说明“发生过什么”，不等于每一条都会再次发送给模型。

因此，Tau 的持久会话不是自动学习用户偏好的长期记忆库。恢复会话是重新构造该会话活跃分支的状态；跨会话的偏好、项目说明或 skills 属于别的资源系统。区分这几个概念能避开一个常见误解：档案在，不等于这一轮要把整本族谱发给模型。

## 上下文大小如何估算

`src/tau_coding/context_window.py` 将 active context 拆成 system prompt、消息和工具定义。没有 Provider 用量时，它按每四个字符约一个 token 给出确定性估算，并给消息和工具加上固定开销。这不是精确 tokenizer，却足以让界面和自动策略在未知模型上工作。

若最新成功的助手消息报告了 usage，Tau 会优先采用这份 Provider 用量，并只估算之后新增的消息与动态工具。Provider 已经知道它看见的完整前缀，再去估算旧历史就成了双重计算；而一旦压缩等操作插进了更新的前缀，较旧的 usage 又算过期。出错或中途中止的响应，也当不了可靠的上下文锚点。

所以，context accounting 是“尽可能采用真实服务反馈，缺失时再用稳定近似”，不是宣称一个对所有模型都精确的数字。它的目标是触发合理策略、向用户解释成本，而不是替代 Provider 的真实限制。

## 压缩是新摘要，不是历史重写

当需要压缩时，`CodingSession` 将要替换的 active messages 交给当前 Provider 生成结构化摘要。摘要提示词要求保留目标、约束、已完成与进行中的工作、关键决策、下一步，以及精确的文件路径、函数名、错误信息；这次摘要请求不给工具。

随后 `CodingSession._append_compaction()` 不修改任何旧 JSONL 行，而是追加：

```text
CompactionEntry(summary, replaces_entry_ids, first_kept_entry_id, tokens_before)
LeafEntry(entry_id=compaction.id)
```

重放 `SessionState` 时，`CompactionEntry` 才把被列出的旧消息替换为一条 `Previous conversation summary` 用户消息。旧的 `MessageEntry` 仍保留在文件中，因而您仍然可以审计压缩依据、导出原始记录，或者在树的其他分支继续用原有事实。

手动 `compact()` 可以概括当前 active context；`compact_detailed()` 与自动压缩走的是保留最近消息的方案。当前默认策略会为模型窗口预留 16,384 tokens（`DEFAULT_COMPACTION_RESERVE_TOKENS`），并尽量保留约 20,000 tokens 的最新上下文，同时把切分点对齐到合理的用户消息边界。目标不是把历史压到最小，而是在旧的工作摘要与最近的工具细节之间，留出一个还能接着推理的交界。

若摘要模型不可用或返回空内容，压缩失败会记成一条诊断，不该把当前 turn 一口吞掉。碰上上下文溢出，session 还会试一次“压缩后重试”的恢复路径；原始的 overflow 错误也不会被悄悄算作成功。

**反例：** 压缩时直接改写或删除旧 JSONL 行，把长对话变成一条摘要。当前分支的窗口是腾出来了，但您再也无法审计压缩依据，也无法从压缩前的某个用户消息再开一条分支——那条消息在文件里已经不存在。Tau 选择追加 `CompactionEntry`，只在重放时替换形状。可以对照 `tests/test_session.py` 和 `tests/test_coding_session.py`：压缩后上下文变小，原始历史仍在；自动压缩只替换活跃路径的消息。

另一个反例：每次请求都把 JSONL 里的全部 entry 原样发给模型。持久档案会线性增长，工作记忆却必须有上限。文件还在，只说明您没删；模型看见，才算这一轮的配额。

## 为什么 JSONL 能表示会话树

JSONL 是一行一条 entry 的追加日志，看上去像线性文件；Tau 通过每个 entry 的 `id` 与可选 `parent_id`，再加 `LeafEntry` 指向当前活跃节点，把它变成可分支的历史树。`path_to_entry()` 从叶节点沿 parent chain 回溯并反转，得到当前分支从根到叶的路径；重复 ID、缺失父节点或循环会成为明确的 session tree 错误。

分支不是复制整个文件，也不是删除“走错”的后续内容。`CodingSession.branch_to_entry()` 在选定可分支 entry 后追加一个新的 leaf，刷新 state，并将 Harness 消息替换为该活跃路径的重放结果。若从用户消息分支，系统会回到该消息之前并预填输入，好让您改写请求；若要求总结被放弃的分支，则会追加 `BranchSummaryEntry`，把那段探索压成摘要带回新的上下文。走错的路不必删掉，记一笔就行。

这样会话就能同时撑住两种需求：保留原始尝试，供审计或回看；从较早节点重新探索，又不让旧分支继续污染当前的模型上下文。树解决的是“选择哪段历史继续”，压缩解决的是“这段历史在窗口中占多大”；二者相关，但不是同一件事。

## JSONL 的持久化边界

`JsonlSessionStorage` 将 entry 序列化为 Pi 兼容的 JSONL，每次 append 后可顺序读回。持久化格式的迁移也全都关在 `session/jsonl.py` 里：旧版 assistant 字符串会迁移为有序内容块，旧版 tool 消息会迁移为 `ToolResultMessage`。运行时模型因此可以保持严格，历史文件仍能尽量兼容。

这一安排比“把 Python 对象直接 pickle”更适合 Agent 会话：人能看、能导出，失败能定位到具体行，新增 entry 类型也用不着覆写整个文件。代价是恢复必须重放 entry；Tau 接受这个代价，以换取可解释、可分支的历史。

至于谁负责把消息写进这份文件：`JsonlSessionStorage` 只管追加与读回，写入时机由 `CodingSession` 通过 Harness 事件装配的持久化 listener 决定——消息结束时才把消息和 leaf entry 落盘。这条装配线属于产品层，第六篇会展开它。

## 测试保护哪些不变量

`tests/test_context_window.py` 验证 token fallback、Provider usage 锚点、过期 usage 的失效、压缩提示词和自动压缩阈值。`tests/test_session.py` 验证 JSONL 往返、旧格式迁移、树路径与压缩/分支摘要重放。`tests/test_coding_session.py` 则覆盖活跃分支恢复、分支后仍保持活跃模型、自动压缩只替换活跃路径的消息等行为。

这些测试锁定的核心不是“摘要文案是否漂亮”，而是状态是否可解释：压缩后上下文变小，原始历史仍在；切换分支后当前消息改变，其他分支仍在；恢复 session 后，Harness 继续的是正确的活跃路径。

## 三分钟核对

```bash
uv run pytest tests/test_session.py tests/test_context_window.py -q
```

看 JSONL 往返、旧格式迁移、树路径与压缩重放是否全部通过——本篇的核心不变量就锁在这两个文件里。

```bash
grep -n "CompactionEntry\|parent_id" src/tau_agent/session/entries.py
```

看 entry 类型定义：`parent_id` 是树的边，`CompactionEntry` 是追加式压缩的凭证，二者都在，说明“写摘要，不改原始记录”仍成立。

## 小结

Tau 让 Agent 持续工作，靠的不是无限堆积聊天记录，而是一套可重放的状态管理：JSONL 保存发生过的全部 entry，树选择当前探索路径，摘要压缩模型当前需要的旧上下文。模型可以忘记冗长细节，系统不能失去任务目标、关键决策与可审计的历史。

下一篇从这些通用状态回到产品层，看 `CodingSession`、CLI、资源加载和可替换 TUI 怎么把这颗可移植的 Agent 大脑，变成真正能用的编码环境。
