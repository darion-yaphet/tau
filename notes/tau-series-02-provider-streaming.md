# 第二篇：模型不是 Agent——tau_ai 如何统一不可靠的外部智能

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

第一篇铺了运行路径：用户请求经过 `CodingSession` 和 `AgentHarness`，进入 Agent loop。现在把镜头推到最容易被低估、也最容易把差异渗进内核的一层：模型 Provider。

不同服务的 API、认证、流格式和工具 ID 约束各不相同，也可能在流开始之后才报错。若这些差异渗进 Agent 核心，工具循环很快会写满 `if provider == ...`。

Tau 把模型服务当成不可靠、各不相同的外部智能。`tau_ai` 负责适配，`tau_agent` 只依赖稳定协议；loop 只需处理收到的助手事件。

## 先看边界：协议属于核心，适配属于 tau_ai

当前源码中，`ModelProvider` 协议定义在 `src/tau_agent/provider.py`，而 `src/tau_ai/provider.py` 只把它重新导出。Provider 是 Agent 核心依赖的抽象，不是某家模型 SDK 的抽象。SDK 会改名、会改协议；核心抽象不能跟着改。

协议的核心只有一个异步方法：

```python
def stream_response(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: CancellationToken | None = None,
    session_id: str | None = None,
) -> AsyncIterator[AssistantMessageEvent]:
```

一次请求需要的输入列得很清楚：模型名、系统提示词、历史消息和可调用工具；运行时控制单列：取消信号与可选会话标识。返回值不是 HTTP 响应，也不是最终文本，而是 `AssistantMessageEvent` 的异步流。

协议不规定 URL、OAuth、SSE JSON 或供应商名称；那些属于 `tau_ai`。它只要求 Agent 获得可逐步消费的助手响应，因此 `run_agent_loop()` 可替换 Provider，测试也可替换网络。

## 为什么“流”必须成为一等公民

对用户而言，流式输出首先意味着终端能立刻显示文字。对 Agent 架构而言，它还有更深的作用：模型回答不是一块不可分的最终文本，而是一条逐渐成形的消息。成形的过程要能看见，失败的方式也要能看见。

Tau 的公共事件在 `src/tau_agent/provider_events.py` 定义，并由 `tau_ai.events` 导出。它们覆盖了以下生命周期：

```text
AssistantStartEvent
  ├─ TextStartEvent → TextDeltaEvent* → TextEndEvent
  ├─ ThinkingStartEvent → ThinkingDeltaEvent* → ThinkingEndEvent
  └─ ToolCallStartEvent → ToolCallDeltaEvent* → ToolCallEndEvent
AssistantDoneEvent | AssistantErrorEvent
```

增量事件携带当前 `partial` 助手消息快照和内容索引，消费者能更新正确的内容块。最终的 `AssistantDoneEvent` 带完整 `AssistantMessage`；错误以 `AssistantErrorEvent` 表达。上层先渲染增量，再由终止事件确定成功、长度限制、工具调用或错误。

工具调用、思考块、使用量、停止原因和诊断都是同一轮消息的一部分。只返回文本会破坏循环与跨 Provider 的会话重放：漂亮句子还在，工具调用丢了，下一轮就对不上号。

## 归一化：把服务方言翻译成统一形状

真实适配器通常先产生一个私有的过渡事件流，再由 `src/tau_ai/stream.py` 的 `canonicalize_provider_stream()` 翻译为公共助手事件。它维护一个正在增长的 `AssistantMessage`，并处理几个容易被忽略的细节：

- 文本与思考块切换时，先结束前一个活动块，保证内容顺序可重放；
- 工具调用成为独立内容块，并对应开始与结束事件；
- Provider 的终止原因被归一化为 `stop`、`length` 或 `toolUse`；
- 解析收尾时保留流中实际出现的内容顺序，再从最终消息补齐用量和其他元数据；
- 若流没有产生终止事件，主动产出可诊断的错误，而不是假装回答完成。

这一层由此建立起跨 Provider 的时间语义：消息开始、若干内容变化，以及唯一的完成或错误结局。Agent loop、TUI、JSON renderer 和会话系统都能依赖这个顺序。

注意重试的边界。私有流中可以出现 `ProviderRetryEvent`，但 canonicalizer 不把它暴露成公共助手内容：代码里就是一条 `continue`。重试属于网络与服务稳定性的实现细节，Provider 内部仍然按自己的 HTTP 策略干活。只有最终无法恢复的情况才成为 `AssistantErrorEvent`。

**反例：** 把某次 HTTP 429 重试写进对话历史。下一轮模型会看到“刚才失败过一次又好了”，像一条用户消息或助手内容。那不是任务记忆，是传输层噪声写进了对话。Tau 选择把重试留在适配器内部；可以对照 `canonicalize_provider_stream()` 对 `ProviderRetryEvent` 的跳过，以及 `tests/test_tau_ai.py` 里对 SSE 归一化的断言。

