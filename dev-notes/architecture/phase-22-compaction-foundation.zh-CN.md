---
title: "阶段 22：压缩重放基础"
---

此阶段启动了 Tau 的压缩和上下文管理工作。它现在包括模型驱动的 Pi 风格摘要、自动压缩时保留
近期上下文，以及上下文溢出触发的重试行为。

实现位于：

```text
src/tau_agent/session/entries.py
src/tau_agent/session/memory.py
src/tau_coding/context_window.py
src/tau_coding/session.py
src/tau_coding/commands.py
```

## 新增内容

`CompactionEntry` 现在会在会话重放期间发挥作用。

当 `SessionState.from_entries()` 看到现代压缩条目时，它会：

1. 插入一条提供商中立的摘要消息
2. 在完整的当前根到叶条目路径中找到 `first_kept_entry_id`
3. 按路径顺序保留位于该边界及其之后、会产生上下文的条目
4. 保持原始只追加条目不变

这与 Pi 的包含首个保留项语义一致。如果边界缺失或无法找到，重放不会保留压缩前消息，但仍会
包含后续条目。采用此编码前写入的 Tau 会话使用 `replaces_entry_ids`；非空旧式列表具有优先级，
并保留原有的任意集合重放行为。

摘要消息当前使用以下稳定格式：

```text
Previous conversation summary:
<summary>
```

## 存在原因

Tau 需要通过压缩减少当前提供商上下文，同时保留完整会话文件。这保持了 Pi 的只追加会话属性：

```text
session file = durable history
SessionState.messages = reconstructed active context
```

手动 `/compact` 可以追加 `CompactionEntry` 值，无需编辑或删除旧条目。新记录持久化一个首个保留
边界，而不是所有已摘要条目 ID 的列表。

## 上下文大小估算

Tau 现在在 `tau_coding` 中提供确定性的近似上下文大小辅助函数：

```python
estimate_text_tokens(...)
estimate_message_tokens(...)
estimate_tool_tokens(...)
estimate_context_tokens(...)
```

这些辅助函数有意使用粗略的字符估算，而不是提供商专属 tokenizer。`/status` 将当前估算显示为：

```text
Estimated context tokens: <count>
```

这为自动压缩阈值提供了稳定的应用层基础组件，而不会向 `tau_agent` 加入 tokenizer 策略。

Tau 默认使用以下公式启用 Pi 风格自动压缩：

```text
model context window - 16384 reserve tokens
```

内置模型带有已配置的上下文窗口元数据，未知或自定义模型回退到 `128000` token 窗口。可以通过
以下参数覆盖单次运行的阈值：

```bash
tau --auto-compact-threshold 100000
```

当当前上下文估算在新提示词之前或模型响应之后超过有效阈值时，Tau 会请求当前提供商生成结构化摘要，
追加 `CompactionEntry`，然后重建内存对话记录。Tau 在遇到看起来像上下文溢出的提供商错误后，
还会尝试一次压缩并重试。

## 手动压缩

Tau 现在支持在 TUI 中由模型生成手动压缩：

```text
/compact [instructions]
```

该命令使用 Tau 内置的 Pi 风格压缩提示词。可选指令会作为额外关注点追加到提示词中。生成的摘要
存储在 `CompactionEntry` 中，并成为按文件顺序的当前尖端。随后 Tau 重放会话，并替换内存中的
harness 对话记录，供后续轮次使用。

当前提示词、触发条件和已知限制请参阅 [上下文压缩](../context-compaction.md)。

## 边界

此基础位于 `tau_agent`，因为重放会话条目属于可移植 harness 的职责。它不了解斜杠命令、
Textual、Rich、Tau 主目录路径、token 阈值或由哪个模型生成摘要。

Token 估算、摘要生成、溢出分类和命令体验位于 `tau_coding`。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_session.py
tests/test_context_window.py
tests/test_commands.py
tests/test_coding_session.py
tests/test_tui_app.py
```

测试验证：

- 压缩条目通过 JSONL 往返转换时不会写入空的旧式 ID 列表
- 线性重放会包含首个边界条目，并处理各种边界深度
- 分支重放只在当前分支路径上应用压缩
- 基于夹具的旧式重放保留替换列表优先级
- 上下文大小估算具有确定性
- `/status` 包含估算上下文 token 数量
- `/compact [instructions]` 请求模型生成压缩
- 手动压缩会持久化条目并重建后续轮次上下文
- 选择启用的自动压缩会在超过阈值时于提示词之前和响应之后运行
- 溢出触发的压缩会在可能时重试提供商调用一次
