# 第一篇：从一句话到一次行动——先建立 Tau 的 Agent 心智模型

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

第 0 篇给了产品和三层地图。本篇不再讲 Tau 是什么，只回答一件事：**一行用户输入，怎样变成可取消、可保存、可观察的一次 Agent 运行。**

读编码 Agent 源码，不少人第一反应是去找模型调用，或去翻命令行入口。关键不在模型开口那一次，而在于它吐出来的东西有没有进入循环：执行工具、收回结果、发出事件、写进历史。

## 从 print mode 的两行看起

`src/tau_coding/cli.py` 在准备好 session 和 renderer 之后，执行：

```python
async for event in session.prompt(prompt):
    renderer.render(event)
```

CLI 不等“最终字符串”从天上掉下来，而是消费事件流。交互式 Textual TUI 也以同样方式消费 `session.prompt(...)`：它把事件映射到组件状态，print mode 把事件映射到终端输出。核心不必知道屏幕上有哪些组件。

整条路径可以画成：

```text
用户输入
  │
  ▼
CodingSession：应用策略、资源、持久化与上下文管理
  │
  ▼
AgentHarness：可复用的 Agent 状态与控制面
  │
  ▼
run_agent_loop：模型 → 工具 → 结果 → 再次模型
  │
  ├──────────────► ModelProvider：流式模型响应
  │
  └──────────────► AgentEvent：渲染、保存、诊断、UI
```

`tau_ai` 处理模型适配，`tau_agent` 装着可移植的循环，`tau_coding` 把它塞进编码环境。本篇沿着中间这条竖线往下走。

**反例：** 若入口写成 `text = await ask_model(prompt); print(text)`，您会丢掉三件事：工具执行中途的进度、`Ctrl+C` 时该补齐的历史（第 3 篇展开取消后历史如何闭合）、以及 TUI / JSON / transcript 本可共享的同一事实序列。等最终字符串的人，往往已经错过踩刹车的时机。

## 一次真实运行长什么样

上面说的事件流不是概念图。仓库里有一份用 FakeProvider + `run_agent_loop` 实跑出来的切片：一轮“模型决定 read → 工具返回 → 模型总结”，共 22 条事件，序列化方式与 CLI 的 `JsonEventRenderer` 完全一致（Pi 兼容 camelCase JSON）。挑几条看（`usage` 等重复字段用 `...` 省略）：

用户 prompt 作为第一条消息进入历史：

```json
{"type":"message_start","message":{"role":"user","content":"What does hello.txt say?","timestamp":1789812357436}}
```

工具调用参数流式拼接完成，得到结构化的 `toolCall` 块：

```json
{"type":"message_update","message":{"role":"assistant","content":[{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],"usage":{...},...},"assistantMessageEvent":{"type":"toolcall_end","toolCall":{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}},...}}
```

工具返回结构化结果：`content` 给模型看，`details` 给 UI 看，`isError` 决定后续分支：

```json
{"type":"tool_execution_end","toolCallId":"call_read_1","toolName":"read","result":{"content":[{"type":"text","text":"hello from tau\n"}],"details":{"path":"hello.txt","lines":1}},"isError":false}
```

工具结果落成一条 `toolResult` 角色消息写入历史，下一轮会回放进 provider 上下文：

```json
{"type":"message_end","message":{"role":"toolResult","toolCallId":"call_read_1","toolName":"read","content":[{"type":"text","text":"hello from tau\n"}],"details":{"path":"hello.txt","lines":1},"isError":false,"timestamp":1789812357437}}
```

最后是 `agent_end`。它的 `messages` 字段就是闭环的完整证据——user → assistant+toolCall → toolResult → assistant，四条消息一条不缺：

```json
{"type":"agent_end","messages":[{"role":"user","content":"What does hello.txt say?",...},{"role":"assistant","content":[{"type":"text","text":"Let me read that file."},{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],...,"stopReason":"toolUse",...},{"role":"toolResult","toolCallId":"call_read_1","toolName":"read","content":[{"type":"text","text":"hello from tau\n"}],...},{"role":"assistant","content":[{"type":"text","text":"The file says: hello from tau"}],...,"stopReason":"stop",...}]}
```

`stopReason: "stop"` 且无工具调用，loop 退出。完整 22 条在 `notes/assets/run-slice/events.jsonl`，带注释的精选版在同目录 `events-annotated.md`；想自己复跑，`uv run python notes/assets/run-slice/generate_slice.py` 即可，不需要任何真实模型 API。

## 应用层先准备，再把输入交给 Harness

`CodingSession.prompt()` 位于 `src/tau_coding/session.py`。在内容交给 Agent 之前，它先跑应用层杂务：调用扩展的 input hook、展开 prompt text、在运行中处理 steering 或 follow-up、刷新模型限制，并在需要时自动压缩上下文。随后才构造 `UserMessage`，并调用：

```python
events = self._harness.prompt_message(prompt_message)
async for event in events:
    yield event
```

项目 resources、slash command、会话落盘和上下文压缩都属于应用策略。通用 Harness 不需要知道 Tau 的目录结构，也不应该 import Textual 或 Rich。第 0 篇那张单向管道在这里具体化：`tau_coding` 准备环境，`tau_agent` 只接收消息。

