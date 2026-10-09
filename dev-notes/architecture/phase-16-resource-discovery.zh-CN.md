---
title: "Phase 16：稳健的资源发现"
---

Phase 16 使技能和提示模板发现具备足够容错能力，可以处理真实的用户资源目录。

实现位于：

```text
src/tau_coding/resources.py
src/tau_coding/skills.py
src/tau_coding/prompt_templates.py
src/tau_coding/session.py
src/tau_coding/commands.py
```

## 新增内容

Tau 现在具有 `ResourceDiagnostic` 类型，用于非致命的资源发现备注。它记录：

- 资源类型，例如 `skill` 或 `prompt`
- 已知时记录受影响的资源名称
- 已知时记录路径
- 简短消息
- 严重程度字符串

技能和提示模板现在具有诊断式加载器：

```python
load_skills_with_diagnostics(...)
load_prompt_templates_with_diagnostics(...)
```

旧的严格加载器仍然存在，供希望发现错误抛出 `ResourceError` 的调用者使用。

## 发现行为

资源目录仍按优先级递增顺序加载：

1. 用户 Tau 资源
2. 用户 `.agents` 资源
3. 项目 Tau 资源
4. 项目 `.agents` 资源

如果较高优先级资源与较低优先级资源同名，则较高优先级资源胜出。诊断式加载器会报告覆盖，而不是隐藏它。

如果一个目录包含两个同名技能，Tau 会保留第一个确定性匹配项，并报告重复名称诊断。这样一个有问题的本地资源不会阻止 TUI 打开。

## 编码会话集成

`CodingSession.load()` 使用诊断式加载器。加载后的诊断可通过以下属性访问：

```python
session.resource_diagnostics
```

这让发现诊断留在 `tau_coding` 中，因为本地资源、命令和 UI 行为均位于此处。可复用的 `tau_agent` 包继续独立于资源路径和 Markdown 发现。

## 命令和 TUI 可见性

斜杠命令注册表现在包括：

```text
/resources
```

它会显示加载的技能和提示模板数量，以及所有发现诊断。`/status` 也包含资源诊断数量，`/skills` 则在存在技能诊断时显示它们。

Textual TUI 不需要专用诊断 API。它已经将命令结果渲染为对话记录条目，因此 `/resources` 可以交互式显示相同信息。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_skills.py
tests/test_prompt_templates.py
tests/test_commands.py
tests/test_coding_session.py
```

测试验证：

- 确定性的覆盖处理
- 重复技能诊断
- 容错的 `CodingSession` 加载
- `/status` 资源诊断数量
- `/resources` 命令输出
