# 提供商目录实时验证运行手册

本运行手册说明我们如何验证源自 Pi 的 API 提供商目录，以及在添加或更改提供商/模型时如何重复该过程。

目标不是对回答质量进行基准测试，而是证明 Tau 展示的每个模型和每个思考/推理级别，都能被提供商 API 以 Tau 所发送的负载接受。

## 何时使用本运行手册

在以下情况下运行实时验证：

- 导入或重新生成提供商目录元数据
- 添加提供商运行时适配器
- 更改推理/思考负载
- 更改模型列表或默认模型
- 遇到模型 ID 已弃用或推理级别不受支持等提供商错误

不要随意对大型目录运行验证。即使是很小的提示词，在数百个模型和推理级别上重复后也可能消耗额度。

## 验证通过的标准

本轮使用了一个很小的提示词：

```text
Reply with exactly: OK
```

一旦 Tau 收到提供商的 `response_start` 事件，请求就计为有效。这意味着：

1. Tau 成功构建了运行时提供商。
2. 凭据被接受到足以发出请求的程度。
3. 提供商接受了模型 ID。
4. 提供商接受了该级别的推理/思考负载。
5. 端点开始了流式响应。

这里有意提前停止。等待完整文本输出可以验证更多流行为，但会消耗更多 token。仅对小型样本或测试流解析行为时使用完整响应验证。

## 验证矩阵

矩阵为：

```text
credentialed providers
  × provider.models
  × provider_thinking_levels(provider, model)
```

对于非推理模型，使用无思考级别的一次请求进行验证。

Tau 有两个实用的目录范围：

- **有效目录**：内置目录加 `~/.tau/catalog.toml` 用户覆盖层。这与特定机器上 UI 显示的内容一致。
- **打包目录**：仅内置目录。PR 验证应使用此范围，防止本地用户覆盖层掩盖或重新引入陈旧模型。

本轮先验证有效目录，以捕获本地 UI 展示的内容；随后使用 `--builtins-only` 重新运行摘要和重试，以验证 PR 目录本身。

## 本地验证辅助脚本

我们使用了位于以下位置的临时辅助脚本：

```text
~/.tau/provider-validation/validate_provider_catalog.py
```

该脚本有意设计为可恢复执行。每次尝试都会向以下文件追加一个 JSON 对象：

```text
~/.tau/provider-validation/pi-api-provider-catalog/results.jsonl
```

关键字段为：

| 字段 | 含义 |
|---|---|
| `provider` | Tau 提供商名称 |
| `model` | 发送给提供商的模型 ID |
| `thinking_level` | Tau 思考级别；非推理验证时为 `null` |
| `status` | `ok` 或 `error` |
| `message` | 失败尝试的提供商/运行时错误消息 |
| `data.status_code` | 可用时的 HTTP 状态 |
| `data.body` | 可用时截断后的提供商错误正文 |

脚本支持三种操作：

```bash
# Show credentialed providers, missing credentials, and total attempt counts.
uv run python ~/.tau/provider-validation/validate_provider_catalog.py plan

# Run live validation.
uv run python ~/.tau/provider-validation/validate_provider_catalog.py run \
  --builtins-only \
  --concurrency 4 \
  --provider-concurrency 2 \
  --timeout-seconds 30 \
  --max-retries 0

# Summarize recorded results.
uv run python ~/.tau/provider-validation/validate_provider_catalog.py summarize \
  --builtins-only \
  --top-errors 12
```

重要参数：

| 参数 | 用途 |
|---|---|
| `--builtins-only` | 忽略 `~/.tau/catalog.toml`；只验证 PR 中打包的目录。 |
| `--provider NAME` | 将运行限制为一个提供商；可重复指定。 |
| `--limit N` | 只运行接下来的 N 个待处理尝试。适合抽样。 |
| `--retry-errors` | 重试已有错误行的尝试。修复后使用。 |
| `--concurrency N` | 全局最大并发调用数。 |
| `--provider-concurrency N` | 每个提供商的最大并发调用数。 |
| `--wait-for response_start` | 成本最低的成功条件；验证请求是否被接受。 |
| `--wait-for text` | 成本更高；验证文本是否实际流出。 |

除非设置 `--retry-errors`，否则脚本会跳过已有结果的尝试。

## 安全并发默认值

从保守配置开始：

```bash
--concurrency 4 --provider-concurrency 2 --max-retries 0
```

对于昂贵的提供商或限额较低的账户，使用：

```bash
--concurrency 1 --provider-concurrency 1
```

对于 OpenRouter，我们使用较低的单提供商并发，因为目录很大，且许多路由是免费/共享的上游：

```bash
uv run python ~/.tau/provider-validation/validate_provider_catalog.py run \
  --builtins-only \
  --provider openrouter \
  --concurrency 2 \
  --provider-concurrency 1 \
  --timeout-seconds 35 \
  --max-retries 0
```

首次运行应避免自动重试。重试可能掩盖确定性的负载/模型故障，并消耗额外额度。只有在错误分类后才重试。

## 故障分类

修复任何内容之前，先对错误进行分类。

### 提供商范围内反复出现时立即修复

如果某个提供商的每个模型都因同一种负载/schema 错误失败，应暂停验证、修复适配器，然后使用 `--retry-errors` 重新运行该提供商。

本轮示例：

- Google 拒绝了每个模型，因为 `systemInstruction` 被嵌套在 `generationConfig` 下。
- 修复：将 `systemInstruction` 移到 Google 请求负载的顶层。
- 重跑：此后 Google 模型除真实的模型/级别问题外均通过。

