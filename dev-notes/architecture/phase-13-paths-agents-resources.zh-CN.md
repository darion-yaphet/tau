---
title: "Phase 13：Tau 主目录、路径和 `.agents` 资源"
---

Phase 13 明确 Tau 的用户/项目文件系统位置，并开始为每个普通 Tau 会话自动加载 `.agents` 资源。

实现位于：

```text
src/tau_coding/paths.py
src/tau_coding/resources.py
src/tau_coding/skills.py
src/tau_coding/prompt_templates.py
```

## 新增内容

Tau 现在具有规范路径辅助对象：

```python
TauPaths
```

它定义以下用户级位置：

```text
~/.tau/
~/.tau/sessions/
~/.tau/skills/
~/.tau/prompts/
~/.agents/
~/.agents/skills/
~/.agents/prompts/
```

以及以下项目级位置：

```text
<project>/.tau/skills/
<project>/.tau/prompts/
<project>/.agents/
<project>/.agents/skills/
<project>/.agents/prompts/
```

## 会话位置

早期 TUI 的默认会话路径从项目本地存储：

```text
<project>/.tau/sessions/default.jsonl
```

迁移到按项目路径划分的用户主目录存储：

```text
~/.tau/sessions/<project-hash>/default.jsonl
```

这样既能将 JSONL 对话记录保存在一致的用户拥有位置，又能继续按项目分离会话。

## 自动 `.agents` 资源

TauResourcePaths 现在默认包含 `.agents` 目录。

未提供 cwd 时，Tau 从以下位置加载用户资源：

```text
~/.tau/skills/
~/.agents/skills/
~/.agents/
~/.tau/prompts/
~/.agents/prompts/
```

提供 cwd 时，Tau 还会从以下位置加载项目资源：

```text
<project>/.tau/skills/
<project>/.agents/skills/
<project>/.agents/
<project>/.tau/prompts/
<project>/.agents/prompts/
```

这使 `.agents` 成为普通 Tau 会话环境的一部分，而不是可选扩展机制。

## 优先级

资源目录按优先级递增顺序加载：

1. 用户 Tau 资源
2. 用户 `.agents` 资源
3. 项目 Tau 资源
4. 项目 `.agents` 资源

如果两个目录定义相同名称的技能或提示模板，后加载/优先级更高的资源胜出。这使项目资源可以覆盖用户默认值。

同一目录中的重复名称仍然无效，并会抛出 `ResourceError`。

直接位于 `.agents` 目录中的 `AGENTS.md` 文件不会被视为技能。项目指令发现保留给后续上下文发现阶段。

## CodingSession 集成

`CodingSession.load()` 现在使用会话 cwd 构建默认资源路径：

```python
TauResourcePaths(cwd=config.cwd)
```

这意味着普通编码会话会自动看到用户和项目的 `.agents` 资源。

打印模式在为系统提示组装加载技能时，也会传入可感知 cwd 的资源路径。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_paths.py
tests/test_resources.py
tests/test_skills.py
tests/test_prompt_templates.py
tests/test_cli.py
tests/test_coding_session.py
```

测试验证：

- 规范 Tau 路径构造
- 用户主目录中的项目会话路径
- `.agents` 技能发现
- `.agents` 提示发现
- 项目资源覆盖用户资源
- `AGENTS.md` 不会作为技能加载
- 用于确定性测试的隔离资源路径

## 下一阶段

下一阶段应在这些稳定的用户主目录会话位置之上添加真正的会话管理器和恢复流程。
