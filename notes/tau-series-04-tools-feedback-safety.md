# 第四篇：让模型行动——工具循环、反馈与安全边界

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

模型能提出“读取文件”或“运行测试”，却不会自己触碰仓库。工具契约规定：模型只能提出名称与参数，Agent 运行工具，再把结构化结果送回模型。

Tau 的 `tau_agent` 定义通用工具协议，`tau_coding` 提供 `read`、`write`、`edit` 与 `bash`。每次调用都进入“请求 → 执行 → 结果 → 再推理”的闭环；输出是下一轮判断的证据。没有真实反馈，模型就会继续按自己的想象往下走。

## 工具是一份能力契约

`src/tau_agent/tools.py` 中的 `AgentTool` 是一个冻结 dataclass。它把一个工具描述为：

```text
name、label、description、parameters、execute_fn
prompt metadata、arguments preparer、execution mode、renderers
```

其中 `parameters` 是交给 Provider 的 JSON Schema；`execute_fn` 是接收 tool-call ID、参数、取消信号和可选进度回调的异步执行器。模型拿不到文件对象，也拿不到 shell；它只能生成符合 schema 的调用。执行器也不直接返回字符串，而是返回 `AgentToolResult`，其中有面向模型的 `content`、可供 UI 或日志使用的 `details`，以及可选的动态工具名和终止标记。

同一工具定义可服务三类消费者：Provider 用 schema 构造请求，loop 用执行器获得事实，前端用 label、renderer 和 prompt metadata 呈现过程。

`AgentTool` 还声明了 `execution_mode`，但读代码时别过度推断：字段默认值是 `"parallel"`，而 `run_agent_loop()` 目前按 assistant message 中的调用顺序逐个执行工具。这个字段说的是工具的能力意图，不代表当前 loop 已经把同一轮的调用并行了。

## 默认工具集如何接触项目

`create_coding_tools()` 在 `src/tau_coding/tools.py` 返回固定顺序的四个工具：

```text
read → write → edit → bash
```

它们共享调用时指定的 `cwd`，相对路径按它解析。这个约定让模型能在项目工作目录里自然地引用 `src/app.py`，也让 `bash` 在同一目录启动子进程。但它不是文件安全沙箱：read、write 和 edit 的文档都允许绝对路径。因此，`cwd` 是执行上下文，别把它误当成授权边界。

`read` 读取 UTF-8 文本，也能靠内容而不是扩展名认出它支持的图片。大文本会按行数或字节数截断，并附带继续读取的 offset 提示；图片会先过一遍大小、格式和模型视觉能力的检查，再变成 provider-neutral image block。这样模型既不会因为一次读取就吞掉整个上下文，也不会明明不支持图片，还凭空描述图片内容。

`write` 会先创建父目录，再用 UTF-8 写入或覆盖目标文件。`edit` 则是精确替换工具：每个 `oldText` 必须非空、在原始文件中唯一、与其他编辑不重叠；所有替换先验证，任一失败时文件保持不变。成功后结果会带上 diff、unified patch 与首个变更行。对于已有文件的小改动，这比让模型生成整份文件更容易验证和审阅。

`bash` 在 `cwd` 中执行任意 shell 命令，合并 stdout 与 stderr，并返回退出码、耗时、截断元数据和完整输出文件路径。可选 timeout 超时时，POSIX 平台会终止整个新进程组，免得管道或子进程留在后头；取消信号也走同一类清理路径。它是四个里能力最强的一个，也最需要授权策略裹着。

**反例：** 把 `cwd` 当成“只能碰这个目录”。给 `read` 一个仓库外的绝对路径，工具仍会去读——schema 和 `cwd` 都不拦这个。授权必须写在 `before_tool_call` 或产品层策略里，不能靠工作目录的心理安慰。`tests/test_coding_tools.py` 覆盖的是截断、图片降级、edit 原子失败、bash timeout，不是路径沙箱；读测试名单时别把没测的能力脑补进去。

## 一次工具调用怎样进入反馈回路

Provider 把模型的工具调用收敛为 `ToolCall(id, name, arguments)` 后，Agent loop 先把完整的 assistant message 放入历史。随后它逐个处理其中的调用：

```text
ToolExecutionStartEvent
  → before_tool_call（可阻止）
  → AgentTool.execute(...)
  → ToolExecutionUpdateEvent*（可选进度）
  → after_tool_call（可转换结果）
  → ToolExecutionEndEvent
  → ToolResultMessage 的开始与结束事件
```

无论 UI 是否展示了工具输出，loop 都会创建带相同 `tool_call_id` 的 `ToolResultMessage`，追加到 messages，再请求模型下一轮响应。模型实际看到的是“read 调用返回了哪些内容”或“bash 退出码为多少”，不是 Agent 在后台静默做过什么。

假设模型要求读取 `README.md`。read 的结果进入历史后，下一次 Provider 调用会收到用户请求、包含 `ToolCall` 的助手消息，以及对应的 `ToolResultMessage`。模型于是可以引用文件内容继续回答，或者发现入口还不够明确，转而调用另一个工具。这个闭环把模型的计划变成可以被环境纠正的行动，而不是一次性生成代码。

