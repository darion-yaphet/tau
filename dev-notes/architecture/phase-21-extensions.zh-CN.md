---
title: "阶段 21：扩展"
---

Tau 扩展是用于定制编码会话的 Python 模块：它们可以增加工具和斜杠命令、观察代理事件流，
并拦截工具调用、工具结果和用户输入。该设计有意将 Pi 的扩展系统（`earendil-works/pi` 中的
`packages/coding-agent/src/core/extensions/`）移植到 Tau 的 Python 架构中，在保持核心小巧的
同时，仍能支持 Claude Code 风格的子代理扩展等真实扩展。

此设计经过对抗性评审后修订；值得关注的 v1 决策会在文中以 **裁决：** 标出。

## 目标

- 使用与技能和提示词模板相同的发现约定，从用户和项目目录加载扩展。
- 为扩展提供单一 `ExtensionAPI` 对象，并采用与 Pi 一致的命名：`register_tool`、
  `register_command`、`on(event)`、`send_user_message`、`append_entry`，以及会话上下文的只读访问。
- 支持 Pi 的关键钩子语义：`tool_call`（阻止/修改）、`tool_result`（转换）、`input`（转换/处理），
  并观察每个可移植 `AgentEvent`。
- 不修改 `tau_agent`：扩展机制完全位于 `tau_coding`，使用现有接缝
  （`AgentHarness.subscribe`、执行器包装、`CommandRegistry`、`CustomEntry`）。
- 隔离失败：损坏的扩展会成为 `ResourceDiagnostic`，绝不会让会话崩溃。

## 非目标（本阶段）

- npm 风格包管理（`pi install`）、提供商注册、自定义 TUI 组件/部件（扩展编写的 Textual 部件）、
  自定义**条目**渲染器（`registerEntryRenderer`/`appendEntry` 渲染的非 LLM 上下文卡片）、快捷键和
  标志注册、替换系统提示词、重写 `context`/`before_provider_request`，以及项目信任存储。
  这些能力已经保留名称和扩展点，但尚未实现。

  **之后已实现：** 自定义**消息**渲染器（`register_message_renderer` + `send_custom_message`），
  即 Pi 渲染器界面中用于格式化会参与 LLM 上下文的消息的子集（见下文“自定义消息渲染”）；以及由
  扩展编写的**组件部件**（`context.ui.components`：槽位部件、主区域视图和派发前按键拦截器）。
  后者经组件接缝实验采纳为正式设计（见“自定义消息渲染”下取代旧结论的裁决，以及
  `dev-notes/design/component-seam-experiment.md`）。此外还有宿主加框的**侧边栏分区**
  （`context.ui.sidebar`），其稳定键按扩展所有权隔离。自定义条目渲染器仍不在范围内。

这些能力落地时，应先研究 Pi 的实现（`earendil-works/pi` 中的
`packages/coding-agent/src/core/extensions/` 和 `docs/extensions.md`），再移植其设计，包括名称、
语义和事件结构；除非存在严格更好的方案。偏差应像下文一样附带说明原因的 **裁决：**。

## 发现与加载

扩展位置按以下顺序加载（发生名称冲突时先注册者胜出，与 Pi 的项目优先规则一致；这有意不同于采用
后者胜出的技能/提示词）：

1. `<cwd>/.tau/extensions/` —— 项目扩展（**默认关闭**，见“安全”）
2. `~/.tau/extensions/` —— 用户扩展
3. 显式传入的路径（可重复使用 `tau --extension/-e PATH`；路径可以是文件或目录）

目录内部只扫描一层，与 Pi 一致：

- `*.py` 文件是扩展模块
- 包含 `extension.py` 的子目录是扩展（对应 Pi 的 `index.ts` 约定）
- 如果某个目录（子目录或显式 `-e` 路径）的 `pyproject.toml` 声明
  `[tool.tau] extensions = ["src/pkg/extension.py", ...]`，则改为加载声明的入口。这对应 Pi 的
  `package.json` 中 `pi.extensions` 清单，使 src 布局仓库无需根目录垫片。

名称以 `_` 或 `.` 开头的条目会被跳过。符号链接文件会被跟随。

**裁决：** 清单位于 `pyproject.toml` 的 `[tool.tau]` 下，这是 Python 生态中工具配置的惯用位置，
而不是另设清单文件。它优先于同级 `extension.py`（与 Pi 顺序一致）；每个声明入口会作为以入口父目录
为根的包加载，使同级模块可以继续相对导入。包名取父目录名；入口不是 `extension.py` 时取文件主干名。
与 Pi 的差异：声明但不存在的入口会发出 `error` 诊断，而不是静默跳过，因为清单是明确声明，且 Tau
已经展示发现诊断。没有产生可用入口的清单会回退到 `extension.py` 约定；无法解析的
`pyproject.toml` 是 `warning`，因为扫描目录可能包含无关项目。声明路径不限制在清单目录内
（与 Pi 的 `path.resolve(dir, extPath)` 一致）；扩展本来就会执行任意代码，路径限制只是安全假象。

