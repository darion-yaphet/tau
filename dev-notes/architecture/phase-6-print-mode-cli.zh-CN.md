---
title: "阶段 6：非交互式打印模式 CLI"
---

阶段 6 将 Tau 的提供商层、代理 harness 和内置编码工具连接为可用的非交互式 CLI。后续阶段将
打印模式迁移到 `CodingSession` 包装器，使其与 TUI 共享相同的编码代理环境。

CLI 入口位于：

```text
src/tau_coding/cli.py
```

## 新增内容

`tau` 命令可以通过 `-p`/`--prompt` 在打印模式中运行单个提示词：

```bash
tau -p "write tests for main.py"
tau --prompt "explain this repo"
tau --model gpt-5.5 -p "summarize README.md"
```

不带 `-p`/`--prompt` 时，Tau 会启动交互式 TUI。位置参数形式的提示词会被视为 TUI 初始提示词：

```bash
tau
tau "explain this repo"
```

最初的阶段 6 命令会：

1. 从环境加载 OpenAI 兼容的提供商设置
2. 创建 Tau 内置编码工具
3. 构建最小默认系统提示词
4. 创建 `AgentHarness`
5. 将助手文本流式输出到 stdout
6. 将工具执行摘要输出到 stderr

## 提供商配置

打印模式使用 Tau 已配置的提供商设置。

如果尚未保存提供商登录信息，默认 OpenAI 兼容提供商需要：

```bash
export OPENAI_API_KEY="..."
```

可选环境变量：

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
```

当前默认模型为：

```text
gpt-5.5
```

使用 `--model` 可选择配置端点支持的其他模型。

## 工作目录

工具默认以当前工作目录为根目录。

使用 `--cwd` 可让工具在其他位置运行：

```bash
tau --cwd /path/to/project -p "inspect the tests"
```

## 当前行为

打印模式现在组合了更高层的 `CodingSession` 环境。单次运行仍保持非交互式并适合脚本使用，
但现在会共享：

- 完整的 Tau 系统提示词组装
- 已发现的项目指令
- 已加载的技能和提示词模板
- 内置编码工具
- `~/.tau/sessions/` 下的只追加会话持久化
- 与 TUI 相同的提供商/模型解析方式
- 已配置的提供商超时和重试设置
- 基于 Rich 的工具活动对话记录渲染

这在保持面向用户命令简单的同时，减少了打印模式与交互模式之间的偏差。

## 历史上的最小系统提示词

阶段 6 最初构建了一个小型提示词，其中包含：

- Tau 的身份
- 可用工具列表
- 每个工具的 `prompt_snippet`
- 每个工具的 `prompt_guidelines`

这足以支持第一个 CLI 切片。当前实现改用共享系统提示词构建器。

## 边界

CLI 位于 `tau_coding`。它依赖提供商设置、`CodingSession` 和渲染器，但可复用的
`tau_agent` 包仍不依赖 CLI、Rich、Textual、配置文件或会话存储。

## 测试

此阶段由 `tests/test_cli.py` 覆盖，包括：

- `tau --version`
- 未提供打印模式提示词时默认启动 TUI
- 位置参数提示词作为初始提示词启动 TUI
- 共享系统提示词内容
- 使用虚假提供商进行打印模式流式输出
- 打印模式会话持久化
- 打印模式技能展开

## 下一阶段

路线图的下一阶段是只追加会话树持久化。