## Harness 是状态容器，不是 UI 控制器

`src/tau_agent/harness.py` 中的 `AgentHarnessConfig` 只要求跟通用 Agent 有关的那几样：`provider`、`model`、`system`、工具列表、最大轮数、会话标识，以及工具执行前后的回调。`AgentHarness` 自己保存消息列表、运行状态、取消 token 和两类排队消息。

`steer()` 把新方向插进当前回答，`follow_up()` 在当前工作后再追一问；两者进不同队列。Harness 运行时拒绝新的普通 `prompt()`——人可以同时说话，状态机不行。重叠运行一旦发生，两套循环会抢同一份消息列表，事后对不上账。第 3 篇会把这条状态机拆开；这里只要知道：并发输入不是 UI 小问题，核心必须先拒绝。

它的 `_run()` 创建取消信号并调用 `run_agent_loop(...)`。对外只产出 `AgentEvent`，也允许订阅 listener。渲染和保存因此可以消费同一事实序列。

## 让它成为 Agent 的，是工具循环

循环在 `src/tau_agent/loop.py` 的 `run_agent_loop()`。它接收 provider、模型名、system prompt、可变消息历史与工具列表，然后依次发出 `AgentStartEvent`、`TurnStartEvent` 与消息事件。接下来通过 provider 的统一接口请求一次模型响应：

```python
provider.stream_response(
    model=model,
    system=system,
    messages=_provider_context(messages),
    tools=tools,
    signal=signal,
    session_id=session_id,
)
```

这儿没有 OpenAI 或 Anthropic 专用类型。`ModelProvider` 只是一个协议：它应当把一次模型响应作为 `AssistantMessageEvent` 的异步流交出来。服务差异留给 `tau_ai`；Agent loop 只关心统一的消息、工具与取消语义。

模型流并不会原样泄漏给前端。循环中的 `_assistant_events()` 将开始、增量、完成和错误翻译为 `MessageStartEvent`、`MessageUpdateEvent`、`MessageEndEvent`。这些类型定义在 `src/tau_agent/events.py`。对消费者而言，来自不同 Provider 的流式细节被压成同一套事件协议；对循环而言，最终仍能得到一个完整的 `AssistantMessage`。

如果这个 assistant message 没有工具调用，循环结束本轮；如果它提出了调用，Tau 就从工具表里按名字找工具，并围绕执行发出：

```text
ToolExecutionStartEvent
ToolExecutionUpdateEvent（如工具产生增量）
ToolExecutionEndEvent
MessageStartEvent / MessageEndEvent（ToolResultMessage）
```

工具结果会被追加进消息历史，然后循环再次请求模型。模型并不知道“本机已经执行过命令”这个隐式事实；它读到的是追加进历史的一条结构化 `ToolResultMessage`。成功输出、参数错误、未知工具、被策略拦住的调用、取消导致的中断，都会以结果的形式回到模型上下文（第 4 篇展开工具反馈与安全边界）。

**反例：** 若 CLI 直接调用 `provider.stream_response(...)` 并打印文本增量，模型仍可以“提出” `read`，但没有人执行它，也没有 `ToolResultMessage` 回去。下一轮模型只能继续编。工具不是外挂函数，它们是闭环里的传感器和执行器。

## 事件为什么比“回调结果”更重要

`AgentEvent` 是带判别字段的联合类型，覆盖 Agent 开始/结束、轮次开始/结束、消息开始/更新/结束，以及工具执行开始/更新/结束。渲染器、TUI、持久化和诊断可以各自订阅同一事实序列，而不必每人再实现一遍 Agent loop。

错误也沿这套协议流动：Provider 错误成为 assistant message，未知或被阻止的工具成为 error result；Harness 在中断时补齐未返回的工具结果，避免历史留下悬空 tool call。

## 三分钟核对

不动手的话，上面这些只是又一篇博客。两个离线动作足够验证本篇的核心论点：

1. **复跑切片。** `uv run python notes/assets/run-slice/generate_slice.py`，然后翻开重新生成的 `notes/assets/run-slice/events.jsonl`，对照正文确认事件顺序：`agent_start` 开场、两轮 turn、`agent_end` 收尾，22 条一条不多。整条链路不碰任何真实模型 API。
2. **跑 Harness 测试。** `uv run pytest tests/test_agent_harness.py -q`，10 个测试全绿。测试名就是论点的可执行版本：`test_harness_rejects_overlap_and_drains_followups` 对应正文“Harness 运行时拒绝新的普通 `prompt()`”，`test_harness_repairs_interrupted_tool_calls` 对应“中断时补齐未返回的工具结果”。

## 用这张地图继续读源码

建议按 `cli.py → session.py → harness.py → loop.py → events.py / provider.py → tau_ai/` 的顺序阅读：先看事件如何被消费，再回溯产品策略、核心状态、工具闭环与跨层协议。

判断标准只有一条：**模型调用只是 Agent 的一次思考；思考进了工具反馈、消息状态和事件观察组成的循环，系统才开始像 Agent 工作。**

下一篇从这条链路最外侧的输入说起：`tau_ai` 怎么把各家模型服务不一样的流式行为，收敛成核心循环能依赖的 Provider 契约。