每个模块通过 `importlib` 以唯一合成模块名（`tau_extension_<slug>_<n>`）导入，因此文件名相同的
项目和用户扩展不会在 `sys.modules` 中冲突。目录扩展作为真正的包导入（将
`submodule_search_locations` 设为该目录），同级模块可通过相对导入（`from . import helper`）访问，
并进入合成命名空间下的 `sys.modules`。**裁决：** 加载器不修改 `sys.path`；不支持扩展内部绝对导入，
从而让辅助模块可安全重新加载且无名称冲突。

模块必须定义：

```python
def setup(tau: ExtensionAPI) -> None: ...
```

**裁决：** v1 中 `setup` 只允许同步函数（协程形式会产生加载错误），这样发现逻辑才能从同步的
`/reload` 路径调用。导入错误、缺少 `setup`、以及 `setup` 抛出的异常都会被捕获为
`ResourceDiagnostic`（`kind="extension"`、`severity="error"`），并跳过该扩展。

`tau --no-extensions` 会完全关闭目录发现；显式 `--extension` 路径仍会加载，与 Pi 保留 CLI
显式配置的语义一致。

**裁决：** `session_start` 由**宿主**发出，而不是由 `CodingSession.load` 发出。Pi 会先启动 UI 再
初始化扩展，正是为了让 `session_start` 处理器能使用对话框和通知。Tau 原先在 UI bridge 存在前发出，
会静默丢弃通知并取消对话框。现在 `load` 将事件标为待处理，宿主在 `set_ui_bridge(...)` 后调用
`session.emit_pending_session_start()`（幂等）释放它；TUI 在 `on_mount` 中执行，打印模式则在安装
`StderrUiBridge` 后立即执行。替换会话的路径（`new`/`resume`/`branch` 和 `/reload`）仍直接发出，
因为它们复用已经附加 bridge 的长生命周期 runtime。宿主如果从不调用就不会得到 `session_start`；
这是宿主责任，与 Pi 一致。

## 包布局

```text
src/tau_coding/extensions/
    __init__.py     public re-exports
    api.py          ExtensionAPI, ExtensionContext, hook payload/result types
    loader.py       discovery + importlib loading + diagnostics
    runtime.py      ExtensionRuntime: hook dispatch, tool wrapping,
                    command collection, harness/session binding
```

`tau_agent` 不导入扩展。runtime 只消费公共 `tau_agent` 类型（`AgentTool`、`AgentToolResult`、
`AgentEvent`、`CustomEntry`）。钩子载荷/结果类型使用冻结 dataclass；pydantic 继续只用于
`tau_agent` 线路类型。

## ExtensionAPI 界面（v1）

```python
class ExtensionAPI:
    # registration (valid during setup and afterwards)
    def register_tool(self, tool: AgentTool) -> None: ...
    def register_command(
        self, name: str, handler: ExtensionCommandHandler, *,
        description: str = "", usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None: ...
    def on(self, event: str, handler: ExtensionHandler | None = None): ...
        # usable as api.on("tool_call", fn) or @api.on("tool_call")

    # actions (valid once the session is bound; raise ExtensionError before)
    def send_user_message(
        self, content: str, *, deliver_as: Literal["steer", "follow_up"] = "follow_up",
    ) -> None: ...
    async def append_entry(self, namespace: str, data: dict[str, JSONValue]) -> None: ...
    def notify(self, message: str, level: Literal["info", "warning", "error"] = "info") -> None: ...
    def set_inference_provider(self, route: str | None) -> str: ...

    # context (read-only; includes inference_provider)
    @property
    def context(self) -> ExtensionContext: ...
```

`ExtensionContext` 公开 `cwd`、`model`、`provider_name`、`inference_provider`、`session_id`、
`session_name`、`thinking_level`、`system_prompt`、`is_running`、`has_ui` 和 `transcript`。它是绑定
`CodingSession` 的实时视图；绑定前调用动作方法会抛出 `ExtensionError`，对应 Pi 先提供抛错占位再
执行 `bindCore` 的模型。

`context.paths -> TauPaths` 是当前扩展代际解析后的只读文件系统路径快照。宿主提供的
`TauResourcePaths.paths` 具有权威性，保留自定义 `TauPaths.home` 和 `TauPaths.agents_home`。缺失时，
runtime 使用 `TauPaths(home=resource_paths.root, agents_home=resource_paths.agents_root or ~/.agents)`。
这区分了 Tau 的 `root`/`home`、`.agents` 资源的 `agents_root`/`agents_home` 和项目 `cwd`。
`ExtensionRuntime(paths=custom_paths)` 会立即公开构造值，`load` 随后将其替换为已加载资源快照。

快照按代际限定。`/reload` 和创建新代际的会话替换会使旧上下文失效；即使从捕获的旧上下文读取
`context.paths` 也会抛出 `ExtensionError`。处理器必须在替换后读取新上下文路径。

`transcript -> tuple[AgentMessage, ...]` 提供当前路径父对话（`CodingSession.messages`）的只读访问。
它对应 Pi 唯一交给扩展的对话界面 `ctx.sessionManager.getBranch()`，使子代理扩展可以移植
pi-subagents 的 `buildParentContext`，而无需让 `src/tau_agent` 导入 `src/tau_coding`。

