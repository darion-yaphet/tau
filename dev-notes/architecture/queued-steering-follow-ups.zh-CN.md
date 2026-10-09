---
title: "排队的引导消息与后续消息"
---

此切片在 Agent 运行期间增加了 Pi 风格的消息排队。

## 新增内容

`tau_agent.AgentHarness` 现在拥有两个提示词队列：

- 引导消息：在当前助手轮次及任何工具批次之后注入
- 后续消息：只在运行原本将要停止时注入

harness 暴露 `steer()`、`follow_up()`、`clear_queues()`、`queued_messages`、`pending_message_count` 和 `is_running`。直接重叠的 `prompt()` 与 `continue_()` 调用会被拒绝，防止调用方从两个活动运行中修改同一份对话记录。

`run_agent_loop()` 接受与提供商无关的队列排空回调。队列排空时，循环会把排队的用户消息追加到普通提示词所用的同一消息列表，并在下一次提供商调用之前发出常规用户 `MessageStartEvent` 和 `MessageEndEvent` 值。

`QueueUpdateEvent` 向前端报告待处理的引导和后续文本。Textual TUI 用它显示状态行队列计数。

## 编码会话边界

`CodingSession.prompt()` 保持普通的非运行中提示词不变。harness 运行期间，调用方必须传入以下一种形式：

```python
session.prompt(text, streaming_behavior="steer")
session.prompt(text, streaming_behavior="follow_up")
```

会话在入队前展开 `/skill:<name>` 提示词文本。它不会在排队时持久化消息。持久化仍发生在当前运行结束之后，此时 harness 已将排队的用户消息注入对话记录。

## TUI 行为

内置 Textual 前端映射如下：

- 运行期间按 `Enter` 加入引导队列
- 运行期间按 `Alt-Enter` 加入后续队列
- 运行期间在空提示词上按 `Up` 编辑最新的排队后续消息

运行继续时，待处理队列会显示在提示词上方。如果排队了多条后续消息，编辑操作会把最近入队的后续消息取回提示词中，并将其从队列移除。一旦排队消息被注入，它会作为普通用户消息出现在对话中，待处理队列显示也会更新。

## 边界

队列所有权位于 `tau_agent`，提示词展开和持久化保留在 `tau_coding`，Textual 只决定快捷键和呈现方式。提供商适配器不知道排队消息的存在；它们会在下一次提供商调用时收到更新后的对话记录。

## 测试

该行为由以下测试覆盖：

```text
tests/test_agent_loop.py
tests/test_agent_harness.py
tests/test_coding_session.py
tests/test_tui_adapter.py
tests/test_tui_app.py
```
