# 多模态读取工具结果

Tau 的 `read` 工具现在会根据文件魔数检测受支持的图像，并将其作为规范的 `ImageContent` 块与简短文本说明一同返回。处理后的附件大小上限为 5 MB，动态 PNG 文件不会被视为受支持的静态 PNG 附件。体积过大的静态图像现在会由后续的[有界读取图像处理](./read-image-processing.zh-CN.md)进行规范化。代理循环已经能保留有序的工具结果内容，因此图像数据在到达 `tau_ai` 序列化边界之前始终与提供商无关。

每个提供商适配器都会将图像块映射到自己的传输格式：

- Anthropic 将 base64 图像块嵌套在 `tool_result.content` 中。
- OpenAI Responses 和 Codex 在函数调用输出中使用 `input_image` 块。
- OpenAI Chat Completions 和 Mistral 保留文本工具结果，并在后续用户消息中附加图像。
- Gemini 3 使用多模态 `functionResponse.parts`；较旧的 Gemini 模型则接收一条独立的用户图像消息。

运行时提供商配置会根据所选模型在目录中的 `input` 元数据推导图像支持。每个提供商配置（包括独立的 `OpenAICodexProviderConfig`）在创建运行时对象时都会保留这些元数据。精简的 OpenAI Codex、OpenCode Go、OpenCode Zen 和 GitHub Copilot 目录条目现在为每个模型声明输入模态，与 Pi 生成的提供商目录保持一致。纯文本模型会收到明确的省略标记，从而避免无效的提供商请求，并让模型知道视觉上下文缺失。GitHub Copilot 的图像请求还会包含其所需的 `Copilot-Vision-Request: true` 请求头。

图像 base64 载荷已从工具结果的 `details` 移入 `content`。这样可以避免会话重复存储，并让所有前端继续渲染简短文本说明，而不会暴露 base64 数据。

## 验证

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
