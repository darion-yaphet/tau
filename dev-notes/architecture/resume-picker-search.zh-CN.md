# `/resume` 选择器搜索栏

## 变更内容

`/resume` 模态框（`src/tau_coding/tui/app.py` 中的 `SessionPickerScreen`）现在带有搜索字段，与模型选择器（`ModelPickerScreen`）和登录提供商选择器（`LoginProviderPickerScreen`）中的现有搜索体验一致。

- 打开 `/resume`（或按 `Ctrl+R`）时，会聚焦会话列表上方新增的 `#session-picker-search` 输入框。
- 输入内容会按会话名称或模型实时过滤可见会话（不区分大小写的子字符串匹配）。工作目录不影响搜索结果。
- 无论焦点位于搜索字段还是列表，方向键、Enter 和 Escape 的行为都保持不变——搜索输入框会把这些按键转发给选择器屏幕，而不是用它们编辑自身文本。
- 提交搜索字段（Enter）会选择当前高亮的会话，与列表获得焦点时按 Enter 相同。
- 没有匹配会话时，帮助行切换为 “No matching sessions - Escape closes”。

## 存在原因

会话列表会随时间增长，通过滚动未过滤列表查找某个特定旧会话会变得繁琐。这使 `/resume` 与已经提供此模式的模型选择器和登录选择器保持一致。

## 与现有选择器的对应关系

新的 `SessionPickerSearchInput` 及 `SessionPickerScreen` 中的刷新/过滤逻辑仿照 `ModelPickerSearchInput` / `_filter_model_choices` 和 `LoginProviderSearchInput` / `_filter_login_providers`：

- 专用 `Input` 子类将导航键（`up`、`down`、`escape`）保留在选择器本地，而不是让 `Input` 部件消费它们。
- 模块级辅助函数 `_filter_session_records` 根据完整记录列表和当前搜索文本计算可见记录。
- 每当搜索文本或记录集变化时，`_refresh_session_list` 都会重建 `ListView` 子元素和帮助文本。

无需更改 `tau_agent` 或 `tau_ai`——这完全是 `tau_coding` 的 TUI/前端变更，符合让可复用 Agent harness 不含 Textual 特有代码的原则。

## 测试

- `test_tui_app_session_picker_search_filters_sessions`——输入查询会缩小列表，Enter 仍会恢复高亮的（已过滤）会话。
- `test_tui_app_session_picker_search_does_not_match_workspace_path`——会话名称和模型可匹配，而工作目录匹配会被忽略。
- `test_tui_app_session_picker_search_with_no_matches_shows_help_text`——输入无匹配查询会清空列表并更新帮助文本。
- 现有 `/resume` 选择器测试（方向键导航、Enter 恢复、人类可读元数据）保持不变并继续通过。

## 手动验证

1. 创建几个标题/模型不同的会话（或恢复真实历史），让 `/resume` 包含多行。
2. 运行 `tau`，然后按 `Ctrl+R`（或输入 `/resume` 并按 Enter）。
3. 确认搜索字段已聚焦，然后输入会话名称或模型名称的一部分——列表应实时缩小。工作区路径片段不应产生匹配。
4. 按 Enter 恢复高亮会话。
5. 尝试一个无匹配项的查询，并确认帮助文本变为 “No matching sessions - Escape closes”。
6. 随时按 Escape，确认选择器仍会关闭而不恢复会话。
