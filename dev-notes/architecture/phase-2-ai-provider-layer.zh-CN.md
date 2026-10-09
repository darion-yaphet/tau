---
title: "阶段 2：AI 提供商层"
---

阶段 2 在 `tau_ai` 中创建 Tau 的提供商/模型流式传输层。

这一层的目的是让 Tau 能与模型 API 通信，同时避免提供商专属细节泄漏到可复用代理循环中。

## 新增内容

阶段 2 增加了以下模块：

```text
src/tau_ai/events.py              provider-neutral stream events
src/tau_ai/provider.py            ModelProvider protocol
src/tau_ai/fake.py                deterministic fake provider for tests
src/tau_ai/env.py                 environment-based provider configuration
src/tau_ai/openai_compatible.py   OpenAI-compatible chat completions adapter
```

## 提供商层为何存在

模型 API 的流式响应方式并不相同。每个提供商都有自己的请求格式、响应分块、工具调用编码、错误结构
和认证规则。

Tau 不希望代理循环了解这些细节。

因此分层为：

```text
Provider API payloads
        ↓
tau_ai adapter
        ↓
ProviderEvent stream
        ↓
tau_agent loop
```

未来的代理循环可以消费同一种 Tau 事件流，而不受后端模型影响。

## 提供商事件

提供商事件比代理事件更底层。

它们描述模型正在流式输出什么，而不是完整代理正在做什么。

当前提供商事件为：

- `ProviderResponseStartEvent`
- `ProviderTextDeltaEvent`
- `ProviderToolCallEvent`
- `ProviderResponseEndEvent`
- `ProviderErrorEvent`

简单文本响应可能如下：

```text
response_start
text_delta       "Hel"
text_delta       "lo"
response_end     AssistantMessage(content="Hello")
```

包含工具调用的响应可能如下：

```text
response_start
text_delta       "I'll inspect that file."
tool_call        ToolCall(name="read", arguments={"path": "README.md"})
response_end     AssistantMessage(... tool_calls=[...])
```

## `ModelProvider`

`ModelProvider` 是每个模型适配器必须满足的协议。

概念上它表示：

```python
provider.stream_response(
    model="...",
    system="...",
    messages=[...],
    tools=[...],
)
```

并返回提供商事件的异步流。

这一接缝让 Tau 以后能够支持多个后端。

## 虚假提供商

`FakeProvider` 重放脚本化的提供商事件。

这很重要，因为 Tau 应当能在没有以下条件时测试：

- 网络访问
- API 密钥
- 不确定的模型输出
- 提供商速率限制

之后可以给纯代理循环传入虚假提供商，使其精确发出测试所需的文本和工具调用。

## OpenAI 兼容提供商

第一个真实适配器面向 OpenAI 兼容的 `/chat/completions` API。

它处理：

- bearer token 认证
- 聊天消息格式化
- 工具 schema 格式化
- 服务器发送事件流
- 流式文本增量
- 流式工具调用参数组装
- 提供商错误

通过以下函数加载配置：

```python
openai_compatible_config_from_env()
```

使用：

```text
OPENAI_API_KEY
OPENAI_BASE_URL
```

`OPENAI_BASE_URL` 默认为：

```text
https://api.openai.com/v1
```

## 重要边界

`tau_ai` 不执行工具。

如果模型请求工具，提供商层只会发出包含中立 `ToolCall` 的 `ProviderToolCallEvent`。

工具执行属于未来的 `tau_agent` 循环：

```text
tau_ai:
  "The model requested this tool call."

tau_agent:
  "Find the registered AgentTool, execute it, append the result, continue."
```

保持此边界清晰，才能为 CLI、测试、Rich 输出、Textual 和其他前端复用同一个提供商层。

## 阶段 2 如何支持后续阶段

### 阶段 3：纯代理循环

循环消费 `ProviderEvent`，将其转换为更高层的 `AgentEvent`，执行工具，并决定是否需要下一次模型轮次。
后续提供商加固在同一转换路径中加入重试和思考增量事件，而不会让提供商了解 CLI 或 TUI。

### 阶段 4：harness

harness 拥有提供商实例，将当前对话记录传入 `stream_response()`，并公开可供打印模式、JSON 事件流、
Rich 渲染和 Textual 使用的提示/继续 API。

### 阶段 6：打印模式 CLI

CLI 从配置或命令行参数选择提供商/模型，然后在流式输出到达时显示它。

### 未来提供商

其他提供商可以实现同一 `ModelProvider` 协议，而无需更改代理循环。