**裁决：** `context.transcript` 返回消息的**深拷贝**，而不是实时对象。Pi 依赖 TypeScript 的
`Readonly<...>` 提供编译期只读；Python 无法保证这一点，Tau 的消息模型又是可变 pydantic 实例，
因此实时对象可能让扩展静默损坏会话。复制用于强制执行只读。这是与 Pi 的有意差异，其余语义保持一致。
Tau 的压缩和分支摘要已折叠为 `UserMessage`，因此扩展会把它们视为用户轮次，而非独立 `[Summary]`
类型。如需精确兼容，应公开分支**条目**，v1 不这样做。

事件处理器可以同步或异步；异步处理器会被等待。它们在会话事件循环上运行，因此必须快速；慢工作应放入
新任务。每次调用都包裹 try/except：处理器异常会被记录为 runtime 诊断，之后继续派发。唯一例外与 Pi
一致：抛出异常的 `tool_call` 钩子会阻止工具执行，以实现故障安全。

### 事件

> **已取代的协议说明（Pi 0.80.6 切换）：** 下述事件/工具/消息描述记录最初的 Tau 扩展实现。
> 当前契约以 `tau_agent.events`、`tau_agent.tools` 和 `tau_coding.events` 中的规范协议为准：
> 处理器接收 `(event, context)`，流式提供商事件嵌套在
> `message_update.assistant_message_event` 中，工具执行器接收
> `(tool_call_id, arguments, signal, on_update)`，自定义消息使用专用 `CustomMessage` 角色。
> 会话到扩展的适配器还会为 `turn_start` 增加 Pi 的从零开始 `turn_index` 和毫秒 `timestamp`，
> 为 `turn_end` 增加匹配的 `turn_index`；可移植 `tau_agent` 事件仍与会话无关。最终结构参见已发布
> 扩展指南和 `dev-notes/design/pi-event-migration-audit.md`。

观察事件直接复用 `AgentEvent` 的 `type` 字面值：`agent_start`、`agent_end`、`turn_start`、
`turn_end`、`message_start`、`message_delta`、`thinking_delta`、`message_end`、
`tool_execution_start`、`tool_execution_update`、`tool_execution_end`、`error`、`retry`、
`queue_update`。它们由 `AgentHarness.subscribe` 传递，不能修改任何内容。
`api.on("agent_event", fn)` 是通配监听器；它会为每个流式 token 增量触发，因此应优先监听具体事件。

runtime 派发的生命周期事件：

| 事件 | 载荷 | 结果 |
|---|---|---|
| `session_start` | `SessionStartEvent(reason: "startup" \| "reload" \| "new" \| "resume" \| "branch")` | — |
| `session_shutdown` | `SessionShutdownEvent(reason)` | — |
| `input` | `InputEvent(text, source="interactive" \| "extension", streaming_behavior="steer" \| "follow_up" \| None)` | `InputHookResult(action="continue" \| "transform" \| "handled", text=None, message=None)` |
| `tool_call` | `ToolCallHookEvent(tool_name, arguments)` | `ToolCallHookResult(block=False, reason=None, arguments=None)` |
| `tool_result` | `ToolResultHookEvent(tool_name, arguments, result)` | `ToolResultHookResult(content=None, ok=None, details=None)` |

**裁决：** `input` 钩子载荷移植 Pi 的 `InputEvent` 元数据时有两项省略。Pi 的 `images` 字段被移除
（Tau 尚无图片输入），Pi 的 `"rpc"` 来源也被移除（Tau 尚无 RPC 模式），所以 `source` 只有
`"interactive" | "extension"`。两个新字段都带向后兼容默认值（`source="interactive"`、
`streaming_behavior=None`），因此只读取 `.text` 的处理器仍可工作。当扩展通过 `send_user_message`/
`send_custom_message` 启动空闲轮次时，`source="extension"`；该路径经 `session.prompt` 进入
`run_input_hooks`。会话已运行时，扩展消息通过 `queue_steering_message`/`queue_follow_up_message`
传递，完全绕过钩子阶段，不会发出 `input`。`streaming_behavior` **确实会填充**：TUI 中用户在运行
期间提交的输入通过 `session.prompt(streaming_behavior=…)`，而 `prompt` 在进入运行中
steer/follow-up 分支前运行 input 钩子，因此钩子在流式路径看到 `"steer"`/`"follow_up"`，空闲路径
看到 `None`，与 Pi 的“空闲时为 undefined”一致。`queue_*_message` 接缝保持无钩子，与 Pi 一致，
因为那些是宿主/扩展注入的消息，而非用户输入。

**裁决：** `tool_call`/`tool_result` 钩子载荷不携带 `tool_call_id`。这些钩子通过包装工具执行器实现，
而执行器签名（`tau_agent/tools.py`）不接收调用 ID；循环会在执行后为结果写入 ID。需要 ID 关联的扩展
应使用观察事件（`tool_execution_start/end`），其中包含完整 `ToolCall`/`AgentToolResult`。

