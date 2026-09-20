# 第三篇：Agent 的大脑——Harness、消息与事件契约

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

Provider 解决“怎样收到可靠回答”；Agent 核心解决：**怎样让回答在多轮任务中成为可继续、可观察、可取消的状态？** 收到回答不算完事：没法接着用、没法盯着看、没法中途拉闸，下一轮就没有可信起点。

Tau 把它放在 `tau_agent`：这里只有可移植的消息、工具、事件、loop 和 `AgentHarness`，没有命令行、项目目录、Rich 或 Textual。同一颗 Agent 大脑可供终端、测试、脚本或另一个前端复用。

本篇暂不解释工具如何执行。重点是 Agent 在一次或多次请求之间保存什么、向外发出什么，以及 `AgentHarness` 为什么是状态协调器而不是另一个 UI 控制器。

## 消息不是字符串列表

一个最小的聊天程序常把历史表示成 `list[str]`，顶多是 `{"role", "content"}`。可一旦模型能思考、能调用工具、能返回图片、能记录用量、还会出错，这套表示很快就撑不住：工具结果算谁的？一段回答里文本和工具调用的先后顺序怎么留住？失败该不该进可审计的历史？

Tau 在 `src/tau_agent/messages.py` 使用严格的 Pydantic `WireModel` 表示这些事实。它拒绝未知字段，同时支持 Python 的 snake_case 字段名与 Pi 兼容的 camelCase 序列化。这样消息既能当内部类型用，也能当稳定的 JSONL 会话和事件协议用。

关键在有序内容块。`AssistantMessage.content` 可按发生顺序包含：

```text
ThinkingContent → TextContent → ToolCall → TextContent
```

因此，`AssistantMessage.text` 只取可见文本，`thinking_text` 只取思考块，`tool_calls` 只取工具调用；原始顺序仍完整保留。Provider 没法把“先说明计划、再调用 read、再补充文字”压成一段模糊字符串。后续渲染、重放与审计也不必猜测内容边界。

`AgentMessage` 还区分 `UserMessage`、`AssistantMessage`、`ToolResultMessage`，以及会话特有的 bash 执行、压缩摘要、分支摘要与自定义消息。特别是 `ToolResultMessage` 显式关联 `tool_call_id`、工具名、细节和 `is_error`。模型下一轮看到的结果，能对回上次那条调用。对得上号，才叫反馈。

## Harness 把配置和可变状态放在一起

`src/tau_agent/harness.py` 中的 `AgentHarnessConfig` 描述一次 Agent 运行必须固定的依赖：

```text
provider、model、system、tools、max_turns、session_id
before_tool_call、after_tool_call
```

其中 Provider、模型、system prompt 与工具定义了“这颗大脑如何工作”；最大轮数管住任务别无穷循环；工具前后回调留给应用层施加策略。配置里没有任何 CLI 或 TUI 类型，可移植边界就划在这里。

`AgentHarness` 则保存运行中会变化的状态：消息历史、是否正在运行、当前取消信号、事件 listener，以及 steering 和 follow-up 两个队列。它公开的 `messages` 是不可变 tuple，外部不能绕过 Harness 随意改列表；需要恢复或替换历史时，必须走显式的 `append_message()` 或 `replace_messages()`。

这让 Harness 与“无状态的 `ask_model()` 函数”根本不同。它知道一次回答已向模型发送了什么，也知道下一条输入应该立即插入、稍后处理，还是因为已有运行而被拒绝。

## 一次 prompt 的生命周期

调用 `harness.prompt("解释这个模块")` 时，Harness 会把它包装成 `UserMessage`，拒绝重叠运行，标记自身为 running，然后进入私有的 `_run()`。在那里它创建 `SimpleCancellationToken`，并把配置、消息历史、取排队消息的函数与工具回调一起交给 `run_agent_loop()`。

对调用者而言，返回值是 `AsyncIterator[AgentEvent]`。用户消息、模型增量、工具执行与最终消息沿一条有顺序的流到达，调用者不必从几个 API 分别拼凑。Harness 对每个事件先通知订阅者，再 `yield` 给当前消费者：同一份事实既可以被屏幕渲染，也可以被会话持久化或诊断记录。持久化 listener 具体怎样把这条事件流落盘成 JSONL 会话树，是第 5 篇的主题；第 6 篇还会看到 `CodingSession` 如何装配它。

外层 `AgentEvent` 在 `src/tau_agent/events.py` 中分为三组：

```text
AgentStart / AgentEnd，TurnStart / TurnEnd
MessageStart / MessageUpdate / MessageEnd
ToolExecutionStart / ToolExecutionUpdate / ToolExecutionEnd
```

其中 `MessageUpdateEvent` 还嵌入 Provider 的助手增量事件。于是 Agent 消费者始终知道“哪条消息在更新”，而需要逐字渲染的 UI 仍可读取内部的 text delta、thinking delta 或 tool-call delta。Provider 的流式细节不会逃出消息生命周期。

这不是类型图上的推测。下面是从一次真实运行（FakeProvider 实跑）中截出的一条 `message_update`（usage 等重复字段已用 `...` 截断）：

```json
{"type":"message_update","message":{"role":"assistant","content":[{"type":"text","text":"Let me read that file."}],"usage":{...},"stopReason":"stop","timestamp":1789812357436},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"Let me read that file.","partial":{"role":"assistant","content":[{"type":"text","text":"Let me read that file."}],"usage":{...},"stopReason":"stop","timestamp":1789812357436}}}
```

