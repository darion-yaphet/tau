---
title: "阶段 20.3：技能调用可靠性"
---

阶段 20.3 加强了 Tau 公开和调用 Markdown 技能的方式。

## 自动提供技能

当 `read` 工具可用时，已加载技能会包含在系统提示词中：

```xml
<available_skills>
  <skill>
    <name>testing</name>
    <description>Use when writing tests</description>
    <location>/repo/.agents/skills/testing.md</location>
  </skill>
</available_skills>
```

提示词现在采用 Pi 的措辞：

```text
Read the full skill file when the task matches its description.
```

这意味着模型可以从索引中选择相关技能，对列出的位置调用 `read`，并以普通工具结果的形式
接收完整的技能 Markdown。

## 手动调用技能

`/skill:<name> [instructions]` 仍是提示词展开指令，而不是会结束当前轮次的斜杠命令。
它现在会展开成相同的 Pi 风格技能块：

```text
<skill name="testing" location="/repo/.agents/skills/testing.md">
References are relative to /repo/.agents/skills.

# Testing
Run pytest.
</skill>

add parser tests
```

该块是自包含的：它包含源文件，并告诉模型技能内部的相对引用应从哪里解析。

## TUI 显示

会话仍会发送并持久化完整展开的技能块，使代理拥有完整指令。TUI 在渲染用户消息时会解析这个
结构化块，改为显示紧凑的技能项：

```text
Using skill: testing
```

如果原始 `/skill:<name>` 输入包含额外指令，这些指令会在紧凑技能项之后作为可见用户消息渲染。
完整技能 Markdown 不会出现在常规对话视图中。

## 边界

技能发现和提示词展开仍位于 `tau_coding`。`tau_agent` 只看到普通的提供商中立消息和工具调用。
模型可见的技能索引使用现有 `read` 工具，而不会给可复用 harness 增加专用技能工具。
TUI 解析只影响展示，不会改变存储内容或提供商可见的消息内容。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_skills.py
tests/test_cli.py
tests/test_coding_session.py
tests/test_system_prompt.py
tests/test_tui_adapter.py
tests/test_tui_app.py
```

测试验证两条路径：

- 显式 `/skill:<name>` 展开会向提供商发送完整技能内容
- 系统提示词列出已加载技能的位置，模型触发的 `read` 调用可以将技能文件加载到下一次提供商轮次
