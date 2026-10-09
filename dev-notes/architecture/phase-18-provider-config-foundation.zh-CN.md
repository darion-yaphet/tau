---
title: "Phase 18：提供商配置基础"
---

本阶段开始构建 Tau 的持久提供商配置，但不添加扩展系统。

实现位于：

```text
src/tau_coding/provider_config.py
src/tau_coding/cli.py
src/tau_coding/tui/app.py
src/tau_coding/commands.py
src/tau_coding/session.py
```

## 新增内容

Tau 现在在 `tau_coding` 下具有提供商设置模型：

```python
ProviderSettings
OpenAICompatibleProviderConfig
ProviderSelection
```

设置存储在：

```text
~/.tau/providers.json
```

如果该文件不存在，Tau 会使用一个 OpenAI 兼容默认值：

```text
provider: openai
model: gpt-4.1-mini
api key env var: OPENAI_API_KEY
base URL env var: OPENAI_BASE_URL
timeout env var: OPENAI_TIMEOUT_SECONDS
retry env vars: OPENAI_MAX_RETRIES, OPENAI_MAX_RETRY_DELAY_SECONDS
```

API key 不存储在配置文件中。内置提供商使用 `credential_name` 从 `~/.tau/credentials.json` 读取 key；没有凭据名称的自定义提供商从 `api_key_env` 指定的环境变量中读取。

## 配置示例

```json
{
  "default_provider": "local",
  "providers": [
    {
      "name": "local",
      "type": "openai-compatible",
      "base_url": "http://localhost:11434/v1",
      "api_key_env": "LOCAL_API_KEY",
      "models": ["qwen", "llama"],
      "default_model": "qwen",
      "timeout_seconds": 120,
      "max_retries": 2,
      "max_retry_delay_seconds": 0.5
    }
  ]
}
```

## 运行时解析

打印模式和 TUI 启动现在从持久设置中解析提供商/模型选择：

```text
tau --provider local --model qwen
tau -p "review this" --provider local
```

省略 `--model` 时，Tau 使用已配置提供商的默认模型。省略 `--provider` 时，Tau 使用 `default_provider`。

## CLI 命令

Tau 可以列出已配置提供商：

```text
tau providers
```

Tau 还可以创建或更新 OpenAI 兼容提供商条目：

```text
tau --provider local \
  --base-url http://localhost:11434/v1 \
  --api-key-env LOCAL_API_KEY \
  --timeout-seconds 120 \
  --max-retries 2 \
  --max-retry-delay-seconds 0.5 \
  --model qwen \
  setup
```

设置选项是位于 `setup` 命令词之前的顶层选项。这样既保留了用于以初始提示启动 TUI 的 Pi 风格 `tau "prompt"` 形式，又添加了轻量设置流程。设置只写入提供商元数据；对于自定义提供商，如果指定的 API key 环境变量当前未设置，它会发出警告。

提供商 HTTP 超时可以通过 `~/.tau/providers.json` 中的 `timeout_seconds` 配置。默认 OpenAI 兼容提供商也可以读取 `OPENAI_TIMEOUT_SECONDS`。配置值会传递给 HTTPX 流式客户端，而不是在提供商适配器中硬编码超时行为。

瞬时重试行为可通过 `max_retries` 和 `max_retry_delay_seconds` 配置，默认提供商也可以通过 `OPENAI_MAX_RETRIES` 和 `OPENAI_MAX_RETRY_DELAY_SECONDS` 配置。Tau 会重试 429、5xx 等瞬时 HTTP 状态，以及在发出任何部分流内容之前发生的 HTTP 传输错误。

## 斜杠命令

斜杠命令公开活动模型配置：

```text
/model
/model <name>
/login
```

当模型属于活动提供商的已知模型时，`/model <name>` 会为当前进程的后续轮次切换活动模型。

在 TUI 中，`/model` 会打开交互式选择器。选择器可包含来自所有已配置提供商的模型，因此选择模型时可能在幕后切换活动提供商。模型命令会在验证或显示选项前刷新提供商设置。`/login` 是 TUI 中添加或刷新内置提供商的路径，并会在保存凭据后刷新提供商设置。

同一个提供商设置文件还可以存储作用域模型：

```json
{
  "scoped_models": [
    {"provider": "openai", "model": "gpt-5.5"},
    {"provider": "local", "model": "qwen"}
  ]
}
```

Tau 将这些模型视为 TUI 收藏项。编码会话会根据当前可用提供商筛选存储列表后再公开，因此过期条目留在 JSON 文件中也不会造成影响。这与 Pi 的作用域模型概念一致，同时将持久配置保留在 `tau_coding`，使可复用框架无需了解提供商设置。

## 边界

提供商设置属于 `tau_coding`，而不是 `tau_agent`。

可复用框架仍然只接收已准备好的 `ModelProvider` 和模型名称。它不了解 Tau 主目录、JSON 配置文件、凭据、环境变量或 CLI/TUI 设置行为。

## 限制

Phase 18 有意保持最小设置流程。提供商元数据通过 CLI 设置命令编辑，而不是交互式 TUI 表单。后续登录工作为内置提供商 API key 添加了 `~/.tau/credentials.json`；自定义提供商仍使用环境变量，直至 Tau 具备自定义提供商凭据表单。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_provider_config.py
tests/test_cli.py
tests/test_commands.py
tests/test_tui_app.py
```

测试验证：

- 配置缺失时回退到 OpenAI 兼容默认值
- 提供商设置通过 `~/.tau/providers.json` 往返保存
- 提供商设置和列表 CLI 行为
- 提供商 HTTP 超时和重试解析以及运行时配置转发
- 默认提供商/模型选择
- 已配置 API key 环境变量和存储凭据
- CLI 提供商/模型转发
- TUI 启动选择
- `/login` 和 `/model` 命令行为
