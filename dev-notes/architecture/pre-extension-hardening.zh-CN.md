---
title: "扩展前加固总结"
---

本文总结了第 21 阶段扩展之前完成的生产环境加固工作。第 21 阶段仍按计划暂缓。

## Agent 与提供商运行时

Tau 现在为重试、思考/推理增量以及排队中的提示词状态提供与提供商无关的进度事件：

- `RetryEvent` 报告提供商重试尝试，而不会向前端暴露提供商特有的 HTTP 细节。
- `ThinkingDeltaEvent` 携带可选的流式提供商推理文本，但不会将其记录为持久化的助手消息。
- `QueueUpdateEvent` 携带待处理的引导和后续提示词文本，供前端状态显示使用。

OpenAI 兼容、Anthropic 和 OpenAI Codex 订阅提供商会在发出最终提供商错误之前重试瞬时传输故障。重试策略可通过提供商设置和环境变量配置。

凭据解析规则是明确的：对于配置了 `credential_name` 的提供商，存储在 `~/.tau/credentials.json` 中的 Tau 凭据优先于环境变量回退值。

## 上下文、技能与会话

上下文统计现在返回结构化的 `ContextUsageEstimate`，其中包含总计、系统、消息和工具 token 估算值。TUI 从事件流刷新，因此侧边栏和紧凑会话中的上下文数字会在用户消息、助手响应、工具结果、压缩以及恢复/新建会话流程后更新。

已加载的技能可通过两种方式使用：

- 系统提示词列出技能名称、描述和文件位置，使模型能够通过常规 `read` 工具读取相关技能文件
- `/skill:<name> [request]` 会将完整的技能 Markdown 展开到下一条提示词中

会话可通过 `tau export` 导出。它会根据已索引的会话 ID 或 JSONL 会话路径写出自包含的 HTML 视图，并同时保留会话树结构和按存储顺序排列的对话记录。

## TUI 行为

Textual 前端仍位于适配器边界之后：

```text
CodingSession emits AgentEvent values
        ↓
TuiEventAdapter updates TuiState
        ↓
Textual widgets render transcript, status, and controls
```

最近的 TUI 加固包括：

- 响应式侧边栏行为，展示提供商/模型、思考模式、工具、技能、提示词模板和上下文文件
- 在流式用户消息、助手消息、工具、压缩和恢复事件后刷新上下文大小
- 对可见对话输出进行 Textual 文本选择
- 使用 `Alt-Up` / `Alt-Down` 选择消息
- 使用 `Ctrl-C` 复制所选消息
- 使用 `Ctrl-O` 展开内联工具结果
- Agent 运行期间显示动画活动状态
- 使用 `Shift-Tab` 循环切换思考模式
- 使用 `Ctrl-T` 可选显示思考 token，默认隐藏
- 运行期间按 `Enter` 将引导消息加入队列
- 运行期间按 `Alt-Enter` 将后续消息加入队列

排队时不会立即持久化提示词。只有当 `AgentHarness` 将其注入当前运行并发出常规用户消息事件时，它们才会成为持久化的会话消息。

## 架构边界

新增行为保留了 Tau 的包分层：

- `tau_ai` 负责提供商特有的重试、token 和流解析。
- `tau_agent` 负责可移植的消息、事件、循环协调、harness 状态、队列语义、工具和会话原语。
- `tau_coding` 负责提供商配置、凭据、资源、命令、持久化工作流、文档和 Textual UI 策略。

可复用的 Agent 包仍不会导入 Textual、Rich 渲染、Typer、本地配置路径、斜杠命令注册表或项目资源加载逻辑。

## 验证

这些加固切片由以下范围内的针对性测试覆盖：

```text
tests/test_agent_harness.py
tests/test_agent_loop.py
tests/test_coding_session.py
tests/test_context_window.py
tests/test_provider_config.py
tests/test_rendering.py
tests/test_session_export.py
tests/test_skills.py
tests/test_tau_ai.py
tests/test_tui_adapter.py
tests/test_tui_app.py
tests/test_tui_config.py
```

合并最后一批扩展前变更之前，完整门禁已通过：

```bash
uv run pytest
uv run ruff check .
uv run mypy
```
