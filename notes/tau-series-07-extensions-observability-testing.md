# 第七篇：让 Agent 可演进——扩展、可观测性与测试方法

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

一个编码 Agent 迟早得加本事：接入新 Provider、增加专用工具、加入项目工作流、显示额外状态，或者把结果交给另一个前端。最直接的做法是把需求写进 `CodingSession` 或 Agent loop；几回下来，原本清晰的边界就被产品特例填满了，升级和测试也跟着难做。

Tau 眼下选的是另一条路：核心继续只管消息、工具与事件；`ExtensionRuntime` 在产品层组合可选能力；所有关键过程都拿事件和诊断摊开来；测试用假的模型与工具锁定行为。这里的“可扩展”指的不是让任意代码到处乱插，而是给变化留出明确的接缝和生命周期。

## 扩展要接在边界上，而不是改进 loop

`src/tau_coding/extensions/runtime.py` 中的 `ExtensionRuntime` 挂在一个已经准备就绪的 session 快照上。它能加载受信任的内置扩展和发现到的扩展源，然后向运行时注册工具、动态 Provider、本地后端、slash command、prompt guideline/section 和自定义消息 renderer。

扩展工具通过 `compose_tools()` 合并：同名扩展工具在原位置覆盖内置工具，扩展独有工具按注册顺序追加。合并后的每个工具都会被 Runtime 包装，因此所有工具——不论内置还是扩展提供——都经过相同的 hook seam。核心 Agent loop 看见的仍是一组普通 `AgentTool`，不需要知道工具来自哪个扩展。

这个模式比“让扩展改 Agent loop”稳定得多。新 Provider 进入 Provider registry，新命令进入命令注册表，新 prompt 内容在 system prompt 组装时追加；它们各自改变所属边界，而不改变 `run_agent_loop()` 的消息与工具反馈规则。

**反例：** 扩展 monkey-patch `run_agent_loop`，在里面加一条“遇到某模型就换重试策略”。短期能跑，但 Fake Provider 测试、print mode 和 TUI 会各自看见不同的循环语义；下一次改 loop 的人也不知道那条补丁还在。Tau 把重试留在 `tau_ai`，把策略留在 hook。`tests/test_pi_event_protocol.py` 连 `tau_agent` 反向 import `tau_ai` 都不许，就是为了让这种补丁没有合法落点。

## Hook 让策略可插入且可追踪

扩展 API 目前能订阅 input、tool call、tool result、session start/shutdown、project trust 和各类 Agent 事件。input hook 可以放行、改写，或者干脆自己处理掉用户输入；tool-call hook 可以阻止调用或替换参数；tool-result hook 可以转换返回内容与 details。

例如，扩展可以在 bash 执行前阻止某些危险模式，或者为所有输入补充项目工作流。但这类规则是扩展策略，不是 Agent 核心默认行为。测试中的 `permission_gate` 示例正是这一接缝的演示：它包装 bash 并阻止选定模式，同时不影响其他工具。把策略放在 hook 里，用户才知道它从哪儿来、能不能卸载，也才不会悄悄改掉别的 session。

Runtime 还隔离扩展 handler 的失败。hook 或事件 handler 抛出普通异常时，Runtime 记录 diagnostic 并继续调度其他能力；自定义 renderer 报错或返回错误类型时，前端回退到原始内容。扩展不该因为展示逻辑出错，就把整个 TUI 或工具循环带崩。

不过扩展也不是安全沙箱：它们是会被加载、被执行的 Python 代码。当前 session 加载过程先解析 project trust，再决定是否开放项目资源与项目扩展；所以扩展的来源、信任与生命周期，必须在产品层明着管。把项目目录里的 Python 当礼物拆开，要先看这份代码会不会在您的机器上跑起来。

## Generation 与资源所有权

扩展运行时不该跨项目、跨重载地无限活下去。`ExtensionRuntime` 带有 generation；重新加载或替换目标 session 时，Tau 准备新 Runtime，成功后再 retire 旧 generation。retire 会解掉 Harness listener、清理注册表、让旧 context 失效，并要求这一代所属的 Provider 与本地后台停止，或者进入受控关闭。

