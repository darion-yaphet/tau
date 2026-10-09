---
title: "阶段 3：纯代理循环"
---

阶段 3 增加 Tau 的第一个真实代理引擎：连接模型提供商、对话记录和一组工具的纯循环。

循环位于：

```text
src/tau_agent/loop.py
```

称为“纯”循环，是因为它不了解 CLI、Rich、Textual、斜杠命令、本地会话文件或项目专属资源，
只处理提供商中立的 Tau 类型。

## 新增内容

阶段 3 增加：

- `run_agent_loop()`
- 助手消息和工具结果引起的对话记录变更
- 提供商事件到代理事件的转换
- 顺序执行工具
- 未知工具处理
- 工具异常处理
- 提供商错误处理
- 可选最大轮次保护

后续加固增加了用于 steering/follow-up 提示的队列排空钩子，以及对重试和思考/推理进度事件的
提供商中立转发。

## 循环输入

`run_agent_loop()` 接收：

- 一个 `ModelProvider`
- 模型名称
- 系统提示词
- 可变的 `AgentMessage` 对象列表
- 已注册 `AgentTool` 对象列表
- 可选最大轮次数
- 可选取消 token
- 可选 steering/follow-up 队列排空回调
- 用于 `QueueUpdateEvent` 的可选队列状态回调

概念上如下：

```python
async for event in run_agent_loop(
    provider=provider,
    model="...",
    system="...",
    messages=messages,
    tools=tools,
):
    ...
```

调用方拥有对话记录，循环在工作完成时向其中追加内容。

## 基本文本流程

对于不含工具调用的普通响应，流程为：

```text
agent_start
turn_start
message_start
message_delta
message_delta
message_end
turn_end
agent_end
```

内部步骤：

1. 循环请求提供商流式输出模型响应。
2. 提供商文本增量转换成 `MessageDeltaEvent`。
3. 最终提供商响应转换成 `AssistantMessage`。
4. 助手消息追加到对话记录。
5. 因为没有工具调用，循环停止。

## 工具调用流程

如果助手请求工具，循环会先执行工具再停止。

助手消息示例：

```python
AssistantMessage(
    content="I'll inspect that file.",
    tool_calls=[
        ToolCall(id="call-1", name="read", arguments={"path": "README.md"})
    ],
)
```

循环随后：

1. 找到名为 `read` 的已注册 `AgentTool`
2. 发出 `tool_execution_start`
3. 异步执行工具
4. 发出 `tool_execution_end`
5. 追加 `ToolResultMessage`
6. 开始下一次模型轮次

第二次模型轮次会看到更新后的对话记录：

```text
UserMessage
AssistantMessage with tool call
ToolResultMessage
```

并可以生成最终答案。

## 循环为何更改对话记录

循环接收可变 `messages` 列表，并向其追加新消息。

这让循环基本保持无状态。后续 `AgentHarness` 可以拥有对话记录并将其传入循环。循环无需知道消息
来自哪里或之后如何持久化。

## 提供商事件与代理事件

阶段 2 引入的提供商事件包括：

- `response_start`
- `text_delta`
- `response_end`
- `error`

阶段 3 将它们转换为更高层代理事件：

```text
ProviderResponseStartEvent  -> MessageStartEvent
ProviderTextDeltaEvent      -> MessageDeltaEvent
ProviderResponseEndEvent    -> MessageEndEvent
ProviderErrorEvent          -> ErrorEvent
```

因此 UI 层可以监听代理事件，而无需了解提供商内部实现。

后续提供商适配器还会转换：

```text
ProviderRetryEvent         -> RetryEvent
ProviderThinkingDeltaEvent -> ThinkingDeltaEvent
```

循环会转发这些事件，而不在可移植代理层嵌入提供商专属载荷。

## 排队的 steering 与 follow-up

`AgentHarness` 拥有提示队列，但 `run_agent_loop()` 负责注入位置。提供队列回调时：

- steering 消息在当前助手轮次和所有工具批次之后排空
- follow-up 消息在运行原本即将停止时排空
- 排空的消息作为普通用户消息追加到对话记录
- 下一次提供商调用前，循环为每条注入用户消息发出 `MessageStartEvent(message_role="user")`
  和 `MessageEndEvent`
- 排空后循环发出 `QueueUpdateEvent`，使前端更新待处理消息状态

不传队列回调的直接调用方保持原有行为。

## 错误处理

循环当前显式处理常见失败情况。

### 未知工具

模型请求未注册工具时，循环记录失败工具结果：

```text
Unknown tool: tool_name
```

该结果会作为 `ToolResultMessage` 追加，使模型能在后续轮次恢复。

### 工具异常

工具抛出异常时，循环会捕获异常并转换为失败的 `AgentToolResult`。

工具是隔离边界。损坏的工具默认不应使整个循环崩溃。

### 提供商错误

提供商发出错误时，循环会发出代理 `error` 事件并停止运行。

### 最大轮次

与 Pi 一样，Tau 循环默认不限制轮次数。它会继续运行，直到助手停止请求工具或发生其他正常停止条件。

需要安全上限的调用方可以传入 `max_turns`。达到配置限制时，循环发出可恢复错误。

## 重要边界

循环执行已注册工具，但不自行定义编码工具。

也就是说：

```text
tau_agent.loop:
  knows how to execute an AgentTool

tau_coding tools:
  know how to read files, write files, edit files, or run bash
```

这让 Tau 的可复用代理包独立于编码代理专属行为。

## 阶段 3 如何支持后续各层

### AgentHarness

harness 拥有对话记录、取消 token、监听器和排队的 steering/follow-up 提示，并通过 `prompt()`
和 `continue_()` 等方法调用 `run_agent_loop()`。

### 内置编码工具

编码工具是 `AgentTool` 实例。循环执行它们，但不知道工具是在读取、写入、编辑文件还是运行 shell 命令。

### 打印模式 CLI 与 TUI

渲染器和 Textual TUI 消费循环发出的 `AgentEvent`，并决定如何显示流式文本、工具活动、重试状态、
思考增量、队列状态和错误。

### 会话

会话存储会持久化循环追加的对话消息，包括工具结果以及注入后的排队用户提示。

## 设计规则

代理循环只应协调提供商流、消息、工具和事件。

需要 CLI 参数、终端渲染、本地配置路径、斜杠命令或项目资源的行为，都应位于 `tau_agent.loop` 之外。
