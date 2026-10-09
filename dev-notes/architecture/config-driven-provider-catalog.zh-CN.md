---
title: "配置驱动的提供商目录"
---

Issue：https://github.com/huggingface/tau/issues/238

## 变更内容

Tau 的内置提供商目录现在由数据驱动。提供商和模型元数据位于：

```text
src/tau_coding/data/catalog.toml
```

运行时，Tau 还可以叠加用户目录：

```text
~/.tau/catalog.toml
```

该叠加层可选。它可以添加新提供商，也可以部分覆盖内置提供商。

提供商运行时偏好设置仍单独保存在：

```text
~/.tau/providers.json
```

该文件现在保存引用和偏好设置，例如默认提供商、每个提供商的默认模型、请求头、超时/重试设置以及作用域模型。它不需要重复提供商定义。

## 存在原因

在此变更之前，添加提供商或更新模型列表都需要编辑 Python 源码。即使变更仅涉及元数据，也必须通过 PR 完成。将目录迁移到 TOML 后，源码可以专注于验证和运行时行为，同时让提供商新增变得小巧且易于审查。

## 架构边界

实现保留在 `tau_coding` 中，因为目录加载会使用 Tau 主目录路径和应用专用的提供商偏好设置。可复用的 `tau_agent` 框架仍然只接收已准备好的模型提供商和模型名称。`tau_ai` 层继续专注于与提供商无关的运行时适配器。

公共兼容接口仍是 `tau_coding.provider_catalog` 中的 `ProviderCatalogEntry`。TOML 解析、验证和叠加行为位于 `tau_coding.catalog_loader`。

## 目录结构

```toml
schema_version = 1

[[providers]]
name = "local-gateway"
display_name = "Local Gateway"
kind = "openai-compatible"
base_url = "http://localhost:11434/v1"
api_key_env = "LOCAL_GATEWAY_API_KEY"
credential_name = "local-gateway"
models = ["qwen-coder"]
default_model = "qwen-coder"
docs_url = "https://example.test/local-gateway"

[providers.context_windows]
qwen-coder = 64000
```

支持的 `kind` 值为 `openai-compatible`、`anthropic` 和 `openai-codex`。对于用户定义的提供商，`openai-compatible` 是首选路径。

## 叠加行为

当 `~/.tau/catalog.toml` 定义了一个与内置提供商具有相同 `name` 的提供商时：

- 标量字段替换内置值
- `models` 会合并，用户模型排在前面
- `context_windows` 会合并
- 当存在 `thinking_levels` 时，思考相关字段作为一组整体替换

此阶段有意只支持用户级目录叠加。Tau 不读取项目本地目录文件，因此克隆仓库无法静默重定向内置提供商的 `base_url`。

## 运行时偏好设置

`~/.tau/providers.json` 有意只保存运行时偏好设置，而不是提供商元数据：

```json
{
  "default_provider": "local-gateway",
  "provider_preferences": {
    "local-gateway": {
      "default_model": "qwen-coder",
      "headers": {},
      "timeout_seconds": 60,
      "max_retries": 2,
      "max_retry_delay_seconds": 1
    }
  },
  "scoped_models": []
}
```

仍然接受包含完整 `providers` 条目的旧版 `providers.json` 文件。当 Tau 再次保存设置时，自定义提供商定义会写入 `~/.tau/catalog.toml`，而 `providers.json` 会重写为仅包含偏好设置的结构。

## 验证

目录文件发生问题时会尽早以 `CatalogError` 失败。Tau 会拒绝未知键、必填但为空的字符串、空模型名称、不支持的提供商类型、未列在 `models` 中的默认模型、指向未知模型的 `thinking_models` 或 `context_windows` 条目，以及非正数或非整数的上下文窗口值。

## 测试方法

```bash
uv run pytest tests/test_provider_catalog.py
uv run pytest tests/test_provider_config.py
uv run ruff check .
uv run mypy
```

目录测试覆盖内置加载、打包资源访问、用户定义提供商、内置叠加、无效目录，以及已存在 `providers.json` 时与 `ProviderSettings` 的集成。
