---
title: "Phase 17：TUI 斜杠命令自动补全"
---

Phase 17 为 Tau 的 Textual TUI 添加提示自动补全，同时将逻辑保留在 `tau_coding` 中。

实现位于：

```text
src/tau_coding/tui/autocomplete.py
src/tau_coding/tui/app.py
src/tau_coding/tui/widgets.py
```

## 新增内容

Tau 现在根据编码会话元数据为提示输入构建补全建议：

- 来自活动 `CommandRegistry` 的已注册斜杠命令
- 用于 `/skill:<name>` 的已加载技能

纯补全模型为：

```python
CompletionItem
CompletionState
build_completion_state(...)
```

这样无需 Textual 即可测试补全匹配和替换行为。Textual 应用只负责渲染、按键处理，以及将选中补全应用到提示输入。

## TUI 行为

当提示以 `/` 开头时，Tau 会显示匹配的斜杠命令。

示例：

```text
/st       -> /status
/ski      -> /skill:
```

命令补全从活动 `CommandRegistry` 读取规范命令名称、可执行别名和不可执行搜索词。搜索词仅用于筛选：接受搜索词时会插入规范命令。例如，`/res` 可以直接匹配 `/resume`，也可以通过 `/new` 命令的 `reset` 搜索词匹配 `/new`；因此自动补全会优先排列直接命令或别名匹配，并将后备搜索词匹配保留在其下方。

当提示以 `/skill:` 开头时，Tau 会显示匹配的已加载技能。接受技能补全会保留请求的其余部分：

```text
/skill:r fix tests -> /skill:review fix tests
```

提示输入处理：

- `Tab`：接受任何选中的补全
- `Enter`：接受选中的斜杠命令、技能、提示模板、参数和 shell 路径补全；对于 `@` 文件引用，Enter 会提交原始提示文本，而不应用建议
- `Down`：选择下一个补全
- `Up`：选择上一个补全

建议渲染在提示下方的小条带中，而不是混入对话记录。

## 提示模板

提示模板已经加载并可在 `CodingSession` 上使用，但 Tau 尚无面向用户的 `/prompt:<name>` 或等价模板调用命令。因此 Phase 17 尚未公开提示模板参数补全；应在存在模板调用命令后添加。

## 边界

自动补全不进入 `tau_agent`。

可复用代理框架不依赖 Textual、命令注册表、技能、提示模板或 Tau 资源路径。这些仍是由 `tau_coding` 负责的应用关注点。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_tui_autocomplete.py
tests/test_tui_app.py
```

测试验证：

- 已注册命令建议
- `/skill:` 建议
- 技能补全后保留请求文本
- 补全选择循环
- 根据上下文使用 Enter 接受补全，或提交原始 `@` 文件引用
- 对每种补全使用 Tab 接受
