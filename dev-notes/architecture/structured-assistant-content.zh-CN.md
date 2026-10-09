# 结构化助手内容与持久化思考

## 变更内容

Tau 现在将提供商响应保留为有序的助手内容块：

- 文本
- 思考/推理
- 工具调用

最终确定的 Pi 形态 `AssistantMessage.content` 数组会持久化到会话 JSONL。它的 `text`、`thinking_text` 和 `tool_calls` 属性为当前应用和扩展消费者提供便捷视图。

使用字符串 `content` 和独立 `tool_calls` 的旧版 Tau 会话行，会在 JSONL 存储边界迁移为规范的 Pi 消息形态。

## 原因

思考过去只以瞬时 `ThinkingDeltaEvent` 值存在。它可以在实时响应期间显示，但会在 `message_end` 时丢失。这使恢复后的对话不完整，阻碍忠实导出和提供商重放，也迫使 TUI 推断思考应位于何处。

Pi 将思考、文本和工具调用保存在一个有序的助手内容数组中。Tau 现在遵循相同的核心不变量，同时为现有 Python API 和扩展系统保留兼容属性。

## 提供商行为

提供商适配器暴露与 Pi 兼容的 `AssistantMessageEvent` 流。嵌套的文本/思考更新支持响应式前端；最终响应具有权威性，并在可用时包含有序块和不透明重放元数据：

- OpenAI 兼容聊天保留推理字段名称。
- Responses/Codex 在传输提供序列化推理项时保留它们。
- Anthropic 保留思考签名。
- Google 保留思维签名。
- Mistral 保留推理文本。

历史序列化器会在后续工具循环请求中重放受支持的元数据。

## 前端与扩展行为

Textual 状态加载器按顺序投影持久化块，因此恢复的思考可供 Ctrl+T 使用。在 `message_end` 时，实时临时行会通过常规对话重绘路径被最终结构化块替换。该路径仍会解析扩展自定义消息、工具调用和工具结果渲染器。

扩展收到的 Pi 形态消息和嵌套事件协议与 Tau 其他部分相同。由扩展驱动的自定义消息和 TUI 渲染钩子与助手内容保持分离，并继续通过常规重绘路径。

## 验证

运行：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
