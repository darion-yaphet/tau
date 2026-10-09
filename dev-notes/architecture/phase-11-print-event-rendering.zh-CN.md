---
title: "Phase 11：打印与事件渲染模式"
---

Phase 11 为 Tau 的非交互式 CLI 模式添加一个小型渲染边界。

实现位于：

```text
src/tau_coding/rendering/
```

## 新增内容

Tau 现在具有在可移植框架层之外消费 `tau_agent` 事件的事件渲染器：

- `FinalTextRenderer` 只打印助手的最终答案。
- `JsonEventRenderer` 将每个代理事件写为 JSON Lines。
- `TranscriptRenderer` 保留 Tau 之前的实时对话记录行为。
- `PrintOutputMode` 选择 `text`、`json` 或 `transcript` 输出。

CLI 通过以下方式公开它：

```bash
tau --output text "summarize this project"
tau --output json "summarize this project"
tau --output transcript "summarize this project"
```

`text` 是默认模式。

## 存在原因

Pi 将终端输出模式保留在可复用代理核心之外：

```text
agent/session emits events
print mode consumes events for final text or JSON output
interactive mode uses a separate TUI layer
```

Tau 现在遵循同一架构边界：

```text
tau_agent     portable event-producing harness
tau_coding    CLI mode selection and event rendering
future TUI    another consumer of the same event stream
```

这使 `tau_agent` 不依赖 Typer、Rich、Textual、终端行为和 UI 策略。

## 输出模式

### 文本模式

文本模式是 Pi 风格的打印模式。它消费完整事件流，并在运行结束后只打印最终助手消息。

它在显示方面忽略工具事件；出现不可恢复的 `ErrorEvent` 时返回失败。

### JSON 模式

JSON 模式每行写入一个序列化事件：

```json
{"type":"message_start","message_role":"assistant"}
{"type":"message_delta","delta":"Hello"}
```

这为脚本和未来集成提供稳定的事件流，无需依赖面向人类的终端格式。

### 对话记录模式

对话记录模式保留 Tau 之前的打印模式行为：

- 助手增量流式输出到 stdout
- 工具开始、进度更新和工具结果渲染到 stderr
- 成功和失败的工具结果内容渲染到 stderr
- 错误渲染到 stderr

对话记录渲染器使用 Rich 生成人类友好的 stderr 输出，同时保持默认 `text` 模式适合脚本使用。

在 Tau 尚无完整交互式 TUI 时，这种模式很有用。

## CLI 集成

`run_print_mode()` 现在接受：

```python
output: PrintOutputMode = PrintOutputMode.text
```

它使用以下代码创建渲染器：

```python
create_event_renderer(output)
```

并将来自 `CodingSession.prompt()` 的每个事件发送给渲染器。会话包装器会持久化这次单次对话记录，并使打印模式与 TUI 使用相同的提示、资源和工具环境。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_rendering.py
tests/test_cli.py
```

测试验证：

- 对话记录流式行为
- 最终文本行为
- JSON Lines 输出
- 不可恢复错误处理
- CLI 输出模式集成

## 非目标

本阶段不添加：

- Textual UI
- 键盘输入
- 会话选择器
- diff 查看器
- Markdown 渲染器
- 主题系统

这些属于后续前端阶段。

## 下一阶段

下一阶段可以在同一事件边界后构建 Textual TUI，或先在 `tau_coding` 内添加可选 Rich 样式，而无需修改 `tau_agent`。
