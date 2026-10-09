---
title: "阶段 20.1：上下文统计刷新"
---

阶段 20.1 加强了 Tau 的上下文统计，并让 TUI 刷新遵循更新当前代理对话记录的同一事件流。

## 新增内容

`tau_coding.context_window` 现在公开 `ContextUsageEstimate`，这是一个包含以下字段的结构化快照：

```text
total_tokens
system_tokens
message_tokens
tool_tokens
message_count
tool_count
```

`CodingSession.context_usage` 返回当前提供商上下文的该快照。旧的
`context_token_estimate` 属性仍作为兼容性快捷方式保留，供只需要总量的命令、组件和测试使用。

`/status` 仍会输出：

```text
Estimated context tokens: <count>
```

它还包含系统提示词、消息和工具定义的稳定 token 明细。

## 提示词消息事件

提示词运行现在会为用户提示发出可移植的消息事件：

```text
agent_start
turn_start
message_start user
message_end user
...
```

这与 Pi 的循环行为一致：先把提示词加入上下文，再由事件流在助手开始流式输出前报告该消息。
直接调用底层 `run_agent_loop()` 的代码不受影响；它们仍传入已经准备好的消息列表，
并且只接收助手和工具循环事件。

## TUI 刷新边界

Textual 应用不再预先把提交的用户提示加入可见对话记录。`TuiEventAdapter` 改为根据流式事件
渲染用户、助手和工具消息。由于 `_refresh()` 会在每个事件后运行，侧边栏和紧凑会话行现在会在
以下情况后观察到上下文变化：

- 提交用户消息
- 助手消息完成
- 插入工具结果
- 手动或自动压缩
- 恢复会话或替换为新会话

这让可复用的 `tau_agent` 包保持与 UI 无关：它只发出可移植事件。Textual 专属渲染仍位于
`tau_coding.tui`。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_agent_harness.py
tests/test_context_window.py
tests/test_coding_session.py
tests/test_tui_adapter.py
tests/test_tui_app.py
```

测试验证上下文使用量会在常规轮次、压缩和恢复会话后重新计算，并验证 TUI 刷新能在每个流式
消息或工具边界看到变化后的上下文估算值。