这是一种比“全局单例插件表”更可靠的资源模型。旧项目的扩展要是还攥着工具、Provider 或异步发现任务，就不许在新项目 session 里继续注册，也不许把迟到的结果写回新界面。source identity 与 generation 看似是内部细节，实际保护的是重载、切换目录与取消后的所有权边界。

**反例：** 用模块级全局 list 登记扩展工具，换项目时只 `load()` 新扩展、不 retire 旧的。上一份工作的 bash hook 仍可能拦住当前仓库的命令，迟到的异步任务也可能往新 TUI 里塞事件。`ExtensionRuntime.retire()` 要做的，就是让旧 generation 不能再影响新 session。`tests/test_extensions.py` 覆盖发现、重复注册和 hook 包装，读的时候把“换 session 之后旧 handler 还在不在”当作一条必须成立的不变量。

## 可观测性：事件是共享事实，不是日志替代品

上一层的 `AgentEvent` 描述模型、消息、轮次和工具执行。`src/tau_coding/events.py` 在此之上增加产品事件：排队消息更新、压缩开始/结束、entry 追加、会话信息或 thinking level 变化、自动重试，以及最终的 `AgentSettledEvent`。类型联合 `CodingSessionEvent` 因而成为 UI、CLI、RPC 与扩展共同消费的完整事件面。

这带来三个好处。首先，TUI 可以逐步更新 transcript，print renderer 可以输出文本或 JSON，RPC 可以为一个请求输出关联的 response 和事件，谁都不必把 Agent 行为再复制一遍。其次，扩展可以订阅相同事件观察 turn 与工具结果，不必轮询私有状态。最后，失败、压缩和自动重试也能被用户看见，不会只存在于内部控制流。

事件并不能替代持久诊断。`AgentCallDiagnosticLogger` 会把意外异常、终止 assistant 错误和 Hugging Face route failover 写入结构化 JSONL。每条记录包含 run ID、session ID、Provider、模型、cwd 与阶段；Provider 错误只提取状态码、尝试次数和安全的标量分类字段，不写请求或凭证材料。这样，用户看事件就能明白当前这一轮在干什么，维护者看诊断就能定位失败，敏感内容也不会被随手抄进故障日志。钱包被模型消耗是一回事；API Key 被写进日志，是另一类事故。

## 一次失败如何让三层都现形

事件和诊断的价值，出事故那天才算得清。有一次生产 Kimi 会话重试耗尽于 HTTP 429；之后用户每发一条消息都必败于 HTTP 400，界面却像静默停住。复盘拆开看，是三个 bug 接力：失败被持久化成一条无内容的 assistant 消息；下次请求构造上下文时把这条空消息回放给 Provider，被对方以 400 拒绝；TUI 的增量渲染又把空失败消息 finalize 成空 widget，错误条目根本没挂上去。持久化、上下文构造、渲染投影，三层各错一点，合起来就是“每发必败，且看不见”。

修复没有去存储层删历史，而是在边界处过滤。`src/tau_agent/loop.py:185` 的 `_provider_context()` 只在构造 Provider 输入时剔除空的 error/aborted 消息，原始消息仍留给会话、分支与诊断；显示侧 `src/tau_coding/tui/state.py:643` 把 stop_reason 为 error/aborted 的消息投影为“部分文本 + 错误块”，`app.py:5407` 在终态事件时一次性重建 transcript，让 live 渲染与恢复渲染长得一样。取舍也很克制：只过滤“空”失败消息，带部分内容的一律不动，免得悄悄丢掉部分响应；渲染修复走一次性重建，而不是去动高频增量路径。防回归落在 `tests/test_agent_loop.py:341` 与 `tests/test_tui_adapter.py:736`、`:751`。

这件事收束成一条论点：持久化历史不等于 Provider 上下文，它们是同一份事实的两个投影。失败消息是会话事实，但不是模型上下文——混为一谈，一次错误就毒化后续所有请求。这与本篇“事件是共享事实”是同一件事的两面，也接回第 3 篇的历史闭合：历史必须闭合，但闭合的历史不必原样塞回模型。

