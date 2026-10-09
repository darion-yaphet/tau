---
title: "扩展安装器"
---

# 扩展安装器

Tau 现在提供一个小型的 `tau install <source>` 命令，用于让扩展在后续运行中可用。它有意只实现 Pi 软件包安装器中最有用的第一部分，而不引入设置/软件包管理系统。

## 新增内容

该命令接受：

```text
tau install ./extension.py
tau install ./extension-directory
tau install git:github.com/owner/repository
tau install git:github.com/owner/repository@ref
tau install https://github.com/owner/repository.git
```

本地来源会被复制，Git 来源会被克隆到 `~/.tau/extensions/`。除非调用者传入 `--force`，否则已有目标会保留。

## 采用这种形式的原因

在此变更之前，用户必须知道 Tau 的资源目录并手动复制或克隆到其中，或者每次运行时重复使用 `tau -e PATH`。Pi 使用 `pi install` 解决了这个问题。Tau 现在复用了现有的扩展发现机制，同时对齐该命令以来源为中心的用户体验和 Pi 风格的 `git:` 写法。

第一个版本有意比 Pi 的软件包管理器更窄：

- 只安装扩展，不安装技能、提示或主题；
- 支持本地路径和 Git，不支持 npm/PyPI 软件包；
- 不安装 Python 依赖；
- 没有软件包注册表、移除命令或更新命令。

这使软件包策略留在 `tau_coding` 中，并避免修改可移植的 `tau_agent` 框架。

## 安装流程

```text
source argument
      ↓
resolve local path or normalize Git source/ref
      ↓
copy or clone into a temporary root/extensions/<name> layout
      ↓
validate that normal user-directory discovery can see it
      ↓
atomically rename into ~/.tau/extensions
      ↓
load through the existing runtime on the next Tau startup
```

验证过程不会导入或执行扩展代码。单个本地文件必须以 `.py` 结尾。目录必须包含 `extension.py`，或者在 `[tool.tau].extensions` 中声明入口；松散的 Python 文件目录会被拒绝，因为 `-e` 虽然能发现它，但用户目录发现机制无法发现。

暂存操作在目标目录内部进行，因此发布操作是同一文件系统内的重命名。临时目录树精确复现 `root/extensions/<name>` 的发现布局，避免只有嵌套内容的软件包通过显式路径验证后，却在安装完成后消失。复制、克隆、检出、验证和文件系统操作失败时，会清除其暂存产物并以 `ExtensionInstallError` 呈现。强制替换会将先前目标保留为临时备份；若发布失败则恢复该备份。

## 安全边界

安装并不等于沙箱隔离。命令会打印警告，但用户需要自行审查来源。已安装模块在 Tau 进程中执行，可以访问用户的文件系统、网络、凭据和进程。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `src/tau_coding/extension_installer.py` | 来源解析、暂存、验证和发布 |
| `src/tau_coding/cli.py` | `tau install` 命令分发和面向用户的输出 |
| `src/tau_coding/paths.py` | 规范的用户扩展目录 |
| `tests/test_extension_installer.py` | 本地/Git 安装、验证、替换和清理 |
| `tests/test_cli.py` | CLI 分发和错误 |

## 验证

运行：

```bash
uv run pytest tests/test_extension_installer.py tests/test_cli.py
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
