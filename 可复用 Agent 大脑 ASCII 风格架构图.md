# 可复用 Agent 大脑 ASCII 风格架构图

> 本文档整理自 Tau (`tau_agent`) 与 Pi 极简 Agent 架构中关于「可复用 Agent 大脑」（[`AgentHarness`](file:///Users/darion.yaphet/tau/src/tau_agent/harness.py#L62)）的设计规范与运行时结构。

---

## 一、整体架构图 (ASCII)

```text
+---------------------------------------------------------------------------------------+
|                       宿主层 / 表现与环境 (Host & Environment)                         |
|   [ CLI Print 模式 ]     [ Textual TUI ]     [ RPC Server ]     [ AgentSession 封装 ] |
+---------------------------------------------------------------------------------------+
         |                             |                          |
         | prompt() / continue_()      | steer() / follow_up()    | cancel()
         v                             v                          v
+=======================================================================================+
|                   可复用的 Agent 大脑: AgentHarness (tau_agent)                       |
|                                                                                       |
|  +-----------------------------+               +-----------------------------------+  |
|  |     AgentHarnessConfig      |               |       运行时与控制 (Control)      |  |
|  | - provider : ModelProvider  |               | - _running: bool (执行互斥)       |  |
|  | - model    : 模型名称       |               | - SimpleCancellationToken (中断)  |  |
|  | - system   : 系统提示词     |               +-----------------------------------+  |
|  | - tools    : list[AgentTool]|                                                      |
|  | - max_turns / hooks         |               +-----------------------------------+  |
|  +-----------------------------+               |       动态干预队列 (Queues)       |  |
|                                                | - _steering_queue  (中途改道/插话)|  |
|  +------------------------------------------+  | - _follow_up_queue (轮后追问排队) |  |
|  |         会话状态机 (Transcript)          |  +-----------------------------------+  |
|  | - _messages: list[AgentMessage]          |                                         |
|  | - messages: 只读元组快照                 |  +-----------------------------------+  |
|  | - _append_interrupted_tool_results()     |  |       旁路监听 (Pub/Sub)          |  |
|  |   (悬挂工具调用自愈 / Dangling Repair)   |  | - _listeners (持久化/度量/审计)   |  |
|  +------------------------------------------+  +-----------------------------------+  |
|                        |                                                              |
|                        | 驱动装配 (Prompts, Transcript, Queues, Signal)               |
|                        v                                                              |
|  +---------------------------------------------------------------------------------+  |
|  |                     无状态核心引擎: run_agent_loop(...)                         |  |
|  |                                                                                 |  |
|  |   [Turn Loop] ---> [模型生成] ---> [Tool Calls 解析] ---> [并发/异步执行工具]   |  |
|  |        ^                                                          |             |  |
|  |        +------------------ 结果写回 / 轮次推进 <-------------------+             |  |
|  +---------------------------------------------------------------------------------+  |
|             |                                                                         |
|             | yield (事件流)                                                          |
+=============|=========================================================================+
              |
              | 产出 AgentEvent 流 (MessageStart, ToolStart, MessageUpdate, AgentEnd...)
              v
+------------------------------------+          +---------------------------------------+
|          主链路消费者              |          |              旁路观察者               |
|  (UI 实时逐字渲染 / 终端打印)      |          |  (JsonlSessionStorage 自动存盘/审计)  |
+------------------------------------+          +---------------------------------------+
              |                                                     |
              +--------------------------+--------------------------+
                                         |
                   下层技术依赖 (独立解耦，可按需替换)
                                         v
        +----------------------------------+   +----------------------------------+
        |      tau_ai / ModelProvider      |   |            AgentTool             |
        |   (Anthropic / OpenAI / 本地模型)|   |  (read_file, bash, edit, ...)    |
        +----------------------------------+   +----------------------------------+
```

---

## 二、架构分层与职责边界

Tau 严格延续了 Pi 的三层分离原则：

```text
AgentHarness = reusable agent brain       (可复用的 Agent 大脑)
AgentSession = coding-agent environment   (编码环境/会话上下文与业务包装)
TUI / CLI / RPC = frontends               (前端表现层)
```

### 1. 北向接口（宿主层与控制输入）
- **[`AgentHarness.prompt()`](file:///Users/darion.yaphet/tau/src/tau_agent/harness.py#L152)**：追加用户消息并启动交互循环。
- **[`AgentHarness.continue_()`](file:///Users/darion.yaphet/tau/src/tau_agent/harness.py#L155)**：不追加新用户输入，直接继续推进未决的思考/工具轮次（用于恢复会话或打断后重试）。
- **`steer()` / `steer_message()`**：高优先级轮内干预。在 Agent 连续工具调用的步骤之间插入用户纠偏指令，使其立即调整执行方向。
- **`follow_up()` / `follow_up_message()`**：低优先级轮后排队。在当前完整的执行回合完成后顺序消费后续输入。
- **`cancel()`**：协同式打断，通过 [`SimpleCancellationToken`](file:///Users/darion.yaphet/tau/src/tau_agent/harness.py#L51) 通知底层流式生成和工具执行优雅终止。

### 2. 核心状态机与运行时容器（[`AgentHarness`](file:///Users/darion.yaphet/tau/src/tau_agent/harness.py#L62)）
- **静态配置隔离**：通过 [`AgentHarnessConfig`](file:///Users/darion.yaphet/tau/src/tau_agent/harness.py#L39) 装配 Provider、模型、系统提示词与工具集，内部不硬编码任何具体业务偏好。
- **状态所有权与快照安全**：内部维护 `_messages` 列表，外部通过 `harness.messages` 读取只读元组（`tuple`）快照，杜绝外部对上下文链的意外污染。
- **悬挂工具自愈（Dangling Tool Repair）**：通过 `_append_interrupted_tool_results()` 自动检测因取消或异常未获得返回的工具调用，注入合成错误结果，保证大模型上下文消息协议的合法性。
- **双通道事件分发**：
  - **主通道**：以 `AsyncIterator[AgentEvent]` 形式向调用者实时 `yield` 事件。
  - **旁路通道**：支持 `subscribe(listener)` 注册观察者，用于外部进行日志记录、指标采集或持久化存盘（如 [`JsonlSessionStorage`](file:///Users/darion.yaphet/tau/src/tau_agent/session.py#L112)）。

### 3. 底层循环与外部依赖
- **无状态核心引擎**：[`run_agent_loop`](file:///Users/darion.yaphet/tau/src/tau_agent/loop.py#L110) 只负责纯函数式的轮次运转，自身不持有状态，完全由外层 Harness 注入上下文并接收产出。
- **可插拔依赖**：仅依赖抽象协议 [`ModelProvider`](file:///Users/darion.yaphet/tau/src/tau_agent/provider.py#L12) 与 [`AgentTool`](file:///Users/darion.yaphet/tau/src/tau_agent/tools.py#L32)，在单测时可无缝注入 Fake 实现，获得极高的确定性验证能力。