## 如何测试 Agent，而不只测试函数

Agent 测试的敌人是非确定性：真实模型会改变措辞，网络会超时，工具会读到不断变化的工作目录。Tau 用 `FakeProvider` 回放预先定义的助手事件流，用 Fake Tool 返回固定结果。这样测试就能断言真正要紧的序列：用户消息是否进入历史、工具结果是否回到第二次 Provider 请求、取消是否补齐悬空调用、事件是否按开始/更新/结束出现。

测试应按边界分层：

- `tests/test_tau_ai.py`、`test_agent_loop.py` 和 `test_agent_harness.py` 锁定 Provider、循环与状态机契约；
- `tests/test_coding_tools.py` 验证本地工具的输入、失败与取消语义；
- `tests/test_session.py`、`test_context_window.py` 验证可重放历史与压缩；
- `tests/test_extensions.py` 和 `test_example_extensions.py` 通过真实 Runtime 加载扩展示例，检查发现、重复注册、hook 包装和作者可复制的使用方式；
- `tests/test_rpc.py` 验证另一个前端协议可获得关联 response 和同一事件流（RPC 前端互换的设计留待第八篇展开：从能跑到可信——trust、本地推理、成本与 RPC）。

这种组合一边保住单元级的确定性，一边验证跨层不变量。比如 Extension 测试不必调用真实模型，却能确认注册的工具最终真的经过 Runtime 包装；RPC 测试不必启动 TUI，却能确认事件协议能被另一个进程消费。

**反例：** 断言最终回答等于某段固定文案。真模型换个措辞，测试就红；更糟的是，文案对了也掩盖悬空 tool call、事件顺序错误、扩展 handler 把 loop 带崩。Tau 选择用不变量定义质量：每个 tool call 都有能对上的结果或一条显式中断；事件顺序可被前端消费；失败能诊断且不泄露凭证；session 恢复后仍是同一活跃分支；扩展失败不破坏核心运行；替换 Runtime 后旧 generation 不能继续影响新 session。

## 三分钟核对

- `uv run pytest tests/test_agent_loop.py -q -k empty_failed`：空失败消息留在 harness 历史里，但不进入下一次 Provider 请求。
- `uv run pytest tests/test_tui_adapter.py -q -k "assistant_error or restores_partial"`：失败消息在显示侧投影为“部分文本 + 错误块”，恢复会话后错误仍可见。
- `uv run pytest tests/test_extensions.py -q`：扩展发现、重复注册与 hook 包装的不变量仍成立。

## 用不变量而不是快照定义质量

这类不变量也解释了 Tau 的设计取舍。它宁愿多定义几种 typed event、entry 和 lifecycle，也不把正确性押在 TUI 状态、全局变量，或者某次模型恰好输出的文字上。可观测性和测试不是核心做完之后才补的辅助功能，而是让 Agent 在复杂环境里还能接着长的那副骨架。

## 小结

回看这一系列，Tau 的分工小而清晰：Provider 将外部模型归一，Harness 与 loop 维护工具反馈，CodingSession 装配编码环境，事件将过程交给前端与持久化，扩展通过受控接缝增加能力，测试保护跨层不变量。

如果您要在自己的项目中借鉴 Tau，优先复用这些边界，而不是先复制 UI 或某个 Provider adapter。先让一次请求的状态、工具结果和事件序列可信；要往上长的时候，再把变化放到明确的适配层与 extension seam 中。这比一上来就堆智能功能，更能让系统长期读得懂、改得动。

第 0 篇那张三层图，到这里可以收回来看一遍：`tau_ai` 管不可靠的外部智能，`tau_agent` 管可移植的循环与状态，`tau_coding` 管真实世界里的项目、信任和脸。脸可以换；循环和历史必须对得上号。

到这里，前七篇讲的都还是一次会话里的事。第八篇将离开单次请求，看看 0.4.x 如何把产品推向长期可依赖：project trust 怎么圈住项目资源，本地推理怎么给模型多一条路，成本和 RPC 又凭什么被摆上桌面。