**裁决：** 已实现实时工具执行进度（Pi 的 `onUpdate`），但载荷比 Pi 更轻。工具通过可选
`on_update` 回调（`ToolUpdateCallback`，位于 `tau_agent/tools.py`）报告进度，签名为
`(message: str, data: dict[str, JSONValue] | None = None) -> None`，有意不采用 Pi 的
`onUpdate(partialResult: AgentToolResult)`。循环把每次调用转换为已有的
`ToolExecutionUpdateEvent(tool_call_id, message, data)`，它不会回显局部结果的
`content`/`details`/`ok`。原因是 Tau 的更新事件用于驱动进度行，而不是重新渲染局部工具结果；需要
完整结果的扩展应读取最终 `tool_execution_end`。`on_update` 与 Pi 一样是同步且发后不管；循环通过
无界队列以及 `_execute_tool` 中的任务/队列竞速，把调用桥接到异步事件流，保持顺序，即使工具出错或
事件流关闭/取消也绝不会丢失最终结果。

**裁决：** `on_update` 接缝通过**签名检查选择启用**，而不是改变执行器签名。`AgentTool` 在构造时
（`__post_init__` 中通过 `inspect.signature`，只检查一次）判断执行器是否声明 `on_update` 参数，
`AgentTool.execute` 只向接受该参数的执行器转发回调。这样所有现有 `(arguments, signal)` 执行器，
包括全部内置工具，都无需更改，而不必机械地增加 `on_update=None`。扩大 `ToolExecutor` 协议的替代
方案被拒绝，因为它会强迫每个执行器带上该参数，并破坏内置工具的结构类型。扩展 runtime 的工具包装器
（`_wrap_tool`）始终声明并转发 `on_update`；被包装的内部工具仍通过自身检查门，在执行器不接受时
丢弃回调。

**裁决：** 提供商 token 用量公开在 `AssistantMessage.usage` 上，与 Pi 在
`packages/ai/src/types.ts` 中的放置方式一致。扩展从 `message_end` 观察事件的
`event.message.usage` 读取真实计费用量，例如 `.input`、`.output`、`.cache_read`、`.cache_write`、
`.cache_write_1h`、`.reasoning`、`.total_tokens`。`usage` 是 `Usage | None`：提供商未报告时为
`None`，而不是 Pi 始终存在的全零对象，因此下游扩展能区分“未报告”和“确实为零”。有两个字段级偏差，
都因为 Tau 没有逐模型价格表（不存在 Pi `models.ts` 中 `calculateCost`/`model.cost` 的对应物）：
（1）类型中保留 `Usage.cost` 以维持结构兼容，但始终为 `None`，提供商只填 token 数；（2）OpenAI
Responses/Codex 路径的 `cache_write` 保持 0，因为该 API 不报告缓存创建 token，与 Pi 相同。
用量仅针对**单次响应**；生命周期/上下文总量可以通过对话中所有 `message.usage` 求和得到，Pi 同样在
UI/会话层聚合，而不是存到消息上。

链式语义与 Pi 一致：`input` 转换依次传递，`handled` 会短路；`tool_call` 阻止会短路剩余处理器；
`tool_result` 覆盖会依次传递，每个处理器看到前面的修改。扩展按加载顺序运行，同一扩展的处理器按注册
顺序运行。

### 工具

扩展注册普通 `AgentTool` 值（名称、描述、原始 JSON schema `input_schema`、异步执行器），与内置工具
采用相同的手写 schema 约定（ADR 0002）。每个名称先注册者胜出；与内置工具同名的扩展工具会替换
内置工具，遵循 Pi 的覆盖规则。注册的工具像内置工具一样出现在系统提示词工具列表、TUI 侧边栏和
`/session` 计数中。工具附带的 `prompt_snippet`/`prompt_guidelines` 会像内置工具一样进入系统提示词，
`add_prompt_guideline` 则通过 `BuildSystemPromptOptions.extra_guidelines` 增加独立指南行；这些内容变化时
会在 `/reload` 时重建。需要结构化常驻上下文的扩展使用 `add_prompt_section(title, body)`。这些由来源
拥有的自由格式块会在 CLI/资源追加内容之后，通过 `BuildSystemPromptOptions.extra_sections` 进入提示词，
保留注册顺序，并参与重新加载变更检测。

### 命令

`register_command` 会把处理器包装成 `SlashCommand`，注册到每个会话的 `CommandRegistry`。该注册表
通过调用 `create_default_command_registry()` 后叠加扩展命令构建；注册表没有克隆操作，重新构建就是
实现机制。内置命令名称不能覆盖；重复注册会被捕获并记录为诊断，从而保持
`tests/test_commands.py::test_registered_commands_are_pi_aligned` 不变。

**裁决：** v1 的扩展命令处理器只允许同步函数。完整命令路径
（`CommandRegistry.execute` → `CodingSession.handle_command` → TUI 提交处理器）是同步的；将它改为
异步会波及 TUI。处理器接收 `(args: str, context: ExtensionCommandContext)`，可返回 `None` 或字符串
消息，后者进入普通 `CommandResult.message` 路径。长时间命令工作应调用 `send_user_message` 或创建任务。
扩展命令会自动出现在 TUI 自动补全中，因为自动补全读取 `CommandRegistry.list_commands()`。