工具也能产生进度。执行器通过 `on_update` 交出部分 `AgentToolResult`，loop 把它们转换为 `ToolExecutionUpdateEvent`。耗时操作靠这个才有可观察性；而最终结果仍是唯一会写入消息历史的 `ToolResultMessage`，免得把临时进度当成永久事实。

## 失败不是异常出口，而是模型输入

工具边界最容易写成“异常就崩”。崩了清净，也彻底没了下一轮。Tau 则把可预期的失败变成结构化错误结果。当前 `_execute_tool_call()` 有几条明确路径：

- `before_tool_call` 阻止调用，返回被阻止原因；
- 取消信号已经触发，返回 `Operation aborted`；
- 工具表中没有这个名称，返回 `Tool <name> not found`；
- 执行器抛出普通异常，`_run_tool()` 捕获并将异常文本转为错误结果。

这些路径都会产生 `ToolExecutionEndEvent`，并形成 `is_error=True` 的 `ToolResultMessage`。模型因此有机会换一种工具、修正参数、向用户解释阻塞原因，或停止任务。只有 Python 的 `CancelledError` 会继续向上传播，因为真正的取消不能被伪装成普通工具失败。用户按了 `Ctrl+C`，那不是建议。

**反例：** 让 `read` 在文件不存在时直接 `raise`，并且不捕获。Agent loop 会中断，TUI 只看到一次崩溃，模型没有机会改路径或换 `bash ls`。Tau 把这类失败写成 `is_error=True` 的结果送回上下文：`tests/test_agent_loop.py` 验证未知工具也会成为规范的错误结果，并进入第二次 Provider 调用。可以对照：普通异常走结果通道，`CancelledError` 继续向上，两条路不能并成一条。

错误能否回到上下文，决定了 Agent 是在失败后调整，还是只会无声中断。

## 安全边界必须在能力之外明确设置

工具 schema 会约束参数形状，`edit` 会避免模糊替换，`bash` 能取消、能超时；这些都管执行可靠，不管授权。`write` 可以覆盖文件，`bash` 可以运行命令，绝对路径也照样能传进去。仅靠“模型通常会谨慎”不是安全模型。

Tau 将执行前后的策略钩子放进 `AgentHarnessConfig`：`before_tool_call` 可以返回“是否阻止”及原因，`after_tool_call` 可以检查或转换结果。核心 loop 只落实这个决定，并把原因反馈给模型。这样产品层就能加入用户确认、只读模式、路径规则、审计或项目特定权限，不至于污染通用工具协议。协议管能力，钩子管该不该。把该不该写进工具本体，工具就变成道德委员，还不好卸载。

于是安全责任可以分成三层：工具实现负责输入校验和资源清理；Harness 负责提供可拦截的执行点与完整事件；应用层负责按用户与项目策略决定放行哪些副作用。缺任何一层，都会把“能执行”错误地当成“应执行”。

这条边界在第六篇会落到产品层：trust 决定哪些项目资源和扩展允许进入运行环境，审错了锅在产品层而不是 Harness。trust 政策的完整展开——以及它和本地推理、成本、RPC 怎么一起回答“凭什么还信它”——在第八篇《从能跑到可信——trust、本地推理、成本与 RPC》。

## 测试锁定的不是文本，而是行为

`tests/test_coding_tools.py` 验证默认工具集、read 的截断和图片降级、write 的目录创建、edit 的多替换与原子失败、bash 的 timeout 与子进程清理。`tests/test_agent_loop.py` 则验证工具结果被追加到历史并进入第二次 Provider 调用，未知工具也成为规范的错误结果。

这些测试说明了工具层的承诺：输出可能不同，错误却不能丢；执行可能终止，历史却不能断；具体工具可以替换，Agent loop 对结果的处理方式必须保持一致。

## 三分钟核对

不动代码，也能离线验证本篇的两个承诺：

- `uv run pytest tests/test_coding_tools.py -q`：28 个测试全绿，对应正文说的 read 截断与图片降级、write 创建父目录、edit 多替换与原子失败、bash timeout 与子进程清理——注意名单里没有路径沙箱，别脑补。
- `uv run pytest tests/test_agent_loop.py -q`：12 个测试全绿，验证工具结果会追加到历史并进入第二次 Provider 调用，未知工具也成为 `is_error=True` 的规范错误结果，而不是一次崩溃。

## 小结

工具让模型有了行动，却没把控制权交给它。schema 限定调用形状，异步执行器接触环境，事件公开过程，`ToolResultMessage` 把后果送回上下文，策略钩子承接授权决策。判断一个 Agent，不看它回答得多漂亮，看每一次行动之后有没有拿到可信反馈。

下一篇聊聊这些消息为什么会越来越多：上下文、压缩与会话树怎么让 Agent 在长期任务里接着干，又不丢掉可审计的历史。
