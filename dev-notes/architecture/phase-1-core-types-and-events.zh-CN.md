---
title: "Phase 1：核心类型与事件"
---

Phase 1 为 Tau 未来的代理循环建立共享词汇。

在 Tau 能与模型对话、执行工具、保存会话或渲染 UI 之前，每一层都需要对系统中流动的数据结构达成一致。这正是核心消息、工具、JSON 和事件类型所提供的能力。

## 新增内容

Phase 1 在 `tau_agent` 中添加了四个模块：

```text
src/tau_agent/types.py      JSON-like shared type aliases
src/tau_agent/messages.py   transcript message models
src/tau_agent/tools.py      tool calls, tools, and tool results
src/tau_agent/events.py     event models emitted by the agent layer
```

这些模块与提供商和 UI 无关。它们不会提及 Anthropic、OpenAI、Rich、Textual、Typer、本地文件或终端渲染。

## 消息存在的原因

消息代表对话记录：代理和模型共同处理的对话状态。

Tau 当前定义：

- `UserMessage`
- `AssistantMessage`
- `ToolResultMessage`
- `AgentMessage`

### `UserMessage`

`UserMessage` 存储用户编写的文本。

```python
UserMessage(content="Read README.md")
```

之后，每当用户提交提示时，框架都会追加一条此类消息。

### `AssistantMessage`

`AssistantMessage` 存储助手文本和可选的工具调用。

```python
AssistantMessage(
    content="I'll inspect the README.",
    tool_calls=[...],
)
```

这一点很重要，因为助手可以在一轮中完成两件事：

1. 生成可见文本
2. 请求执行工具

未来的代理循环会将模型流式输出收集到 `AssistantMessage` 中。如果消息包含工具调用，循环会执行这些工具并继续运行。

### `ToolResultMessage`

`ToolResultMessage` 存储特定工具调用的结果。

```python
ToolResultMessage(
    tool_call_id="call-1",
    name="read",
    content="file contents...",
    ok=True,
)
```

模型需要对话记录中的工具结果，以便在工具运行后继续推理。例如：

```text
User asks to read README.md
Assistant calls read
Tau executes read
Tau appends ToolResultMessage
Assistant sees file contents and answers
```

### `AgentMessage`

`AgentMessage` 是所有消息类型的联合。它使后续 API 能以下列形式接受对话记录：

```python
list[AgentMessage]
```

而无需关心每个条目来自用户、助手还是工具。

## 工具类型存在的原因

工具是助手请求 Tau 与环境交互的方式。

Tau 当前定义：

- `ToolCall`
- `AgentTool`
- `AgentToolResult`

### `ToolCall`

`ToolCall` 不是工具本身，而是使用类似 JSON 的参数运行指定名称工具的请求。

```python
ToolCall(
    id="call-1",
    name="read",
    arguments={"path": "README.md"},
)
```

未来的提供商适配器会将提供商专用的工具调用载荷转换为 Tau 的这种中立结构。

### `AgentTool`

`AgentTool` 描述 Tau 可以向模型公开的工具。

它包含：

- `name`
- 便于人类阅读的 `description`
- `input_schema`
- 异步 `executor`

编码代理层之后会注册以下内置工具：

- `read`
- `write`
- `edit`
- `bash`

但可移植的代理循环只需知道：可以使用参数执行 `AgentTool`，并获得 `AgentToolResult`。

### `AgentToolResult`

`AgentToolResult` 是工具执行的结构化输出。

```python
AgentToolResult(
    tool_call_id="call-1",
    name="read",
    ok=True,
    content="file contents...",
)
```

之后，循环会将工具结果转换为 `ToolResultMessage` 对象并追加到对话记录中。

## JSON 类型存在的原因

工具参数和提供商载荷需要类似 JSON，因为模型 API 使用 JSON 交换结构化数据。

Tau 为以下内容定义共享别名：

- `JSONPrimitive`
- `JSONValue`
- `JSONObject`

这些别名使工具 schema、工具参数、事件数据以及与提供商无关的载荷保持类型安全，同时不绑定到特定提供商 SDK。

## 事件存在的原因

事件是可复用代理层在不知道监听者身份的情况下报告进度的方式。

这是关键设计：

```text
Agent loop emits events
        ↓
CLI print mode consumes events
Rich renderers consume events
Textual TUI consumes events
Tests can inspect events
```

Tau 当前为以下内容定义了事件：

- 代理开始/结束
- 轮次开始/结束
- 助手消息开始/增量/结束
- 工具执行开始/更新/结束
- 错误

## 未来的事件流

未来一次提示运行可能产生以下序列：

```text
agent_start
turn_start
message_start
message_delta       "I'll inspect the file."
message_end         AssistantMessage(... tool_calls=[read])
tool_execution_start
tool_execution_end  AgentToolResult(...)
turn_end
turn_start
message_start
message_delta       "The README says..."
message_end         AssistantMessage(... no tool calls)
turn_end
agent_end
```

关键在于，同一事件流可以驱动多个前端。核心循环不需要 `print()`、Rich 面板或 Textual 部件。

## Phase 1 如何支持后续阶段

### Phase 2：提供商层

提供商将外部模型流转换为 Tau 消息、工具调用和与提供商无关的事件。

### Phase 3：纯代理循环

循环使用这些类型来：

1. 向提供商发送消息
2. 收集助手输出
3. 检测工具调用
4. 执行 `AgentTool`
5. 追加 `ToolResultMessage`
6. 在整个过程中发出 `AgentEvent`

### Phase 4：框架

框架维护由 `AgentMessage` 对象组成的对话记录，并公开 `prompt()` 和 `continue_()` 等较高层方法。

### Phase 6 及以后：UI

打印模式、Rich 渲染、JSON 事件流和 Textual 消费 `AgentEvent`，而不是深入循环、提供商或框架内部。

### Phase 7：会话

会话持久化会保存并重放消息对象和相关状态变更。消息模型为持久化层提供稳定基础。

## 设计规则

如果一个类型属于可复用的代理“大脑”，它就位于 `tau_agent`。如果一个类型了解命令行行为、项目文件、斜杠命令、提示或 UI 渲染，它就属于核心代理包之外。