### UI 对话框

`context.ui`（Pi 的 `ctx.ui`）公开宿主提供的交互式对话框和 `notify`：

```python
async def select(title: str, options: Sequence[str], *, timeout: float | None = None) -> str | None
async def confirm(title: str, message: str, *, timeout: float | None = None) -> bool
async def input(title: str, placeholder: str = "", *, timeout: float | None = None) -> str | None
def notify(message: str, level: Literal["info", "warning", "error"] = "info") -> None
```

这些方法委托给宿主 `UiBridge`（`api.py`）：`NullUiBridge`（无界面/测试）和 `StderrUiBridge`
（打印模式）返回 Pi 的无操作默认值（`None`/`False`/`None`）；`_TuiExtensionUiBridge`
（`tui/app.py`）驱动三个 `ModalScreen`（`ExtensionSelectScreen`/`ExtensionConfirmScreen`/
`ExtensionInputScreen`）。`api.notify` 继续作为 `context.ui.notify` 的向后兼容别名。

**裁决：** 扩展 UI 对话框由宿主提供（`select`/`confirm`/`input`），不是由扩展编写的部件，因此虽然
“自定义 TUI 组件”是非目标，它们仍符合该设计精神。以下与 Pi 的差异都是 v1 有意为之：
（a）**没有 `AbortSignal`**，只有 `timeout`，单位为**秒**而不是 Pi 的毫秒；超时后对话框自动关闭并
返回无操作默认值，也不显示 Pi 那样的实时倒计时。（b）`input` 原样返回输入文本，空提交返回空字符串，
只有取消或 escape 返回 `None`，与 Pi 的 `string | undefined` 一致。（c）TUI bridge 通过
`push_screen(screen, callback)` + `asyncio.Future` 等待每个模态框，而不是 `push_screen_wait`；后者
要求 Textual worker 上下文，但同步命令处理器创建的任务不具备该上下文。future/callback 模式已经用于
OAuth `_manual_code_input` 流程，可从应用事件循环上的任意协程使用。

**裁决：** 即使对话框是异步的，上述“同步命令处理器”裁决在 v1 中仍然保留。需要对话框的扩展
`/command` 不会直接等待它，而是创建事件循环任务并立即返回：

```python
def _handler(args, context):
    async def _menu():
        choice = await context.api.context.ui.select("Action", ["deploy", "cancel"])
        if choice is not None:
            context.api.send_user_message(f"run {choice}")
    asyncio.get_running_loop().create_task(_menu())
    return "opening menu..."
```

这样做是安全的，因为 `CodingSession.handle_command` 从 TUI 的异步提交路径（`tui/app.py`）调用，
也就是运行在 Textual 事件循环线程上，因此 `asyncio.get_running_loop()` 可用，创建的任务也共享该
事件循环。长期更干净的方案是把命令路径改为异步（忠实移植 Pi），但这会沿
`CommandRegistry.execute` → `handle_command` → TUI 提交处理器一路波及，因而暂时延后。

## 自定义消息渲染

扩展可以格式化其注入消息，而不是保留原始文本。tau-subagents 用它把后台代理的
`<task-notification>` 渲染为紧凑状态块。

API 对应 Pi 的 `registerMessageRenderer` + `sendMessage`：

```python
def setup(tau):
    tau.register_message_renderer("subagent-notification", render_notification)

# later, from a tool executor / event handler:
tau.send_custom_message(
    "<task-notification>...</task-notification>",
    custom_type="subagent-notification",
    details={"id": run_id, "status": "completed", ...},
)
```

渲染器类型为 `Callable[[CustomMessageView, MessageRenderOptions], str]`：参数包括
`CustomMessageView(custom_type, content, details)` 和 `MessageRenderOptions(expanded)`。它返回
**Rich markup 字符串**，例如 `"[bold]✓ done[/bold]"`。

数据流为：`send_custom_message` → 携带 `custom_type`/`details` 的 runtime `CustomMessage`
（其内容会转换为面向提供商的用户消息）→ `MessageEndEvent(message=...)` → 持久化的
`CustomMessageEntry` → TUI 适配器将其投影为 `ChatItem(role="custom")` → 渲染路径调用
`runtime.render_custom_message(...)`。后者查找已注册渲染器、构造 view/options 并返回 markup，
或返回 `None` 以回退到原始 `content`。解析器安装到每条渲染路径中：实时 TUI
（`state.custom_renderer`，由 `TranscriptView._redraw` / `TranscriptMessageWidget` /
`render_chat_item` 消费）、会话**恢复**（`SessionState` 重建 runtime `CustomMessage`）以及
**打印模式**对话记录（`TranscriptRenderer`，在 `cli.py` 中接线）。

**裁决：** 自定义消息渲染器返回 **markup 字符串，而不是 Textual 部件**，这不同于 Pi 返回
`Component`。扩展因此无需导入任何 TUI 工具包；扩展只生成字符串，由宿主决定显示方式（TUI 中为
Rich markup，打印模式中为 Rich `Text`）。格式错误的 markup 绝不会使前端崩溃：
`Text.from_markup` 在保护逻辑内调用，失败时回退到字面文本。

