# notes/

「从终端到智能体：用 Tau 理解 Agent 架构」系列文章与配套素材。面向想把 Agent 从"会说话的黑盒"拆开的开发者，顺着一次用户请求从进到出的路线，解释一个能干活的 Agent 为什么需要边界、状态和反馈回路。

## 版本快照

本系列基于 **Tau 0.4.1（2026 年 9 月）** 的源码写成。Tau 演进很快，文中的函数名与行级细节以仓库当前源码为准；设计原则部分变化慢得多。每篇末尾的「三分钟核对」都可离线完成，不需要配置真实 Provider。

## 阅读顺序

| 篇 | 文件 | 主题 |
|---|---|---|
| 目录 | [tau-series-outline.md](tau-series-outline.md) | 系列大纲、前置知识、推荐阅读节奏 |
| 0 | [tau-series-00-what-is-tau.md](tau-series-00-what-is-tau.md) | Tau 全景图：四个角色与三层分工 |
| 1 | [tau-series-01-agent-mental-model.md](tau-series-01-agent-mental-model.md) | Agent 心智模型：事件流而非最终字符串（附真实运行切片） |
| 2 | [tau-series-02-provider-streaming.md](tau-series-02-provider-streaming.md) | tau_ai：流式归一化与 Provider 边界 |
| 3 | [tau-series-03-harness-messages-events.md](tau-series-03-harness-messages-events.md) | AgentHarness、消息结构与事件契约 |
| 4 | [tau-series-04-tools-feedback-safety.md](tau-series-04-tools-feedback-safety.md) | 工具循环、失败反馈与安全边界 |
| 5 | [tau-series-05-context-sessions.md](tau-series-05-context-sessions.md) | 上下文估算、压缩与 JSONL 会话树 |
| 6 | [tau-series-06-coding-session-interfaces.md](tau-series-06-coding-session-interfaces.md) | CodingSession 装配、CLI 与可替换 TUI |
| 7 | [tau-series-07-extensions-observability-testing.md](tau-series-07-extensions-observability-testing.md) | 扩展、可观测性、测试方法（附失败复盘） |
| 8 | [tau-series-08-trust-local-cost-rpc.md](tau-series-08-trust-local-cost-rpc.md) | trust、本地推理、成本与 RPC：0.4.x 的长期可信 |

配套素材：[assets/run-slice/](assets/run-slice/) 是用 FakeProvider 实跑出的 22 条 Pi 兼容 JSON 事件（`uv run python assets/run-slice/generate_slice.py` 可复跑），第 1、3 篇引用了它。

工程重构笔记不在此目录，见 [../dev-notes/](../dev-notes/)（如 hotspot-first-split.md）。

## 术语表

系列中反复出现、未逐篇展开的词：

- **Harness / AgentHarness**：`tau_agent` 里的 Agent 运行时，保存消息历史与运行状态，对外只发事件。文中单写 Harness 指这层概念，`AgentHarness` 指具体类。
- **CodingSession**：`tau_coding` 里的产品层会话，负责装配资源、持久化与策略，把 Harness 放进真实编码环境。
- **Provider**：模型服务适配器（Anthropic、OpenAI 兼容、llama.cpp 等）。协议 `ModelProvider` 属于 `tau_agent`，各家适配属于 `tau_ai`。
- **print mode**：`tau -p "..."` 一次性非交互模式，消费与 TUI 相同的事件流。
- **事件（AgentEvent / CodingSessionEvent）**：带判别字段的严格类型，是渲染、持久化、诊断、RPC 共同消费的事实序列，不是日志。
- **ToolResultMessage**：工具执行结果写回历史的消息，通过 `tool_call_id` 与调用对号；模型下一轮读到的是它，不是"执行过"这个隐式事实。
- **JSONL 会话树**：一行一条 entry 的追加式历史，靠 `id`/`parent_id`/`LeafEntry` 组成可分支的树。
- **压缩（compaction）**：追加一条摘要 entry，重放时替换旧消息的形状，不改写原始记录。
- **steer / follow-up**：Agent 运行中接收新输入的两种语义入口：插入当前任务，或排队到当前任务结束后。
- **trust**：项目输入加载守卫（哪些资源与扩展允许进入运行环境），不是运行时沙箱。
- **Pi**：Tau 对标的极简编码 Agent（TypeScript）。Tau 与其对齐消息/事件协议、CLI flags、RPC 契约与 prompt 模板；第 8 篇有对照表。
- **扩展（extension）**：在产品层接缝上注册工具、Provider、命令与 hook 的可选能力，不改动 Agent loop。
- **FakeProvider**：回放预定义事件流的测试 Provider，是整条分层边界的压力测试。
