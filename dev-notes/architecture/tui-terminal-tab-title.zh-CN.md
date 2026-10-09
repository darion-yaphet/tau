# TUI 终端标签页标题

Issue：#260

## 变更内容

Textual TUI 现在会用当前 Tau 会话名称和运行状态更新终端模拟器的窗口/标签页标题：

- 空闲的未命名会话：`τ`
- 空闲的已命名会话：`τ | <session name>`
- 运行中的会话：动态盲文转轮前缀加空闲标题

TUI 关闭时，Tau 会写入中性的 `τ` 标题，避免终端残留陈旧的运行帧。

## 为什么它位于 `tau_coding`

终端标题更新属于前端事项。实现使用 TUI 已拥有的状态：

- `TuiState.running`，由适配器根据 `AgentStartEvent`、`AgentEndEvent` 和不可恢复的 `ErrorEvent` 填充；
- `CodingSession.session_title`，由 `/name` 和自动会话命名更新。

没有向 `tau_agent` 或 `tau_ai` 添加终端或 Textual 依赖。

## 机制

`src/tau_coding/tui/terminal_title.py` 发出 OSC 0 序列：

```text
ESC ] 0 ; <title> BEL
```

OSC 0 得到常见终端模拟器的广泛支持，用于设置终端窗口/标签页标题。`TerminalTitleController` 仅在计算出的标题变化时写入，因此空闲刷新不会向 stdout 发送大量内容。运行期间，现有活动计时器同时驱动应用内提示词动画和标签页标题转轮。

## 能力检测与安全

只有当 stdout 是 TTY、`TERM` 不是 `dumb` 且未检测到 CI 时，才启用标题写入。用户可通过 `TAU_TERMINAL_TITLE=0` 选择退出；仅在辅助函数规则支持时，CI/非 TTY 场景才可用 `TAU_TERMINAL_TITLE=1` 显式选择加入。标题写入采用尽力而为策略：如果 Tau 写入 OSC 序列时终端流抛出异常，Tau 会对该 TUI 进程禁用后续标题写入，而不会中断会话。

会话名称进入 OSC 负载前会被清理：移除包括 BEL 和 ESC 在内的 C0/C1 控制字符，并将标题限制为 120 个字符。

## 测试与手动验证

自动化测试覆盖标题构造、清理、能力检测、去重写入，以及 TUI 运行/命名/空闲状态转换。

手动验证：

1. 在真实终端标签页中打开 `tau`。
2. 确认未命名的空闲 TUI 在标签页标题中显示 `τ`。
3. 运行 `/name build notes`，确认标签页变为 `τ | build notes`。
4. 提交一个运行时间足够长的提示词，以观察动态转轮前缀。
5. 取消运行或等待运行结束，确认转轮停止。
6. 退出 TUI，确认标签页重置为 `τ`。
7. 使用 `TAU_TERMINAL_TITLE=0 tau` 再重复一次，确认 Tau 不管理标签页标题。
