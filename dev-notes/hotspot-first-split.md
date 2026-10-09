# 编码热点拆分：第一刀

这次重构针对 `tau_coding` 里三块已经过大的模块。第一刀只搬家，不改公共进口，也不改运行时行为。

## 挪了什么

- `tau_coding.session_terminal` 管输入栏终端命令的值对象和解析，外加上下文消息的格式化。`CodingSession` 仍然负责执行和落盘。
- `tau_coding.session_tree` 管会话树的值对象，外加遍历和展示的辅助函数。`CodingSession` 仍然管着分支、持久化和活跃状态修复。
- `tau_coding.tui.completion_widgets` 负责画补全行。
- `tau_coding.tui.completion_layout` 管 prompt 活动渲染和补全窗口计算。

原来的 `session.py`、`tui/app.py` 和 `tui/widgets.py` 会把挪走的符号再 export 回来。这是故意的：现有的 CLI、RPC、TUI、扩展和测试的进口照旧能用，实现却有了更干净的位置。换进口的那天再做，避免和这次结构搬家缠在一起。

## 为什么这刀算安全

第一刀只挪值对象、解析、格式化和遍历逻辑。带状态的边界还留在原地：

- 会话生命周期、存储事务、Provider 控制和恢复，仍在 `CodingSession`；
- Textual 应用生命周期和 action 分发，仍在 `TauTuiApp`；
- 带状态的 transcript 和侧栏 widget，仍在 `tui.widgets`。

几处既有的宽容行为也原样留着，包括畸形父节点成环时的树遍历、终端命令的语法结果，以及补全布局渲染。语义没有因为搬家改口。成环的会话树不是正常数据，旧代码已经能在这种树上走通；这刀不许把那条路径刨掉。

所以这回生命周期代码碰都没碰。能挪不等于该把会改状态的代码一块儿塞进新文件。

## 怎么验

```bash
uv run pytest tests/test_coding_session.py tests/test_session_usage.py tests/test_session_manager.py
uv run pytest tests/test_tui_app.py tests/test_tui_components.py tests/test_tui_autocomplete.py
uv run ruff check src tests
uv run mypy
```

测试通过，只说明这刀没改行为；没通过，先看是搬家漏了 export，还是误伤了生命周期。

## 下一刀

继续在同一套门面后面，把内聚的实现一块块拆出来：会话资源 / 树辅助、侧栏 widget、transcript 渲染，以及独立的 TUI 输入或屏幕组件。仍然只做结构搬家。若要动会改状态的生命周期代码，先把回归测试铺厚。