**裁决（2026-07 子代理边界审计中再次确认）：** 当子代理体验（代理条带、原地代理视图）不得不针对
通用数据接缝在宿主侧构建，而无法像 pi-subagents 一样由扩展提供时，团队重新审视了“字符串而非部件”
的选择。曾考虑并拒绝 Pi 风格组件接缝（`ctx.ui.custom` / `setHeader` / `setFooter` /
`setEditorComponent`）。关键差异在于 **Pi 发布自己的 TUI 框架**：`@earendil-works/pi-tui` 是 Pi
单仓库中的同级包，因此 Pi 交给扩展的 `Component` 接口由它自己拥有，可与宿主同步演进。Tau 使用
第三方工具包 **Textual**；部件接缝会把 Textual API 提升为 Tau 的公共扩展契约，使每次 Textual
升级都可能破坏生态，并永远阻碍前端替换。字符串还可用于所有前端（TUI、打印模式和未来宿主），也不会
让渲染循环崩溃或卡死；迄今扩展需求（对话框、消息渲染器、工具调用行、对话来源、主题）均可表示为数据
接缝。实际结果是，“从核心移除代码”的吸引力多半是假象：功能会移出核心，但核心必须增加更庞大的部件
托管层，包括挂载、生命周期、布局槽位、焦点/输入路由和错误隔离。**重新开启条件：**（a）Tau 拥有
自己的 UI 层；（b）出现无法用数据接缝表示的真实第三方扩展需求；（c）上游 Tau 明确需要组件扩展。
在原始部件前，首选折中方案是**声明式 UI 层**，由扩展发出结构化组件描述、宿主渲染；应先基于 Pi 的
`ctx.ui` 界面设计。

**裁决（已于 2026-07-09 被取代——采用组件接缝）：** 条件（b）在实践中发生。让子代理体验留在
宿主侧迫使代理词汇和 UI 进入核心（代理条带与原地查看器位于 `tui/app.py`），违背扩展系统目的。
`component-seam-experiment` 分支随后端到端实现原始部件路径，由 tau-subagents 通过
`ComponentBridge` 拥有完整 UI，并测量了上述裁决预期的成本（`component-seam-experiment.md` §8：
核心源码约增加 250 行，公共契约更重；预测成立并被接受）。最终决定：组件接缝是**正式设计**，不是实验。
组件类型为 Textual `Widget`；这种耦合是有意承担的，扩展针对 Tau 固定的 Textual 版本构建，Textual
大版本升级是核心与扩展共同协调的破坏性变更。能满足需求时仍优先使用字符串（消息渲染器、工具调用/结果
渲染器、字符串列表槽位部件，均可跨前端且适合打印）；需要实时交互 UI 的扩展才使用部件。声明式折中层
仍可作为未来的**补充**，而非替代。

**裁决（已由 issue #704 取代）：** 自定义消息现在符合 Pi 的双层模型：runtime `role:"custom"`
加一等持久化 `custom_message` 条目。Tau 保留持久化条目包装器的命名约定
（`parent_id`/`custom_type`），RPC 投影使用 Pi 的 `parentId`/`customType` 线路名称。JSONL 迁移边界
同时转换早期通用 `role:"custom"` 条目和更旧的、带 `custom_type` 的 Tau v1 用户消息；重放会保留
模型可见内容和元数据。

**裁决：** 解析器**绝不向渲染路径抛错**。缺失渲染器、渲染器抛错、或返回非字符串都会产生 `None`；
后两者记录 runtime 诊断，并按 `custom_type` 去重为一次，因为渲染路径在每次重绘时都会重新运行，
持续损坏的渲染器否则会让诊断无限增长。随后前端显示原始 `content`。每种 `custom_type` 先注册者胜出，
与其他扩展注册表一致；`/reload` 会清空注册表和失败去重集合。

**裁决：** Pi `sendMessage` 的 `display`（从 TUI 隐藏但保留在上下文）和 `triggerTurn` 只完成了
**部分**移植，而且不触发轮次的路径持久性更弱：当 `trigger_turn=False`（或没有安装轮次回调）时，
Tau 将消息排入 harness 的**内存** follow-up 队列；它尚未出现在对话记录中，如果会话在下一次运行前
退出，就会**静默丢失**。相比之下，Pi 即使不触发轮次也会立即将消息持久化到会话文件，并发出
`message_start`/`message_end`（`agent-session.ts:1357-1370`）。需要持久无轮次记录的扩展应同时使用
`append_entry`，或接受默认的触发轮次式传递。实时与恢复 TUI 路径以及 HTML 导出都会遵守
`display=false`；消息仍保留在模型上下文和完整 JSONL 数据中。Pi 的并行
`registerEntryRenderer`/`appendEntry`（非 LLM 上下文卡片）仍不在范围内。

