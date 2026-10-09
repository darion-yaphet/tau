---
title: "提供商重试事件"
---

Tau 在 `tau_ai` 中重试瞬时提供商故障，因为那里可以看到 HTTP 状态码和传输异常。这样既能让重试分类留在 `tau_agent` 之外，也能让可移植的 Agent 循环呈现进度。

## 新增内容

提供商适配器可在重试失败请求之前发出 `ProviderRetryEvent`。该事件包含下一次尝试编号、总尝试次数、延迟、人类可读消息和结构化诊断数据。

`run_agent_loop()` 会将该提供商事件映射为 `RetryEvent`，即渲染器和 TUI 适配器消费的、与提供商无关的 Agent 事件。

## 行为

OpenAI 兼容、Anthropic 和 OpenAI Codex 订阅提供商会重试 `408`、`409`、`429` 和 `5xx` 响应等瞬时状态码，然后才呈现最终提供商错误。默认重试两次，总共尝试请求三次。

Anthropic 和 OpenAI Codex 适配器还会重试瞬时的*流内*故障。这两个 API 都可能先返回 HTTP 200，然后发送 SSE 错误事件。对于 Anthropic，可重试错误类型为 `api_error`、`overloaded_error` 和 `rate_limit_error`。Codex 会依据过载、服务不可用、速率限制、内部/服务器错误和超时等瞬时标记，对 `error` 和 `response.failed` 事件进行分类。

当流内错误在内容或思考增量之前到达时，适配器会发出 `ProviderRetryEvent`，并在同一 `max_retries` 预算下重新发起请求。部分内容之后的错误，以及 `authentication_error` 或 `invalid_api_key` 等非瞬时错误，会保持为终止错误，以免重放可见输出或工具调用。

退避时间短、呈指数增长，并受 `max_retry_delay_seconds` 上限约束。退避延迟期间会检查取消状态，因此 Escape/TUI 取消无需等待整个重试休眠结束。

## 渲染

对话和 TUI 渲染器会将重试进度显示为低调的状态输出。最终文本模式会忽略重试进度，只打印最终助手响应或最终错误。

## 边界

`tau_agent` 不决定某个 HTTP 响应是否可重试。它只将 `RetryEvent` 作为可移植进度转发。提供商特有的详情保留在适配器的 `data` 负载中用于诊断。
