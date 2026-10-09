---
title: "阶段 9：技能与提示词模板"
---

阶段 9 增加 Tau 的第一个 Markdown 资源系统。

实现位于：

```text
src/tau_coding/resources.py
src/tau_coding/skills.py
src/tau_coding/prompt_templates.py
```

## 新增内容

Tau 现在可以从 Tau 资源根目录加载并使用 Markdown 资源：

```text
~/.tau/
  skills/
  prompts/
```

此阶段包括：

- 资源路径辅助函数
- 最小 Markdown frontmatter 解析
- 技能加载
- `/skill:name` 提示词展开
- 技能索引生成
- 提示词模板加载
- `{{ variable }}` 模板渲染
- 用于技能展开的轻量 `CodingSession` 集成

## 资源路径

`TauResourcePaths` 描述 Tau 资源的位置：

```python
from tau_coding import TauResourcePaths

paths = TauResourcePaths()  # ~/.tau
paths.skills_dir            # ~/.tau/skills
paths.prompts_dir           # ~/.tau/prompts
```

测试和调用方可以提供自定义根目录：

```python
paths = TauResourcePaths(root=Path("/tmp/resources"))
```

## Frontmatter

技能和提示词模板可以包含简单 frontmatter：

```md
---
description: Write and run Python tests.
---

# Python Testing

Use pytest.
```

解析器有意只支持简单 `key: value` 对。它不会执行代码，也不实现完整 YAML 解析器。

## 技能

Tau 支持两种技能布局：

```text
~/.tau/skills/python-testing/SKILL.md
~/.tau/skills/git-review.md
```

加载技能：

```python
from tau_coding import TauResourcePaths, load_skills

skills = load_skills(TauResourcePaths())
```

技能名称重复会抛出 `ResourceError`。

## 技能展开

技能命令使用路线图语法：

```text
/skill:python-testing add tests for parser.py
```

展开后生成类似以下提示文本：

```md
<skill name="python-testing" location="/path/to/python-testing/SKILL.md">
References are relative to /path/to/python-testing.

...skill markdown...
</skill>

add tests for parser.py
```

`CodingSession.prompt()` 在将提示词发送给 `AgentHarness` 前展开技能命令。`/skill:name` 不会被
`handle_command()` 消费，因为它是提示词展开指令，而不是结束运行的命令。

## 技能索引

`build_skill_index()` 创建简洁的可用技能列表，供未来组装系统提示词：

```md
Available skills:
- python-testing: Write and run Python tests.
- git-review: Review git changes safely.
```

阶段 10 可以在构建完整系统提示词时使用该索引。

## 提示词模板

提示词模板位于：

```text
~/.tau/prompts/review.md
.agents/prompts/review.md
```

Tau 将加载的提示词模板视为斜杠命令提示词展开。名为 `.agents/prompts/example.md` 的文件可以通过
以下方式调用：

```text
/example src/app.py
```

Tau 在把命令发送给模型前展开它。命令后的调用文本支持 Pi 兼容参数变量：

- `$1`、`$2`……表示位置参数
- `$@` 或 `$ARGUMENTS` 表示所有参数
- `${N:-default}` 表示带默认值的位置参数
- `${@:N}` 和 `${@:N:L}` 表示简单切片

```md
Review $@ for correctness.
```

如果提示词模板没有参数占位符，Tau 会在空行后追加调用参数。仍支持旧式 `{{ arguments }}` 和
`{{ args }}` 占位符。斜杠命令展开时，其他占位符会替换为空白，使自定义提示词可以包含可选字段，
而不会导致 TUI 崩溃。

直接模板渲染也支持简单的 `{{ variable }}` 占位符：

```md
Review {{ target }} for {{ focus }}.
```

直接渲染模板：

```python
from tau_coding import load_prompt_templates, render_prompt_template

templates = load_prompt_templates(paths)
text = render_prompt_template(
    templates[0],
    {"target": "auth.py", "focus": "security"},
)
```

缺失变量会抛出 `ResourceError`，额外变量会被忽略。这种严格行为适用于直接渲染。斜杠命令展开更为
宽容，会把缺失的自定义变量渲染为空文本。

## 边界

此阶段不实现完整系统提示词组装，只增加资源基础组件和 `CodingSession` 中的最小技能展开钩子。

完整提示词组装留待后续阶段，它将组合：

- Tau 基础身份
- 工具和工具指南
- 项目指令
- 技能索引
- 提示词模板
- 后续的扩展片段
- 环境上下文

## 测试

此阶段由以下测试覆盖：

```text
tests/test_resources.py
tests/test_skills.py
tests/test_prompt_templates.py
tests/test_coding_session.py
```

测试验证：

- 资源路径
- frontmatter 解析
- 技能加载与重复检测
- 技能展开
- 技能索引输出
- 提示词模板加载
- 模板渲染
- 编码会话技能展开

## 下一阶段

路线图的下一阶段是系统提示词组装。这里增加的资源模型和加载器为该阶段提供所需的技能和模板输入。