**裁决：** 传递**默认值与 Pi 不同**，有意与 Tau 现有 `send_user_message` 语义一致：Pi 的
`sendMessage` 默认 `triggerTurn: false`，流式传输期间默认 `deliverAs: "steer"`；Tau 的
`send_custom_message` 默认 `trigger_turn=True`、`deliver_as="follow_up"`。tau-subagents 通知路径
恰好需要 follow-up + 触发轮次（在 Pi 中也显式传入 `deliverAs: "followUp", triggerTurn: true`），
而且两种发送方法共享一个默认值对扩展作者更直观。调用方可以传入
`deliver_as="steer"`/`trigger_turn=False` 获取 Pi 形态行为。

注意：有两个界面会有意显示自定义消息的原始 `content`，而不是渲染后的 markup：会话 **HTML 导出**
（`session_export.py` 在没有扩展 runtime 时根据持久化对话渲染消息）和**排队消息预览**
（`harness.py` 的 `queue_update_event` 报告排队内容字符串）。它们按设计就是原始文本视图；只有实时
对话记录（TUI + 打印模式）执行自定义渲染。

## 钩子接线（无需修改 tau_agent 的拦截方式）

- **观察** —— runtime 通过 `AgentHarness.subscribe` 订阅一个监听器，再向扩展处理器扇出事件。某种
  事件没有注册处理器时，会跳过该类型的派发。
- **`tool_call` / `tool_result`** —— 交给 `AgentHarnessConfig.tools` 的每个工具（内置和扩展工具均含）
  都会被包装：包装执行器先运行 `tool_call` 钩子（它们可以阻止或替换 `arguments`），调用内部执行器，
  再对 `AgentToolResult` 运行 `tool_result` 钩子。阻止会返回 `ok=False` 结果，并将原因交给模型。
  循环的派发瓶颈（`loop.py:_execute_tool_calls`）保持不变。
- **`input`** —— `CodingSession.prompt` 在技能/模板展开**之前**对原始提示文本运行 `input` 钩子，
  与 Pi 的“命令检查 → 输入事件 → 展开”顺序一致；斜杠命令在到达 `prompt` 前已由 `handle_command`
  处理。`handled` 会消费输入：提示生成器不产生运行事件就返回，可选 `message` 通过 UI bridge 的通知
  通道发送。
- **`send_user_message` / `send_custom_message`** —— 两者都汇入同一个 `_deliver_message` 路径。
  运行中时映射到 `queue_steering_message` / `queue_follow_up_message`，存在 `custom_type` 时构造
  `CustomMessage`。空闲时，runtime 调用 `turn_requested(content, custom_type, details)` 回调；如果只
  排队 follow-up 再调用 `continue_()`，会先用过期对话访问提供商，因为循环只在一轮结束后排空队列。
  TUI 通过现有独占提示 worker 提交回调，与用户提交使用相同串行化，因此扩展轮次不会与用户发起的运行
  竞速；如果传递途中开始了运行，消息会改排入 follow-up。自定义元数据一路传到
  `CodingSession.prompt` → `harness.prompt`，所以从空闲状态启动轮次的自定义消息仍按其
  `custom_type` 渲染和持久化。打印模式和测试可以不设置回调，此时消息进入下一次运行的 follow-up 队列。
  即使空闲，`send_custom_message(trigger_turn=False)` 也会强制使用 follow-up 队列路径。
- **`append_entry`** —— 异步通过会话追加路径持久化 `CustomEntry(namespace=..., data=...)`，并设置
  正确父链接，使条目位于当前根到叶路径；偏离路径的自定义条目在恢复后不会出现在 `SessionState` 重放中。
- **`notify`** —— 路由到 `CodingSession` 拥有的 `UiBridge` 协议；TUI 安装 Textual 实现，打印模式
  使用 stderr 回退，测试安装记录器。

## 会话集成与 runtime 生命周期

`CodingSessionConfig` 增加：

- `extension_paths: tuple[Path, ...] = ()` —— 显式扩展文件/目录
- `extensions_enabled: bool = True` —— 是否启用目录发现
- `project_extensions_enabled: bool = False` —— 见“安全”
- `extension_runtime: ExtensionRuntime | None = None` —— 会话替换时的内部交接

`CodingSession.load`：

1. 创建 runtime，并通过 `loader.py` 发现/加载扩展；如果配置携带现有 runtime，则不重新运行发现和
   `setup`
2. 将扩展工具与 `create_coding_tools()` 合并（扩展按名称覆盖），并用钩子包装器包装全部工具
3. 构建每会话命令注册表（默认命令 + 扩展命令）
4. 构建 harness、绑定 runtime（上下文和动作此时生效），并订阅扇出监听器

runtime 具有**长生命周期**。`resume`、`new_session` 和分支流程使用当前 runtime 作为
`extension_runtime=` 构造替代会话，再把它重新绑定到新会话/harness（取消旧 harness 订阅并增加
新订阅），并以适当原因发出 `session_shutdown`/`session_start`。因此每个扩展的 `setup` 每进程运行
一次，而不是每次会话切换运行。`aclose` 发出 `session_shutdown(reason="quit")`。

