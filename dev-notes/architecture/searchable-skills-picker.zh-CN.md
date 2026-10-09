# 可搜索的技能选择器

Issue #451 将 `/skills` 添加为仅限 TUI 的发现工作流。常规命令注册表返回 UI 意图，使 Textual 保持在可复用命令/会话层之外。Textual 适配器按名称排序显示已加载技能，并以不区分大小写的方式过滤名称和描述。

选择一行会将 `/skill:<name>` 放入提示词，但不会提交。F1 会在模态框中预览完整的技能头部描述，同时保留 Space 用于多词搜索。Ctrl+Enter 会把完整的 `SKILL.md` 追加到仅用于显示的 TUI 状态，因此它会出现在对话中，但不会进入持久化会话历史或模型上下文。取消会恢复已提交的 `/skills` 文本。空集合和空搜索结果会显示明确状态；现有资源诊断仍保留在原有界面中。

使用以下命令验证：

```bash
uv run pytest tests/test_commands.py tests/test_tui_app.py
```