另一个反例：流在中途断开，却当成功结束。没有 `AssistantDoneEvent` 就补一段空文本往下走，工具循环会以为本轮已经完成。canonicalizer 会产出可诊断的错误，让上层知道该停、该重试，还是该告诉用户。这样的错误一旦真的发生，影响不会停在适配器里：第七篇「一次失败如何让三层都现形」复盘过一次真实失败如何沿 Provider、Harness 和界面逐层浮现。

## 适配器不是一套请求模板

`tau_ai` 当前公开 Anthropic、Google Generative AI、Mistral、OpenAI Codex、OpenAI-compatible 与 Fake Provider。它们共享协议，但不假装底层 API 相同。

以 `src/tau_ai/openai_compatible.py` 为例，同一个适配器能处理传统 `/chat/completions` 和 `/responses` 两种 API。它依据显式配置或模型能力推断来选路径；某些推理模型与 Codex 模型在带函数工具和 reasoning effort 时需要走 Responses API。对 Agent loop 而言，这个选择不可见：它始终只得到相同的助手事件流。

模型别名、session 亲和性、图片能力、请求头、认证、超时与 HTTP 重试也都留在适配器，不渗入 `AgentHarness`。

因此，“Provider-neutral”并不等于抹平所有能力差异。Tau 保留模型、API、response provider、thinking signature 等元数据；适配器只把差异放在边界内。核心获得的是可预测的形状，产品层仍能在需要时看到真实的诊断信息。

## 跨 Provider 的难题在历史，不只在本次响应

一次请求的格式转换并不难，难的是会话继续。今天用某个 OpenAI-compatible 模型完成了两次工具调用，明天改用 Anthropic，历史里可能带着前一个服务专属的思考签名和工具 ID。若原样转发，新 Provider 可能把整段历史拒之门外。

Tau 的测试 `tests/test_cross_provider_history.py` 锁定了这种边界。例如，某些原生工具 ID 含有目标 Provider 不接受的字符时，`portable_tool_call_id()` 会产生稳定且安全的 ID；工具结果会引用同一个转换后的 ID。至于只有源 Provider 能验证的思考签名，则不会被当成另一家 Provider 的有效思考内容重放。原则是：**保留任务所需信息，但不伪造另一个服务的私有状态。**

这也是消息模型比“聊天文本数组”更强的地方。只有在历史里区分出用户消息、助手内容、工具调用、工具结果、思考块与诊断，Provider 才能按自身能力编译出合法请求，不必依赖脆弱的字符串清洗。而这份历史本身如何被逐条落盘成 JSONL entry、又如何从会话树重建出可重放的当前上下文，是第五篇的主题。

## Fake Provider 是架构的压力测试

如果一套 Provider 抽象只在接口图上漂亮，却没法测试，那它多半还耦合着某个 SDK。Tau 的 `src/tau_ai/fake.py` 接收预定义助手事件流，每次调用回放下一段，并记录请求参数；取消时停止产出事件。

`tests/test_tau_ai.py` 验证 Fake Provider 回放和 SSE 规约；`tests/test_pi_event_protocol.py` 验证 `tau_agent` 不反向导入 `tau_ai`，并检查 AgentHarness 的文本与工具事件序列。

Fake Provider 的价值不只是“方便 mock”。它迫使核心只依赖公开协议；如果测试必须启动真实服务才能驱动 Agent loop，就说明边界已经泄漏。真模型会消耗钱包；假模型消耗的是您对边界的诚实。

## 三分钟核对

不碰真实模型，也能亲手核对本篇的关键断言：

- `uv run pytest tests/test_tau_ai.py -q` — 看 SSE 归一化与 Fake Provider 回放的断言是否全绿，尤其是没有终止事件时是否产出错误。
- `uv run pytest tests/test_cross_provider_history.py tests/test_pi_event_protocol.py -q` — 看跨 Provider 工具 ID 转换的边界用例，以及 `tau_agent` 不反向导入 `tau_ai` 的架构约束。
- `grep -n "ProviderRetryEvent" src/tau_ai/stream.py` — 看 canonicalizer 里那条 `continue`：重试事件确实被留在适配器内部，没有变成公共助手事件。

## 小结

读 Provider 代码时，不必先逐个理解 SSE parser。先确认三件事：输入是否完整地通过协议进入适配器？底层流是否被转换成规范的开始、增量与终止事件？跨 Provider 重放历史时，哪些信息可保留、哪些必须降级？

Tau 的答案是把复杂性压到边界：`tau_ai` 接受不同服务的方言，`tau_agent` 只处理统一的助手消息和事件。模型负责生成下一步；Provider 负责把它可靠地传达出去；至于决定怎么行动的 Agent loop，下一篇从 AgentHarness 和消息契约展开。
