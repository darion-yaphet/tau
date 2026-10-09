---
title: "阶段 4：AgentHarness"
---

阶段 4 增加 `AgentHarness`，它是建立在纯代理循环之上的可复用有状态核心。

上一阶段引入 `run_agent_loop()`，用于协调提供商、工具、消息和事件。该循环有意基本保持无状态：
调用方传入对话列表，循环向其中追加助手和工具结果消息。

`AgentHarness` 为调用方拥有该对话记录。

## 新增内容

阶段 4 增加：

```text
src/tau_agent/harness.py
```

其中包含：

- `AgentHarnessConfig`
- `AgentHarness`
- `SimpleCancellationToken`
- `EventListener`

## harness 为何存在

Tau 的架构分为三层：

```text
AgentHarness = reusable brain
AgentSession = coding-agent environment
TUI = one possible frontend
```

harness 是可复用的有状态层，但仍不是完整编码代理应用。

这意味着它可以拥有对话状态并运行循环，但不应了解：

- 会话文件存放位置
- 斜杠命令如何工作
- 如何发现项目指令
- Rich 或 Textual 如何渲染事件
- 默认注册哪些内置编码工具

这些职责属于后续 `tau_coding` 阶段。

## Harness 配置

`AgentHarnessConfig` 存储运行循环所需的稳定输入：

- 提供商
- 模型名称
- 系统提示词
- 工具
- 可选最大轮次数

概念上如下：

```python
config = AgentHarnessConfig(
    provider=provider,
    model="gpt-4.1-mini",
    system="You are Tau.",
    tools=[...],
)
```

harness 接收该配置，并在每次提示或继续时使用它。

## 提示流程

调用 `prompt()` 会追加用户消息并启动循环。

```python
async for event in harness.prompt("Read README.md"):
    ...
```

对话记录变化如下：

```text
before prompt:
  []

after prompt appends user message:
  UserMessage("Read README.md")

after loop runs:
  UserMessage("Read README.md")
  AssistantMessage(...)
  ToolResultMessage(...)   # if tools were used
  AssistantMessage(...)
```

harness 不渲染这些事件，只将它们产出。

## 继续流程

调用 `continue_()` 会运行循环，但不追加新的用户消息。

这适用于未来的以下功能：

- 恢复会话后继续运行
- 中断后继续运行
- 由编码会话包装器决定下一次模型轮次何时发生

```python
async for event in harness.continue_():
    ...
```

## 对话快照

`messages` 属性返回不可变元组快照。

这可以防止 harness 对话记录被外部意外更改：

```python
snapshot = harness.messages
```

未来会话层可以使用该快照进行持久化。需要受控恢复时，`append_message()` 可以追加已有消息。

## 事件监听器

harness 支持事件订阅：

```python
unsubscribe = harness.subscribe(listener)
```

监听器接收与 `prompt()` 和 `continue_()` 产出相同的 `AgentEvent` 对象。

这让未来代码无需成为主事件消费者即可观察运行，例如：

- 日志
- 指标
- 会话持久化钩子
- UI 桥接

主 API 仍是对事件进行异步迭代。

## 取消

阶段 4 增加最小取消 token。

```python
harness.cancel()
```

请求取消后，当前循环可以停止并发出可恢复的取消错误。

这有意保持简单。后续 UI 层可以将其连接到按键绑定、按钮或命令处理器。

## 与循环的关系

harness 委托给 `run_agent_loop()`：

```text
AgentHarness.prompt()
        ↓
append UserMessage
        ↓
run_agent_loop(...)
        ↓
yield AgentEvent objects
        ↓
transcript grows
```

循环仍负责逐轮机制，harness 负责持久的内存对话状态。

## 阶段 4 如何支持后续阶段

### 阶段 5：内置编码工具

harness 已经接受 `AgentTool` 实例。编码工具存在后，编码应用可以把它们传入 harness 配置。

### 阶段 6：打印模式 CLI

CLI 可以创建 `AgentHarness`、调用 `prompt()` 并打印流式事件。

### 阶段 7：会话

会话持久化可以把消息恢复到 harness，并在事件后保存快照。

### 阶段 8：编码会话包装器

编码会话包装器可以围绕 harness 组合更高层行为：斜杠命令、直接 bash 命令、提示词展开、资源和持久化。

## 设计规则

`AgentHarness` 可以拥有代理状态，但必须保持与前端和编码应用无关。

依赖终端渲染、项目文件、用户配置目录或斜杠命令的行为，都应位于 `tau_agent` 之外。
