# OAuth 提供商对齐与登录生产日志

Issue：[#370](https://github.com/huggingface/tau/issues/370)

## 本阶段新增内容

Tau 原有的 OAuth 代码与 OpenAI Codex 直接绑定。本阶段在 `tau_coding` 中增加了一个小型、与提供商无关的注册表，移植 Tau 缺失的两个通用 Pi 订阅提供商，并通过其支持的 API key 机制添加当前 OpenCode 产品：

- Anthropic Claude Pro/Max OAuth（授权码 + PKCE）
- GitHub Copilot OAuth（设备授权，包括 GitHub Enterprise 域名输入）
- 通过同一注册表提供现有的 OpenAI Codex OAuth
- OpenCode Go 和 OpenCode Zen 目录条目（`OPENCODE_API_KEY`，不是 OAuth）

应用扩展也可以扩展该注册表，而无需向 `tau_agent` 添加提供商策略。

## 已审计的上游快照

对齐审计固定在 2026-07-14 提交的 Pi commit [`dcfe36c79702ec240b146c45f167ab75ecddd205`](https://github.com/earendil-works/pi/commit/dcfe36c79702ec240b146c45f167ab75ecddd205)。相关源码位于 `packages/ai/src/utils/oauth/`，Pi 的提供商指南为 [`packages/coding-agent/docs/providers.md`](https://github.com/earendil-works/pi/blob/dcfe36c79702ec240b146c45f167ab75ecddd205/packages/coding-agent/docs/providers.md)。

| 提供商/产品 | 上游认证 | Tau 决策 | 备注 |
| --- | --- | --- | --- |
| OpenAI Codex | OAuth 浏览器回调；Pi 还支持设备码 | 通过注册表交付现有浏览器流程 | 现有 Tau 凭据保持兼容。设备码留作后续工作。 |
| Anthropic Claude Pro/Max | OAuth 授权码 + PKCE | 交付 | OAuth 请求使用 Bearer 认证、Claude Code 身份请求头/beta，以及必需的身份系统块。Anthropic 目前将第三方框架使用描述为按 token 计费的额外用量。 |
| GitHub Copilot | GitHub 设备授权，随后进行 Copilot token 交换 | 交付 | 支持 github.com 和提示输入的 GitHub Enterprise Server 域名。模型可用性取决于账户策略。 |
| Radius | 网关发现的浏览器/设备 OAuth | 从 Tau 核心中排除 | Radius 是 Pi 第一方 `pi-messages` 网关，不是通用提供商。Tau 不应将可复用框架绑定到另一个产品的网关。 |
| OpenCode Go | API key | 作为 API key 提供商交付 | 官方设置要求用户在 OpenCode 控制台订阅并复制 API key。 |
| OpenCode Zen | API key | 作为 API key 提供商交付 | 这是按用量付费的网关访问，并非 OAuth 登录。 |
| Google Gemini API | API key | 保留现有 API key 提供商 | Gemini CLI 的消费者 OAuth 属于第一方客户端登录。本阶段不会在第三方框架中复用未公开的消费者凭据。Vertex ADC 可作为单独的环境认证功能。 |

OpenCode 决策依据 OpenCode `dev` 文档检查：[`go.mdx`](https://github.com/sst/opencode/blob/dev/packages/web/src/content/docs/go.mdx) 和 [`zen.mdx`](https://github.com/sst/opencode/blob/dev/packages/web/src/content/docs/zen.mdx)。OpenCode 较新的控制台账户 OAuth 会动态返回工作区提供商配置，但 OpenCode Go/Zen 文档化的可移植提供商凭据仍是 API key。因此 Tau 不会将任一产品错误标记为 OAuth。

## 架构

```text
Textual OAuthLoginScreen
        │ OAuthLoginCallbacks (URL, device code, prompts, progress)
        ▼
tau_coding.oauth_registry
        │ OAuthProvider protocol
        ├── OpenAI Codex
        ├── Anthropic
        └── GitHub Copilot
        │
        ▼
FileCredentialStore ── OAuthRuntimeCredentialResolver ── tau_ai adapter
```

职责继续遵循 Tau 的软件包边界：

- `tau_coding` 负责登录 UX 契约、提供商注册表、token 刷新、本地凭据存储和提供商设置策略。
- `tau_ai` 只了解请求时的认证材料以及协议专用请求头或载荷行为。
- `tau_agent` 不做改动，也不了解提供商、OAuth、Textual 或本地路径。

`OAuthCredential` 现在允许可选的 `account_id` 和与 JSON 兼容的提供商元数据。现有 Codex JSON 对象可原样加载。GitHub Enterprise 只存储规范化后的域名。写入使用私有临时文件和原子替换，避免出现部分写入的 JSON。

请求时解析器会刷新过期凭据，并在请求前保存替代凭据。这也让 Copilot 能从短期 token 推导每个账户的 API 基础 URL。

## 安全选择和限制

- 浏览器回调保留 PKCE 和 state 验证。
- 设备验证 URL 仅接受 `http` 或 `https` 协议。
- 设备轮询遵循 RFC 8628 默认行为、`slow_down`、过期和取消语义。
- 提供商 HTTP 失败不会在面向用户的异常中包含 token 响应正文、token 或授权码。
- 凭据仍保存在 `~/.tau/credentials.json` 中，权限模式为 `0600`。操作系统密钥环或静态加密存储有意推迟；本地威胁模型更强的用户应优先使用环境变量或保护 Tau 主目录。
- `/logout` 会删除本地凭据，但不会远程撤销提供商授权。用户可以在提供商账户设置中撤销 Tau/CLI 访问。
- Copilot 模型支持因套餐、组织策略和模型选择加入状态而异。内置目录是一份候选列表；解释提供商错误时应考虑这些账户限制。

## 验证和发布流程

确定性测试使用 `httpx.MockTransport` 和虚假凭据，覆盖：

- Anthropic 刷新成功和错误脱敏
- Copilot 设备登录、token 交换、Enterprise 路由、不可信 URL 拒绝和刷新元数据
- RFC 8628 `slow_down` 和取消
- 注册表替换/恢复
- 旧版 Codex 凭据加载、可扩展元数据、权限和原子写入
- 登录选择器中 OAuth 与 API key 方法的分离

使用以下命令运行生产检查：

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

实时 OAuth 冒烟测试有意不自动化，因为它们需要付费个人订阅和外部浏览器。发布前，维护者应使用测试账户手动验证每个启用的流程，包括一个无头 Copilot 设备流程和一个粘贴的浏览器回调。清理后的结果应放入发布 PR；任何 token、代码、回调查询、JWT 或凭据文件内容都不应复制到 GitHub。

## 回滚

注册表和提供商目录新增内容可以在不更改会话文件的情况下回滚。现有 API key 凭据和旧 Codex OAuth 对象保持原格式。如果提供商更改端点或条款，可从 `auth_methods`/内置注册表中移除它，同时保留凭据解析，使用户能够安全更新或删除旧的本地条目。

## 后续工作

- 添加 OpenAI Codex 设备码登录和明确的浏览器/设备选择器。
- 为仅 SSH 使用场景添加与前端无关的非 TUI 登录命令。
- 如果 Tau 引入共享同一凭据文件的并发进程，考虑添加进程级刷新锁；原子替换保护文件完整性，但不会序列化跨进程竞争的读取-修改-写入操作。
- 评估操作系统密钥环集成和远程撤销链接。
- 考虑登录后实时发现/筛选 Copilot 模型。
