---
title: "阶段 24：会话树分支"
---

此阶段在 Textual TUI 中公开 Tau 现有的只追加会话树。它遵循 Pi 的核心行为：
在树中移动属于结构性的会话变更，而不是编辑对话记录。

## 变更内容

`/tree` 为当前会话打开模态树选择器。选择器将每个分支的条目放在一起，并把历史最长的子节点
视为主分支。较短的替代分支会紧跟在其父节点后面，在分支点缩进显示，然后再继续显示不缩进的
主历史。它会标记当前叶子节点，并支持三个操作：

- `Enter` 将当前叶子节点移动到所选条目。
- `S` 通过新的 `branch_summary` 条目移动当前叶子节点。
- `Ctrl+T` 切换工具调用条目的显示，便于在工具密集的历史中导航。

这两个移动操作都会保留所有现有 JSONL 条目。普通导航只存在于内存中。如果用户立即退出，
重新打开时会选择文件顺序中最后一个非旧式叶子条目，而不会恢复尚未提交的选择。下一条消息或
状态变更会以所选目标为父节点追加，从而让新分支持久化。摘要导航会立即追加其
`branch_summary`，因此该摘要会成为持久尖端。选择器会有意隐藏模型变更、思考级别变更、
历史叶子指针和会话信息等元数据条目；它只显示用户消息、助手消息、工具调用、压缩摘要和分支摘要。

## 分支摘要

Tau 已经有 `BranchSummaryEntry` 类型。此阶段让它参与重放：当当前根到叶路径包含分支摘要时，
`SessionState` 会将其转换为用户上下文摘要消息。

分支摘要现在使用当前提供商和模型，对所选条目之后当前路径上的消息进行总结。`CodingSession`
在主 `AgentHarness` 对话记录之外发送一次性摘要请求，使用 Pi 风格的结构化摘要提示词，其中包含
目标、约束、进度、决策和后续步骤。TUI 同时支持默认提示词和每个分支的自定义关注指令。Tau 将
结果文本存入现有 `BranchSummaryEntry` 结构。如果提供商没有返回可用文本、报告错误或抛出异常，
Tau 会回退到自动压缩使用的同一确定性摘要辅助函数。

## 边界

`tau_agent.session` 负责 `branch_summary` 条目的通用重放语义。`tau_coding.CodingSession`
负责分支导航、模型摘要创建、回退摘要创建、存储变更和 harness 消息替换。Textual TUI 只显示
选项，并调用用户所选的会话方法。

## 验证

可用检查：

```bash
uv run pytest tests/test_session.py tests/test_coding_session.py tests/test_commands.py tests/test_tui_app.py -q
uv run ruff check src/tau_agent/session/memory.py src/tau_coding/session.py src/tau_coding/commands.py src/tau_coding/tui/app.py tests/test_session.py tests/test_coding_session.py tests/test_commands.py tests/test_tui_app.py
```
