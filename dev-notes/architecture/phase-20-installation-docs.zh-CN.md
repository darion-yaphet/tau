---
title: "阶段 20：安装与配置文档"
---

此阶段开始完善 Tau 的打包和安装体验。

项目已经通过以下配置公开面向用户的控制台命令：

```toml
[project.scripts]
tau = "tau_coding.cli:app"
```

此切片说明如何把该命令作为普通工具安装和使用。

## 新增内容

新增面向用户的文档：

```text
docs/installation.md
docs/configuration.md
```

更新的文档：

```text
README.md
docs/getting-started.md
docs/index.md
docs/providers.md
mkdocs.yml
```

## 覆盖的工作流

文档现在涵盖：

- 使用 `uv tool install` 安装 Tau
- 使用 `pipx` 安装 Tau
- 验证已安装的 `tau` 命令
- 首次运行时配置提供商
- 使用 `tau` 打开 TUI
- 在打印模式下运行单个提示词
- `~/.tau/providers.json` 下的提供商配置
- `~/.tau/sessions` 下的会话存储
- 技能和提示词模板位置
- 项目上下文文件发现
- 当前禁用的 shell 补全

## 边界

此阶段不改变 `tau_agent` 或提供商/运行时行为，只明确当前面向用户的应用安装和配置路径。

## 测试

该文档切片通过以下命令验证：

```text
uv run tau --version
uv build
uv run --group docs mkdocs build --strict
```
