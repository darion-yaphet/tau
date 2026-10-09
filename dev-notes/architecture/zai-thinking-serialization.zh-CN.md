# Z.AI 思考序列化

## 变更内容

当 `supportsReasoningEffort` 为 false 时，针对 Z.AI 的 OpenAI 兼容请求现在会保留 Tau 的逻辑思考状态。请求使用 Z.AI 文档中的 `thinking` 对象：

```json
{"thinking": {"type": "enabled"}}
```

并在 Tau 的 `off` 模式下发送 `type: "disabled"`。它不再发送无法识别的 `enable_thinking` 字段。

Z.AI 文档将 `reasoning_effort` 描述为仅 GLM-5.2 及更新模型支持的独立字段，其值取决于模型。Tau 只在兼容性元数据表明所选模型支持时才发出该字段。这既为 GLM-5.1 等模型保留提供商级防护，也允许在目录证据支持时明确按模型选择启用。

## 原因

旧的请求构造器会在 `supportsReasoningEffort` 为 false 时、提供商序列化之前，把逻辑 effort 过滤为 `None`。因此，即使用户选择了 `high`，Z.AI 的序列化器也会认为思考已禁用。将逻辑开关处理与原始 effort 字段支持分离，可以修复状态丢失，同时不会为其他提供商启用不受支持的字段。

## 验证

离线 `httpx.MockTransport` 测试覆盖：

- Z.AI 启用和禁用思考的负载；
- 带有和不带有独立受支持 effort 字段的 Z.AI 模型；以及
- 省略不受支持的 OpenAI `reasoning_effort` 字段的现有防护。

协议证据：

- <https://docs.z.ai/guides/capabilities/thinking>
- <https://docs.z.ai/api-reference/llm/chat-completion>
