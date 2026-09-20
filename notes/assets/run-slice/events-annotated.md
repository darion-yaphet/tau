# 运行切片注释版（events-annotated）

从 `events.jsonl`（共 22 条事件）中挑出 11 条最有教学价值的事件，每条附一句注释。
完整序列由 `generate_slice.py` 用 FakeProvider + `run_agent_loop` 真实跑出，
序列化方式与 CLI 的 `JsonEventRenderer` 完全一致（Pi 兼容 camelCase JSON）。

## 第一轮：模型决定调用工具

**1. `agent_start` — loop 启动标志，所有事件流的起点。**

```json
{"type":"agent_start"}
```

**2. `message_start`（user）— 用户 prompt 作为第一条消息进入历史（随后紧跟一条内容相同的 `message_end`）。**

```json
{"type":"message_start","message":{"role":"user","content":"What does hello.txt say?","timestamp":1789812357436}}
```

**3. `message_update` / `text_delta` — 模型流式输出的文本增量，`assistantMessageEvent` 里带着原始 provider 事件与 partial 消息。**

```json
{"type":"message_update","message":{"role":"assistant","content":[{"type":"text","text":"Let me read that file."}],"api":"unknown","provider":"unknown","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"stopReason":"stop","timestamp":1789812357436},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"Let me read that file.","partial":{"role":"assistant","content":[{"type":"text","text":"Let me read that file."}],"api":"unknown","provider":"unknown","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"stopReason":"stop","timestamp":1789812357436}}}
```

**4. `message_update` / `toolcall_end` — 工具调用参数流式拼接完成，得到结构化的 `toolCall` 块（id / name / arguments）。**

```json
{"type":"message_update","message":{"role":"assistant","content":[{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],"api":"unknown","provider":"unknown","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"stopReason":"stop","timestamp":1789812357436},"assistantMessageEvent":{"type":"toolcall_end","contentIndex":1,"toolCall":{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}},"partial":{"role":"assistant","content":[{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],"api":"unknown","provider":"unknown","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"stopReason":"stop","timestamp":1789812357436}}}
```

**5. `message_end`（assistant）— 第一轮助手消息定稿：`stopReason: "toolUse"` 是 loop 继续执行工具的信号，注意 loop 还补上了 `timing`。**

```json
{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"Let me read that file."},{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],"api":"fake-messages","provider":"fake","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"timing":{"timeToFirstOutputMs":0,"totalDurationMs":0},"stopReason":"toolUse","timestamp":1789812357436}}
```

## 工具执行

**6. `tool_execution_start` — loop 按 `toolCall.id` 分发执行，UI 可借此渲染"正在调用 read"。**

```json
{"type":"tool_execution_start","toolCallId":"call_read_1","toolName":"read","args":{"path":"hello.txt"}}
```

**7. `tool_execution_end` — 工具返回结构化结果：`content` 给模型看，`details` 给 UI 看，`isError` 决定后续分支。**

```json
{"type":"tool_execution_end","toolCallId":"call_read_1","toolName":"read","result":{"content":[{"type":"text","text":"hello from tau\n"}],"details":{"path":"hello.txt","lines":1}},"isError":false}
```

**8. `message_end`（toolResult）— 工具结果落成一条 `toolResult` 角色消息写入历史，下一轮会回放进 provider 上下文。**

```json
{"type":"message_end","message":{"role":"toolResult","toolCallId":"call_read_1","toolName":"read","content":[{"type":"text","text":"hello from tau\n"}],"details":{"path":"hello.txt","lines":1},"isError":false,"timestamp":1789812357437}}
```

**9. `turn_end` — 一个 turn 闭合：助手消息 + 本轮产生的全部 `toolResults` 汇总在一起。**

```json
{"type":"turn_end","message":{"role":"assistant","content":[{"type":"text","text":"Let me read that file."},{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],"api":"fake-messages","provider":"fake","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"timing":{"timeToFirstOutputMs":0,"totalDurationMs":0},"stopReason":"toolUse","timestamp":1789812357436},"toolResults":[{"role":"toolResult","toolCallId":"call_read_1","toolName":"read","content":[{"type":"text","text":"hello from tau\n"}],"details":{"path":"hello.txt","lines":1},"isError":false,"timestamp":1789812357437}]}
```

## 第二轮：模型给出最终回答

**10. `message_update` / `text_delta` — 第二个 turn（再次 `turn_start` 之后）模型基于工具结果流式生成最终回答。**

```json
{"type":"message_update","message":{"role":"assistant","content":[{"type":"text","text":"The file says: hello from tau"}],"api":"unknown","provider":"unknown","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"stopReason":"stop","timestamp":1789812357436},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"hello from tau","partial":{"role":"assistant","content":[{"type":"text","text":"The file says: hello from tau"}],"api":"unknown","provider":"unknown","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"stopReason":"stop","timestamp":1789812357436}}}
```

**11. `agent_end` — 收尾事件：`messages` 携带本轮新增的完整历史（user → assistant+toolCall → toolResult → assistant），`stopReason: "stop"` 且无工具调用使 loop 退出。**

```json
{"type":"agent_end","messages":[{"role":"user","content":"What does hello.txt say?","timestamp":1789812357436},{"role":"assistant","content":[{"type":"text","text":"Let me read that file."},{"type":"toolCall","id":"call_read_1","name":"read","arguments":{"path":"hello.txt"}}],"api":"fake-messages","provider":"fake","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"timing":{"timeToFirstOutputMs":0,"totalDurationMs":0},"stopReason":"toolUse","timestamp":1789812357436},{"role":"toolResult","toolCallId":"call_read_1","toolName":"read","content":[{"type":"text","text":"hello from tau\n"}],"details":{"path":"hello.txt","lines":1},"isError":false,"timestamp":1789812357437},{"role":"assistant","content":[{"type":"text","text":"The file says: hello from tau"}],"api":"fake-messages","provider":"fake","model":"fake-model","usage":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0,"totalTokens":0,"cost":{"input":0.0,"output":0.0,"cacheRead":0.0,"cacheWrite":0.0,"total":0.0}},"timing":{"timeToFirstOutputMs":0,"totalDurationMs":0},"stopReason":"stop","timestamp":1789812357436}]}
```

## 未入选的事件（供对照）

- 第 2、16 行 `turn_start`：turn 边界标记，无 payload。
- 第 4、13、17 行 `message_start`：与对应 `message_end` 成对出现，内容相同。
- 第 5 行 `message_start`（assistant 空 partial）：`AssistantStartEvent` 映射而来，content 为空数组。
- 第 7、8 行 `toolcall_start` / `toolcall_delta`：工具调用参数的流式中间态，教学上由 `toolcall_end` 代表。
- 第 19、20、21 行：第二 turn 的 `message_end` / `turn_end`，结构与第一 turn 对称。