`CodingSession.reload` 是异步的。同步命令注册表返回 `reload_requested` 动作，各前端等待会话操作。
重新加载首先在旧扩展代际仍有效时等待 `session_shutdown(reason="reload")`，清除宿主 UI，使旧代际
失效，从 `sys.modules` 清除 `tau_extension_*` 模块，然后在新的注册集合上重新导入并运行 `setup`。
之后原地重建包装工具列表（`harness.config.tools` 有意可变）、重建会话命令注册表、重新订阅扇出
监听器，并在新代际等待 `session_start(reason="reload")`。关闭处理器由此可以通过仍生效的 API 清理，
启动处理器也可以在重新加载报告完成前重新挂载 UI。摘要的 `CodingReloadSummary` 包含 `extensions`
类别。

**裁决：** 重新加载会使旧扩展实例失效，与 Pi 的 `assertActive`/`invalidate` 一致。每个加载代际
共享一个 `ExtensionGeneration` token；`reset_for_reload` 先清除旧 UI，再把该代际标记为过期并生成
新代际。每个 `ExtensionAPI` 方法/属性和每次 `ExtensionContext`/`ExtensionUi` 读取，包括 `has_ui`
等简单读取，都会先检查 token，并在过期时抛出 `ExtensionError`，与 Pi 的“所有操作均断言”一致。
因此，持有重新加载前 `tau` 的孤儿后台任务会明确失败，而不是静默操作新的注册集合；如果失败发生在
处理器内，会作为普通 runtime 诊断记录。重新绑定**不会**使实例失效，这是与 Pi 的有意差异。Pi 在每次
会话切换时替换 runtime，并通过 `withSession` 交付新上下文；Tau 的 runtime 跨 resume/new/branch
长期存在，相同扩展实例有意继续运行，其上下文视图直接反映新绑定会话。

扩展诊断会合并到 `resource_diagnostics`，因此 `/session` 和 `/reload` 无需更改 TUI 即可显示它们。

## 安全

项目扩展会在会话启动时执行任意 Python；在恶意仓库内运行 `tau` 不应直接执行代码。Pi 使用项目
信任提示保护，Tau 尚无信任存储。**裁决：** v1 默认禁用项目目录扩展。只有显式使用
`--project-extensions` 才会加载 `<cwd>/.tau/extensions/`。用户扩展和显式 `-e` 路径默认加载；
`--no-extensions` 关闭所有目录发现。后续应加入逐项目信任提示，再考虑改变默认值。

## 示例扩展：子代理

子代理扩展位于独立仓库 `rian-dolphin/tau-subagents`，而不在本仓库中。它移植
`tintinweb/pi-subagents` 的核心，并作为较新 API 接缝（清单、对话框、渲染器、`on_update`、
`context.transcript`）的参考消费者；它会对每项能力进行特性检测，以便在较旧版本上加载：

- 使用 src 布局，`pyproject.toml` 声明
  `[tool.tau] extensions = ["src/tau_subagents/extension.py"]`，是清单的参考用户
- 注册一个 `agent` 工具（`prompt`、`description`、`subagent_type`、`run_in_background`，以及
  `model`/`thinking`/`max_turns`/`resume`/`isolation`/`inherit_context`/`schedule`）、
  `get_subagent_result`、`steer_subagent`，以及在附加 UI 时打开 `context.ui` 对话菜单的 `/agents` 命令
- 代理类型来自 `.tau/agents/*.md` / `~/.tau/agents/*.md` 文件，带有 `description`、`tools`、
  `model`、`thinking`、`max_turns`、`prompt_mode`、`memory`、`isolation` 等 frontmatter；结构与 Pi
  的代理定义相同。这是示例自有的新约定，与现有 `.agents/` 资源目录并列
- 通过构造限定范围的 `CodingSession` 在**进程内**生成子代理：使用内存存储、工具白名单、独立系统
  提示词，并设置 `extensions_enabled=False` 防止子代理递归生成。这是 Pi `createAgentSession` 的
  Python 对应实现，无需 CLI 子进程
- 前台运行会阻塞，通过 `on_update` 接缝流式传输子代理活动，并返回子代理最终助手文本；后台运行立即
  返回 ID，并通过 `send_custom_message(custom_type="subagent-notification",
  deliver_as="follow_up")` 传递完成通知。通知由已注册消息渲染器显示，在旧版本上回退到
  `send_user_message`，同时也会验证空闲时的 `turn_requested` 路径

更小的示例包括 `hello_tool.py`、用于阻止危险 bash 的 `permission_gate.py`，以及
`sidebar_status.py`。

## 验证

- `tests/test_extensions.py`：发现顺序和优先级、合成模块命名、包式相对导入、损坏扩展隔离、
  强制同步 `setup`、工具注册/覆盖、命令注册/重复处理、事件扇出、`tool_call` 阻止与参数修改、
  `tool_result` 转换、`input` 转换/处理、`send_user_message` 排队和空闲轮次请求、`append_entry`
  持久化和路径内重放、重新加载（包括模块清除和过期监听器替换），以及 runtime 跨 resume/new 存活。
- `tests/test_coding_session.py` 增加会话接线测试；现有自动补全测试覆盖 TUI 自动补全接入。
- 完整门禁：`uv run pytest && uv run ruff check . && uv run mypy`。