### 在目录/运行时中修复

以下属于真实的 Tau 目录/运行时问题：

| 错误模式 | 典型修复 |
|---|---|
| `model ... does not exist`、`No endpoints found`、`deprecated` | 从打包目录移除该模型。 |
| `not a chat model`，要求音频/工具/搜索/文件/MCP | 暂时从编码聊天 API 目录移除。 |
| `Unsupported value: 'minimal'`，预期 `low/medium/high` | 添加按模型配置的 `unsupported_thinking_levels`。 |
| 提供商期望不同的推理值 | 添加 `thinking_level_map`。 |
| 提供商需要特殊路由/header/兼容选项 | 添加提供商/模型 `compat` 元数据和运行时支持。 |
| 提供商范围内的无效 JSON/负载 | 修复适配器并添加回归测试。 |

### 记录说明，不做全局修复

以下属于账户/运行时条件，不一定反映目录事实：

| 错误模式 | 处理方式 |
|---|---|
| `401`、`403`、缺少凭据 | 报告为未验证或账户特有。 |
| `402 Insufficient Balance` | 报告为已有凭据，但因余额不足未验证。 |
| 账户授权错误 | 单独报告；除非提供商文档确认，否则不要全局移除。 |
| `429` 速率限制 | 稍后重试或报告为受速率限制。默认不移除。 |
| `500`、`502`、`503` 瞬时故障/高负载 | 重试一次；若持续发生，报告为瞬时/上游问题。 |

本轮示例：

- DeepSeek 返回 `402 Insufficient Balance`；我们没有移除 DeepSeek 模型。
- OpenAI Codex 针对已登录 ChatGPT 账户拒绝了一些模型；我们没有全局移除它们，因为授权资格可能不同。
- OpenRouter 免费/共享路由返回 `429`；我们将其保留在目录中，并报告为上游速率限制。

## 修复/重跑循环

使用以下循环，直到所有可修复错误都消失：

1. 运行 plan 或 dry run。
2. 对新提供商运行一小批请求。
3. 如果故障覆盖整个提供商，先修复适配器。
4. 运行完整提供商/目录验证。
5. 汇总错误。
6. 应用目录/运行时修复。
7. 只重跑失败项：

   ```bash
   uv run python ~/.tau/provider-validation/validate_provider_catalog.py run \
     --builtins-only \
     --provider PROVIDER_NAME \
     --retry-errors \
     --concurrency 2 \
     --provider-concurrency 1
   ```

8. 再次汇总。
9. 重复上述过程，直到剩余错误仅为已记录的账户/余额/速率限制情况。

## 验证后更新 Tau

常见代码/目录变更：

- 从 `src/tau_coding/data/catalog.toml` 移除陈旧模型 ID。
- 为此前默认模型已移除的提供商设置经过验证的默认值。
- 在 `providers.model_metadata.<model>` 下添加 `unsupported_thinking_levels`。
- 添加 `thinking_level_map`，用于提供商特有的值映射。
- 为适配器特有行为添加模型 `compat` 字段。
- 为任何提供商范围内的负载修复添加适配器回归测试。
- 更新硬编码旧默认值或提供商列表的陈旧测试。

本轮验证的主要修复包括：

- Google Generative AI：`systemInstruction` 现在位于请求顶层。
- OpenRouter：Tau 将 `compat.openrouterProvider` 作为 OpenRouter 的 `provider` 路由选项传递。
- 目录：剪除陈旧/已弃用/无法路由的模型 ID。
- 目录：按模型隐藏不受支持的推理级别。
- Anthropic：针对当前自适应模型收紧自适应思考元数据。

## 最终报告格式

每轮验证结束时，都应提供一份简明报告，包含：

1. 目录范围：有效目录或打包/内置目录。
2. 提示词和成功条件。
3. 预期尝试总数、已记录尝试数、待处理尝试数。
4. 各提供商的 OK/错误计数。
5. 按原因分组的剩余失败。
6. 因缺少凭据而未验证的提供商。
7. 已完成的代码/目录修复。
8. 最终项目验证命令和结果。

将详细本地产物存储在仓库外：

```text
~/.tau/provider-validation/<validation-name>/results.jsonl
~/.tau/provider-validation/<validation-name>/validation-summary.md
```

不要提交原始 JSONL 日志。它们不应包含机密信息，但可能包含账户 ID、提供商特有元数据、速率限制详情和付费账户可用性线索。

## Pi API 提供商验证结果

修复后，对于已有凭据的提供商，打包目录共有 1,193 个预期验证尝试。所有可修复的模型/推理不匹配都已清除。

剩余失败不属于全局目录/运行时错误：

- 对可用 API 密钥，DeepSeek 返回 `402 Insufficient Balance`。
- 对已登录的 ChatGPT 账户，OpenAI Codex 针对 `gpt-5.3-codex` 和 `gpt-5.2` 返回账户授权错误，而其他 Codex 模型可正常工作。
- 若干 OpenRouter 免费/共享上游路由返回 `429` 速率限制。

本轮因缺少凭据而未进行实时验证的提供商包括：Cerebras、Fireworks、Mistral、Moonshot、Together、Vercel AI Gateway、xAI、Xiaomi、Z.ai 及区域变体。

最终本地项目验证：

```bash
uv run pytest -q
uv run ruff check .
uv run mypy
```

```text
677 passed
All checks passed!
Success: no issues found in 64 source files
```
