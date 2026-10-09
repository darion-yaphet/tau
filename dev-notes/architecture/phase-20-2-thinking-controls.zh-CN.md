---
title: "阶段 20.2：思考模式控制"
---

阶段 20.2 将思考模式设为显式的 Tau 编码会话设置，并增加了用于更改它的 TUI 控件。

## 新增内容

`tau_coding.thinking` 定义了 Tau 支持的思考模式：

```text
off
minimal
low
medium
high
xhigh
```

默认值为 `medium`，与 Pi 的默认推理深度偏好一致。Tau 会根据提供商和模型能力验证可见控件，
并在配置的提供商声明支持时传递 OpenAI 兼容的 `reasoning_effort`。不支持的模型会把思考控件
显示为不可用，而不会展示 Tau 无法安全发送的模式。

## 会话持久化

新会话会在初始模型条目后追加一个初始 `thinking_level_change` 条目。显式更改会再追加一个
`thinking_level_change` 条目。作为最后一个非旧式叶子条目，它会成为当前尖端，因此恢复时可以从
会话树重建当前思考模式。

`CodingSession` 公开：

```python
session.thinking_level
session.available_thinking_levels
await session.set_thinking_level("high")
await session.cycle_thinking_level()
```

这让思考状态保留在 `tau_coding` 中，而 `tau_agent.session` 仍是可移植的重放层，负责重建
`ThinkingLevelChangeEntry` 的值。

## 命令与 TUI

Textual TUI 默认将 `Shift-Tab` 绑定为循环切换思考模式。该按键可在 `~/.tau/tui.json` 中配置：

```json
{
  "keybindings": {
    "thinking_cycle": "f3"
  }
}
```

侧边栏和紧凑会话行会先读取 `session.available_thinking_levels`。当当前提供商或模型没有思考
能力元数据时，TUI 会把控件显示为不可用。如果用户仍尝试循环切换或设置思考模式，会话会抛出
面向用户的原因说明。

Tau 不会在默认命令注册表中注册独立的 `/thinking` 命令。命令界面与 Pi/Codex 保持一致，
模型选择和推理控制与模型选择及会话状态放在一起。当当前提供商或模型无法更改思考模式时，
`/session` 会报告 `Thinking mode: unavailable` 及其原因。

## 提供商能力

提供商设置可以用以下配置声明思考支持：

```json
{
  "thinking_levels": ["off", "low", "medium", "high"],
  "thinking_models": ["gpt-5.5"],
  "thinking_default": "medium",
  "thinking_parameter": "reasoning_effort"
}
```

`thinking_models` 是可选的。如果省略，声明的级别适用于该提供商的所有模型。Tau 内置提供商只会
为添加模型时已检查过推理控制的模型声明思考支持。向目录添加新模型时，应先验证提供商文档或 API，
并且只为确实支持 Tau 所配置思考参数的模型更新 `thinking_models`。

支持的参数映射为：

- `reasoning_effort`：顶层 OpenAI 兼容聊天补全字段。
- `reasoning.effort`：嵌套的 Responses API 结构；对于显式选择该结构的提供商配置，发送
  `{ "reasoning": { "effort": "...", "summary": "auto" } }`。
- `anthropic.thinking`：Anthropic 扩展思考；发送
  `{ "thinking": { "type": "enabled", "budget_tokens": ... } }`。

Tau 会记录控件不可用的明确原因：

- 没有 `thinking_levels` 的提供商会报告该提供商未声明思考能力元数据。
- 包含 `thinking_models` 的提供商，会在当前模型不属于该能力集合时报告它未被列出。
- 当当前模型声明具备能力时，提供商专属配置会使用上面映射的运行时参数。

这让持久化提供商元数据位于 `tau_coding`，提供商专属运行时旋钮位于 `tau_ai`，显示选择则位于
CLI/TUI 代码。

## 提供商对比

OpenAI 的公共 API 为聊天补全公开 `reasoning_effort`，并为 Responses API 推理模型公开
`reasoning.effort`。支持的值取决于模型；当前文档列出 `none`、`minimal`、`low`、`medium`、
`high` 和 `xhigh`，默认值和支持情况因模型而异。Tau 的 OpenAI 直连条目只为已配置为具备推理
能力的模型，将 Tau 的标准化级别映射到 `reasoning_effort`。

Codex 客户端为支持的模型公开 `model_reasoning_effort`。Tau 复现 Pi 的 Codex 订阅映射：`off`
省略 `reasoning` 字段，`minimal` 请求 `low`，启用的级别发送 `reasoning.effort`，并设置
`reasoning.summary: "auto"`。内置 `openai-codex` 模型只为经过验证的 GPT-5.x Codex 模型列表
声明此支持。

Anthropic 曾同时使用显式扩展思考 token 预算和较新的自适应/强度控制，而且支持情况会随模型系列
变化。Tau 会为声明支持扩展思考的内置 Claude 模型，将标准化思考级别映射到显式 Anthropic
`budget_tokens`。`off` 会省略 `thinking` 载荷。

没有声明能力元数据的模型或提供商被视为不具备可配置思考模式。切换到这类模型时，会保留已有的
会话思考设置，同时隐藏控件，因此切回有能力的模型时，如果该设置有效便可复用。如果目标模型具备
能力但不支持当前级别，Tau 会将其调整为该模型或提供商的默认值。

## 边界

思考控件仍位于 Textual 专属渲染之外。提供商适配器把支持的推理流转换成提供商中立的思考事件，
`tau_agent` 转发这些事件，但不会把它们记录为持久助手消息；Textual TUI 决定显示还是隐藏它们。
内置 TUI 默认隐藏思考 token，并提供 `Ctrl+T` 作为前端切换键。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_thinking.py
tests/test_commands.py
tests/test_coding_session.py
tests/test_agent_loop.py
tests/test_tau_ai.py
tests/test_tui_adapter.py
tests/test_tui_config.py
tests/test_tui_app.py
```
