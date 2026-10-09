---
title: "Phase 15：斜杠命令注册表"
---

Phase 15 使用 `tau_coding` 中的小型注册表替换 Tau 硬编码的斜杠命令处理。

实现位于：

```text
src/tau_coding/commands.py
```

## 新增内容

Tau 现在具有一个 `CommandRegistry`，可以：

- 使用名称、别名、描述和用法文本注册斜杠命令
- 列出已注册命令，以生成帮助输出
- 解析斜杠命令输入
- 将命令分发到处理器
- 向 UI 层返回结构化命令结果

核心命令类型为：

```python
SlashCommand
CommandContext
CommandResult
CommandRegistry
```

`CodingSession.handle_command()` 现在委托给注册表，而不再硬编码命令行为。

## 内置命令

默认注册表包括：

- `/quit` — 退出当前会话
- `/new` — 开始新会话
- `/compact` — 使用手动摘要替换活动上下文
- `/export` — 导出当前会话
- `/session` — 显示会话信息和统计数据
- `/hotkeys` — 显示常用 TUI 键盘快捷键
- `/resume` — 打开先前会话选择或恢复指定会话 ID
- `/model` — 选择或切换当前模型
- `/login` — 添加或刷新内置提供商登录
- `/reload` — 重新加载本地资源和项目上下文
- `/name` — 重命名当前会话
- `/theme` — 显示或设置 TUI 主题

自动补全还使用不可执行的搜索词。例如，输入 `/clear` 会建议 `/new`，但搜索词不是已注册命令。

## 为何属于 `tau_coding`

斜杠命令是编码代理应用行为。它们依赖资源、会话、技能、提供商/模型 UX 和 UI 预期。

可复用的 `tau_agent` 包继续独立于斜杠命令、Textual、Typer、本地配置目录和 Tau 专用产品行为。

## 与 Pi 命令对齐

Pi 的内置命令列表包括：

```text
settings, model, scoped-models, export, import, share, copy, name, session,
changelog, hotkeys, fork, clone, tree, login, logout, new, compact, resume,
reload, quit
```

Tau 对齐可直接映射到现有能力的命令：

- `/session` 映射到 Tau 现有的会话状态/详细信息输出。
- `/hotkeys` 报告 Tau 当前常用 TUI 快捷键。
- `/quit` 是唯一注册的退出命令；旧的 Tau 专用 `/exit` 和 `/q` 别名有意不保留。
- Tau 命令注册表中存在 `/model`、`/login`、`/new`、`/compact`、`/resume`、`/reload`、`/name`、`/theme` 和 `/export`。

Tau 有意从注册表移除旧的 Tau 专用诊断命令，包括 `/help`、`/status`、`/skills`、`/resources`、`/context` 和 `/thinking`。等价信息仍可通过与 Pi 对齐的命令、持久 UI 界面、快捷键或模型系统提示查看。

其余 Pi 命令被推迟，因为它们需要超出此次注册表清理范围的更大工作流：

- `/settings`
- `/scoped-models`
- `/import`
- `/share`
- `/copy`
- `/changelog`
- `/fork`
- `/clone`
- `/tree`
- `/logout`

## TUI 集成

TUI 仍调用：

```python
session.handle_command(text)
```

但返回的 `CommandResult` 现在可以请求退出之外的行为。

例如，`/new` 返回 `new_session_requested=True`。TUI 会创建并加载新的已索引会话，而不会删除较旧的持久 JSONL 会话历史。

## 提示扩展命令行为

`/skill:<name> [request]` 仍然是提示扩展路径，而不是普通斜杠命令。

提示模板也表现为动态提示扩展命令。例如，`.agents/prompts/example.md` 以 `/example [arguments]` 调用，并由 `CodingSession.prompt()` 在模型看到提示前完成扩展。

注册表有意为技能和提示模板扩展指令返回 `handled=False`，使 `CodingSession.prompt()` 可以在向模型发送提示前替换它们。

与 Pi 一样，Tau 也为每个未注册且以斜杠开头的输入返回 `handled=False`。只有已识别命令会在本地消费；未知的类命令文本和 `/tmp`、`/Users/me/file.png` 等绝对路径仍会作为普通提示继续处理。

与 Pi 对齐的注册表中没有单独的 `/skill` 斜杠命令。

## 未来用途

该注册表是后续阶段的基础：

- TUI 斜杠命令自动补全可从注册表读取命令元数据。
- 扩展最终可以贡献命令。
- 未来的命令面板可以显示相同的命令元数据。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_commands.py
tests/test_coding_session.py
tests/test_tui_app.py
```

测试验证：

- 命令解析和分发
- 生成的帮助输出
- 退出和新建会话控制标志
- 状态和技能输出
- `/skill:<name>` 透传行为
- 已索引会话列表
- 结构化 `/resume <session-id>` 请求
- TUI 对 `/new` 的处理
