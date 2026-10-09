---
title: "Phase 10：系统提示组装"
---

Phase 10 添加 Tau 的规范系统提示构建器。

实现位于：

```text
src/tau_coding/system_prompt.py
```

## 新增内容

Tau 现在可以根据以下内容构建确定性的 Pi 风格系统提示：

- Tau 的默认编码代理身份
- 已启用工具
- 工具提示片段
- 工具提示指南
- 额外指南
- 可选自定义提示文本
- 可选追加提示文本
- 项目上下文文件
- 已加载技能
- 当前日期
- 当前工作目录

## 存在原因

较早阶段添加了工具、技能、提示模板和编码会话。在此阶段之前，CLI 使用一个只列出工具的小型本地提示构建器。Tau 现在拥有共享的提示组装层，可供打印模式、编码会话以及后续 UI/会话前端使用。

## 默认提示结构

默认提示以 Tau 的身份开始：

```text
You are an expert coding assistant operating inside Tau, a coding agent harness.
```

随后使用各工具的 `prompt_snippet` 加入可用工具部分：

```text
Available tools:
- read: Read file contents
- write: Create or overwrite files
- edit: Make precise file edits ...
- bash: Execute bash commands ...
```

接下来加入从已启用工具收集的指南：

```text
Guidelines:
- Use bash for file operations like ls, rg, find
- Use read to examine files instead of cat or sed.
- Use write only for new files or complete rewrites.
- Be concise in your responses
- Show file paths clearly when working with files
```

指南会按确定顺序去重。

默认提示还复现 Pi 的渐进披露文档块，并针对 Tau 做了调整。它指向有关扩展、技能、提供商/模型、命令、TUI 行为和架构的打包 Tau 文档和示例，并告知模型只在处理 Tau 专用问题时读取这些内容。自定义提示仍会替换这个默认文档块。

## 自定义提示

如果提供 `custom_prompt`，它会替换默认的 Tau 身份、工具和指南部分。

Tau 仍会按照 Pi 的行为，在自定义提示后追加：

1. 追加的系统提示文本
2. 项目上下文
3. 技能部分（如果启用了 `read` 工具）
4. 当前日期
5. 当前工作目录

## 项目上下文

项目指令以 `ProjectContextFile` 值表示，并按 Pi 的类 XML 风格格式化：

```xml
<project_context>

Project-specific instructions and guidelines:

<project_instructions path="/repo/AGENTS.md">
...content...
</project_instructions>

</project_context>
```

Phase 10 负责格式化提供的上下文文件。对 `AGENTS.md` 和 `CLAUDE.md` 等文件的完整发现可在以后扩展。

## 技能

技能按 Pi 的 XML 风格技能索引格式化：

```xml
<available_skills>
  <skill>
    <name>python-testing</name>
    <description>Write and run Python tests.</description>
    <location>/home/user/.tau/skills/python-testing/SKILL.md</location>
  </skill>
</available_skills>
```

只有启用 `read` 工具时，Tau 才会包含技能部分。这与 Pi 一致：当任务与技能描述匹配时，模型应使用 `read` 加载完整技能文件。

提示模板不会插入系统提示。它们是用户提示扩展资源。

## 集成

打印模式现在使用 `build_system_prompt()`，而不再使用本地的最小提示构建器。

`CodingSessionConfig.system` 现在可选。如果省略，`CodingSession.load()` 会根据以下内容构建系统提示：

- cwd
- 已配置工具或默认编码工具
- 已加载技能
- 自定义/追加提示选项
- 提供的上下文文件

现有调用者仍可传入显式的 `system` 字符串。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_system_prompt.py
tests/test_cli.py
tests/test_coding_session.py
```

测试验证：

- 默认提示部分
- 可用工具格式化
- 没有片段的隐藏工具
- 指南去重
- 自定义提示行为
- 项目上下文格式化
- 技能 XML 转义
- 仅在存在 `read` 时包含技能
- 打印模式集成
- 编码会话集成

## 下一阶段

路线图下一阶段是 Rich 控制台渲染。这里引入的系统提示构建器为未来前端提供共享提示组装层，避免重复提示逻辑。
