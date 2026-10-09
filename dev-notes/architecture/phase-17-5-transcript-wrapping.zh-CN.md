---
title: "Phase 17.5：TUI 对话记录换行"
---

Phase 17.5 加固 Textual 对话记录界面，使其行为更像最小聊天栈。

实现位于：

```text
src/tau_coding/tui/app.py
src/tau_coding/tui/widgets.py
```

## 新增内容

对话记录条目现在渲染为独立的彩色块，而不是 `you:` 或 `assistant:` 这样的前缀行。

显示状态保持简单：

```python
ChatItem(role="user", text="...")
```

只有 Textual 渲染器决定如何显示该条目。这保留了核心边界：

```text
CodingSession emits events
TuiEventAdapter builds display state
TranscriptView renders blocks
```

## 换行行为

`TranscriptView` 现在是一个包含各个 `TranscriptMessageWidget` 子部件的 `VerticalScroll` 容器。容器保持：

```python
min_width = 1
```

每个消息部件拥有自己的 Rich 可渲染对象和选中文本提取逻辑。这样普通用户和助手消息可以随可用终端宽度重新排版，同时避免使用一个覆盖整个对话记录的巨大选择界面。聊天块正文使用 Rich `Text` 的折叠溢出，使不间断长字符串在块内换行，而不会强制水平滚动。

工具输出和类似代码的文本会保留换行。很长的不间断文本块会被有意折叠，而不是截断。

后续 Phase 23 优化保持相同的显示状态结构，但使用块内的 Rich 可渲染对象来渲染围栏代码块、编辑补丁和助手 Markdown。格式错误或行内围栏会回退到纯文本，避免丢失对话记录内容。

## 视觉模型

每种角色都有独特的深色块样式：

- 用户
- 助手
- 工具
- 状态
- 错误

角色通过颜色表达，而非行内标签。这使 TUI 保持简洁，同时让对话记录结构易于浏览。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_tui_app.py
```

测试验证：

- 聊天条目渲染时没有 `you:`、`assistant:` 或 `tool:` 前缀
- 不间断长消息文本在窄控制台宽度内折叠
- 已挂载对话记录使用较小的 `min_width`
- 选择提取限定在单个消息部件或相邻消息部件中
