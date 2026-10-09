---
title: "阶段 19：项目上下文发现与重新加载"
---

此阶段为 Tau 增加了项目指令发现功能和重新加载命令。相关实现位于
`tau_coding` 中，与资源、命令和会话启动逻辑放在一起。

实现位于：

```text
src/tau_coding/context.py
src/tau_coding/session.py
src/tau_coding/cli.py
src/tau_coding/commands.py
```

## 新增内容

Tau 现在会自动发现 Markdown 指令文件，并将它们插入现有系统提示词的
`ProjectContextFile` 部分。

当前发现顺序为：

```text
~/.tau/AGENTS.md
~/.agents/AGENTS.md
<project root>/AGENTS.md
<project root>/.../<cwd>/AGENTS.md
<cwd>/.tau/AGENTS.md
<cwd>/.agents/AGENTS.md
```

项目根目录是包含常见项目标记的最近一级祖先目录，例如 `.git`、
`pyproject.toml`、`uv.lock`、`setup.py` 或 `package.json`。如果不存在标记，
Tau 会将会话当前工作目录视为项目根目录。

## 系统提示词集成

常规 `CodingSession` 启动和非交互式打印模式都会将发现的上下文文件传给：

```python
BuildSystemPromptOptions(context_files=...)
```

这保留了阶段 10 的提示词边界：

```text
tau_coding discovers local files
tau_coding.system_prompt formats them
tau_agent receives only a ready system string
```

`tau_agent` 仍不依赖本地资源发现、Tau 主目录、项目路径、斜杠命令、Rich 或
Textual。

## 斜杠命令

Tau 现在提供：

```text
/context
/reload
```

`/context` 列出运行中会话当前生效的项目上下文文件。

`/reload` 刷新供后续轮次使用的 Tau 自有资源：

- 技能
- 提示词模板
- 项目上下文文件
- 资源诊断信息

当会话使用 Tau 生成的系统提示词时，只有在构成提示词的资源发生变化后，
重新加载才会重建 harness 系统字符串。对话记录和会话树保持不变。

`/reload` 不刷新提供商配置。提供商和模型设置由使用它们的提供商专属流程刷新，
例如保存凭据后的 `/login`，以及验证选项或打开模型选择器前的 `/model`。

`/status` 和 `/resources` 也会显示当前上下文文件数量。

## 边界

重新加载属于 `tau_coding` 操作。它会更新 harness 周围的编码会话环境，并在需要时
向 harness 提供为后续轮次重建的系统字符串。`tau_agent` 不知道技能、提示词或上下文
文件来自哪里。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_context.py
tests/test_coding_session.py
tests/test_cli.py
tests/test_commands.py
```

测试验证：

- 用户、项目、嵌套目录、`.tau` 和 `.agents` 上下文发现
- 会话系统提示词包含发现的上下文
- 打印模式系统提示词包含发现的上下文
- `/context`、`/status` 和 `/resources` 命令输出
- `/reload` 命令输出
- 仅当提示词输入变化时，重新加载才会更新资源和下一轮系统提示词
