---
title: "Phase 12：Textual TUI"
---

Phase 12 使用 Textual 添加 Tau 的首个交互式终端 UI。

实现位于：

```text
src/tau_coding/tui/
```

## 新增内容

Tau 现在具有一个最小交互式 TUI，它：

- 使用 `CodingSession`，而不是原始 `AgentHarness`
- 通过 Textual 输入部件接收用户提示
- 将 `AgentEvent` 值流式传入 TUI 显示状态
- 显示助手消息、工具事件、状态消息和错误
- 显示深色模式会话侧边栏，其中包含模型、cwd、工具、技能和提示模板
- 将之前的会话消息恢复到可见对话记录
- 支持现有的 `/help` 和 `/exit` 命令处理
- 支持按 Escape 请求取消
- 将早期默认 TUI 会话存储在 `.tau/sessions/default.jsonl`

CLI 默认打开 TUI：

```bash
tau
```

启动 TUI 前可以传入全局选项：

```bash
tau --model gpt-4.1-mini --cwd /path/to/project
```

## 存在原因

Pi 将可复用代理层与终端 UI 关注点分离：

```text
agent/session emits events
terminal frontend consumes events
TUI components render display state
```

Tau 现在使用 Python 原生 Textual 遵循相同边界：

```text
CodingSession.prompt()
  emits AgentEvent
      ↓
TuiEventAdapter
  updates TuiState
      ↓
TauTuiApp
  renders Textual widgets
```

`tau_agent` 仍然不依赖 Textual、Rich、Typer 或终端 UI 行为。

## TUI 状态适配器

适配器层有意与 Textual 分离：

```text
src/tau_coding/tui/state.py
src/tau_coding/tui/adapter.py
```

`TuiState` 存储仅用于显示的状态：

- 对话记录条目
- 当前助手流式缓冲区
- 运行标志
- 最新错误

`TuiEventAdapter` 将可移植的 `AgentEvent` 值应用到该状态。

这样无需启动终端应用即可测试事件到显示的行为。

## Textual 应用

Textual 应用有意保持最小化：

```text
src/tau_coding/tui/app.py
src/tau_coding/tui/widgets.py
```

它使用：

- `Header`
- `Footer`
- 用于状态和侧边栏内容的 `Static`
- 用于侧边栏/主区域布局的 `Horizontal` 和 `Vertical` 容器
- 由可选消息部件组成的可滚动对话记录
- 用于提交提示的 `Input`

提示执行在 Textual worker 中运行，因此事件流入时 UI 可以继续更新。

## 会话存储

Phase 12 添加一个临时的默认本地会话路径辅助函数：

```python
def default_session_path(cwd: Path) -> Path:
    return cwd / ".tau" / "sessions" / "default.jsonl"
```

这有意保持简单。后续阶段可以用更丰富的 Pi 风格会话管理器和选择器替换它。

## 当前限制

TUI 是基础，尚不是完整的 Pi 级界面。它不包括：

- 会话选择器
- 模型选择器
- 命令面板
- 树导航
- diff 查看器
- Markdown 渲染
- 主题自定义
- 快捷键配置
- 扩展 UI 钩子
- 压缩 UI

## 测试

本阶段由以下测试覆盖：

```text
tests/test_tui_adapter.py
tests/test_cli.py
```

测试聚焦于纯适配器和 CLI 连接。完整终端交互测试会推迟到 TUI 界面稳定后。

## 下一阶段

下一阶段可以从以下方向之一逐步扩展 TUI：

- 会话选择和恢复支持
- 更丰富的对话记录渲染
- 命令面板和斜杠命令注册表
- 扩展钩子
- 压缩/上下文管理 UI