注意 `assistantMessageEvent` 如何整个嵌在外层 `message_update` 里：外层只声明“这条 assistant 消息在变化”，内层才带着 provider 原始的 `text_delta` 和 `delta` 文本。同时 `partial` 给出截至当前的完整快照——渲染器可以只取 `delta` 追加，也可以直接用 `partial` 整体重绘，两种策略吃的是同一条事件。“两层事件”由此从类型声明变成可见的字节流。完整 22 条事件序列见 `notes/assets/run-slice/`（第 1 篇附了带注释的完整切片）。

## 并发输入不是 UI 小问题

真实用户经常在 Agent 还在跑的时候补充条件，例如“不要改公共 API”。如果 UI 直接启动第二个 `prompt()`，两次循环就会抢同一份消息列表，最后留下的历史谁也解释不清。

Harness 在运行期间拒绝新的普通 prompt，并提供两种有语义的入口：

- `steer()`：将消息排进当前任务的 steering 队列，让 loop 在合适轮次读取；
- `follow_up()`：将消息留到当前工作结束后，作为后续任务继续执行。

默认 `queue_mode="one_at_a_time"`，每次只取一条排队消息；设为 `"all"` 时才一次清空整个队列。两个队列都能查看、弹出或清空，返回的都是不可变快照。这些看似小的 API，把“何时接收人类新意图”从隐含时序变成了可测试的 Agent 行为。

`tests/test_agent_harness.py` 验证了这一点：重叠 prompt 会报错，follow-up 会在第一个回答结束后启动下一轮，`queue_mode="all"` 会一起提交已排队消息。并发控制因此不靠 Textual widget 的偶然状态，而靠核心状态机。

**反例：** 注释掉 `_ensure_not_running()`，让两个 `prompt()` 并行跑。两套 `run_agent_loop` 会交替 `append` 同一份 `self._messages`：用户消息、工具结果、助手内容的顺序会被打乱，持久化 listener 也会写下无法重放的历史。Tau 的选择是直接抛 `RuntimeError: AgentHarness is already running; use steer() or follow_up() to queue messages.` 界面可以决定怎么提示用户；内核必须先保证列表只有一个写者。

## 取消后也要让历史闭合

取消是另一个容易被忽略的边界。若模型已提出工具调用，工具却还没给出结果，直接停止运行会在消息历史里留下悬空的 `ToolCall`。下一次恢复时，Provider 不知道这次调用是成功、失败还是根本没有执行。

Tau 的 Harness 在进入 loop 前会检查历史，并为没有对应 `ToolResultMessage` 的工具调用补一条错误结果：`Tool call interrupted by user`。若是在运行中取消，它也会用开始和结束事件，把新增的修复消息通知给 listener。于是持久化订阅者与当前事件消费者看到的是同一段闭合历史。

这不是为了掩盖取消，而是为了保留正确事实：调用存在，但没有得到可用结果。测试还验证了一点：listener 在这段清理里抛错，不会盖住真正的取消信号。Agent 状态的可信度，往往取决于失败路径是否和成功路径一样被建模。

**可以自己核对：** 看 `AgentHarness._run()` 的 `finally`：取消后调用 `_append_interrupted_tool_results()`，再把修复消息 `notify` 给 listener，而不是只改内存列表。缺了这一步，JSONL 里就会留下一对不上结果的 tool call——下一轮换 Provider 或恢复会话时，请求会不合法。第 4 篇会从反馈回路一侧接住这个话题：普通异常走工具结果通道、`CancelledError` 向上传播，与这里的补写是同一件事情的两面。

## 事件契约如何让前端可替换

Harness 不渲染任何东西。它只维护 listener 列表，`subscribe()` 返回一个 unsubscribe 函数；listener 可以同步也可以异步。由于事件带判别字段、又是严格的 WireModel，print mode、JSON renderer、Textual TUI 或自定义前端都能各写各的展示策略，不必把模型循环再实现一遍。

`tests/test_pi_event_protocol.py` 锁定了两个约束：`tau_agent` 不反向 import `tau_ai`；文本流会在 Agent 事件中形成正确的嵌套更新与终止消息。前者保护依赖方向，后者保护使用体验与持久化的共同输入。事件不是 UI 的附属通知，而是所有外层能力共享的协议。

## 三分钟核对

不动代码，也能离线验证本篇的关键论断：

- `uv run pytest tests/test_pi_event_protocol.py -q`：确认 `tau_agent` 没有反向 import `tau_ai`，且文本流在 Agent 事件里形成正确的嵌套更新（就是上面那条 `message_update` 的形状）。
- `uv run pytest tests/test_agent_harness.py -q`：对照正文并发输入一节——重叠 prompt 抛错、follow-up 在上一个回答结束后启动下一轮、`queue_mode="all"` 一次提交排队消息，都由这组测试锁定。

两条命令各只需零点几秒，绿了就说明 Harness 的边界、并发语义与事件协议仍与本文描述一致。

## 小结

`AgentHarness` 不是“调用模型的便利类”，而是一层小运行时：有序消息记录事实，事件公开过程，队列、取消与修复维护状态完整性，具体推理和工具执行交给 loop。它因此能脱离 Tau 的 CLI、会话路径和 Textual 独立存在。

下一篇进入反馈回路：模型为什么会提出工具调用、工具结果怎样成为下一轮上下文，以及错误和权限边界如何影响 Agent 的行动。
