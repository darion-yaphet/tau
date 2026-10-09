# Codex 运行时模型限制

Issue：[#389](https://github.com/huggingface/tau/issues/389)

## 生产事故

一个长期运行的 Tau 会话通过 `openai-codex` OAuth 提供商使用 `gpt-5.6-sol`。它最后一次成功请求报告了大约 371,440 个缓存和未缓存输入 token。下一次请求因 `context_length_exceeded` 失败。

Tau 曾将 OpenAI 直接 API 的 1.05M 上下文元数据复制到 Codex 订阅目录中，因此默认压缩阈值为 1,033,616 个 token。请求实际发送到了 `https://chatgpt.com/backend-api/codex/responses`，该处的 Codex 订阅限制独立配置。服务器下发的 Codex 目录报告的配置约为 372K，后续发布中则为 272K。因此 Tau 在达到真正的后端上限前从未进行压缩。

这是服务入口层面的问题，与公开 API 模型页面并不矛盾：同一个模型 ID 的直接 API 访问和 ChatGPT/Codex OAuth 可以使用不同限制。

## 考虑过的方案

### 将 Codex 窗口硬编码为 500K

这种方式确定性强，也可避免额外的启动请求，但 500K 通常是指 372K 输入加 128K 输出的总预算。Tau 旧的压缩公式把 `context_window` 当作可用输入预算，并且只减去 16,384 个 token。因此 500K 条目会在约 483K 时压缩，晚于报告的 372K 输入上限，无法修复此次事故。

较低的硬编码值可以作为安全回退。本次变更对 GPT-5.6 Codex 变体使用 272K，与近期观察到的最保守 Codex 目录配置一致。OpenAI 直接 API 条目仍保持 1.05M。仅靠静态值并非长期方案，因为 Codex 会在发布期间改变它们，也可能按账户提供不同值。

### 发现已认证的 Codex 目录

官方 Codex 客户端请求：

```text
GET <codex-base>/models?client_version=<version>
```

对于 Tau 的默认基础 URL，它会解析为：

```text
https://chatgpt.com/backend-api/codex/models
```

该请求使用与响应请求相同的已刷新 OAuth token、ChatGPT 账户 ID 和来源请求头。模型条目可以包含：

```text
slug
context_window
max_context_window
effective_context_window_percent
auto_compact_token_limit
```

动态发现可以反映实际服务入口和账户。缺点在于，这是 Codex 产品端点，而不是公开 OpenAI API 模型契约，因此其结构或可用性可能发生变化。

## 决策

采用**动态发现，并配合保守的静态回退**。

`tau_ai` 负责已认证请求和防御性解析。它实现可选且与提供商无关的 `ModelLimitsProvider` 能力，并返回类型化的 `RuntimeModelLimits` 值。它会忽略格式错误的条目，并为每个提供商实例缓存一份目录。

`tau_coding` 会在会话加载时请求限制，并在切换提供商或模型后、发送下一条提示前再次请求。它应用实时的原始上下文窗口，以及提供商明确给出的压缩限制；若未给出，则采用 Codex 默认的原始窗口 90%。发现失败不会导致致命错误：配置中的提供商目录仍然生效，`/session` 会报告来源和错误。

`tau_agent` 不做改动。它接收已准备好的提供商，并继续独立于 OAuth、模型目录、Tau 主目录路径和压缩策略。

首次实现有意不提供持久化的实时目录缓存。内存缓存可避免同一运行时提供商中的重复请求；每个新会话都会获取新的账户专用值。磁盘缓存需要过期策略、ETag 处理、凭据/账户分区、原子写入，以及明确判断过期值是否比保守内置回退更安全。这些问题以后可以添加，且无需改变发现协议。

## 限制语义

`RuntimeModelLimits` 将四个概念分别保存：

- 原始上下文窗口
- 最大输出 token 数（如果提供）
- 有效上下文百分比
- 明确的自动压缩阈值（如果提供）

如果未返回阈值，Tau 会使用原始上下文窗口的 90%，并将其限制在有效窗口内。这与官方 Codex 客户端的默认值一致，也为提供商封装、工具、指令和输出保留余量。

Tau 基于字符的用量估算仍然只是近似值。在接近远端硬限制时提前压缩是有意行为。

## 失败行为

目录发现无法防止所有溢出：后端可能在发现和响应之间发生变化，token 估算也可能漏算提供商封装。Tau 现有的溢出路径仍是第二道防线：识别上下文溢出、压缩旧消息，然后重试一次。动态发现能使主动路径足够准确，因此紧急恢复应很少发生。

## 测试

确定性测试使用 `httpx.MockTransport` 和虚假模型限制提供商。覆盖内容包括：

- 已认证的 Codex 模型目录 URL 和请求头
- 防御性解析和内存缓存
- 编码会话中的实时上下文/压缩值
- 发现失败时的非致命回退
- 直接 API 与 Codex 内置元数据之间的隔离

运行：

```bash
uv run pytest tests/test_tau_ai.py tests/test_coding_session.py \
  tests/test_provider_catalog.py tests/test_provider_runtime.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
cd website && hugo --minify && npx --yes pagefind@latest --site public
```

## 手动验证

1. 使用 `/login openai-codex` 登录。
2. 选择一个 GPT-5.6 模型。
3. 启动或恢复会话。
4. 运行 `/session`。
5. 确认 `Context window source` 为 `provider live catalog`，并且显示的限制/压缩阈值与同一账户的 `codex debug models` 相匹配。
6. 阻止 models 请求或使用无效的发现数据，确认 Tau 以 `configured catalog` 启动、报告非致命的发现失败，并且仍能发送正常模型请求。
