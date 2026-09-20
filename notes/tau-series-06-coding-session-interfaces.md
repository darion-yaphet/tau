# 第六篇：从大脑到产品——CodingSession、CLI 与可替换的 TUI

> 本文快照于 Tau 0.4.1（2026 年 9 月）。代码持续演进，函数名与行级细节以仓库当前源码为准。

核心代码已经回答了“模型、消息、循环和工具怎样工作”。可真要启动 `tau`，还得找到项目指令、选好 Provider、加载会话、备好工具与扩展，再把事件显示出来。内核会跑，不等于这是一次完整的编码任务。

Tau 不把这些产品职责塞进 `AgentHarness`。`src/tau_coding/session.py` 的 `CodingSession` 是这层环境包装器：Harness 是可复用大脑，CodingSession 是一次编码任务所处的真实世界。CLI、print mode 和 Textual TUI 都应通过它使用同一 Agent，而不是各自拼装一套 loop。

## CodingSession 负责装配，不重写大脑

`CodingSessionConfig` 汇集工作目录、storage、模型与 Provider、资源路径、命令、自动压缩、thinking level、扩展和 trust。宿主可显式提供 system prompt 或工具，默认则按 Tau 规则构造。

`CodingSession.load()` 的过程可概括为：

```text
读取并重放会话 entry
  → 准备 Provider、模型与 image 支持能力
  → 发现资源、解析项目 trust、加载允许的扩展
  → 创建默认 coding tools，并组合扩展工具
  → 组装 system prompt
  → 用恢复的 messages 构造 AgentHarness
  → 为 Harness 附加持久化与扩展 listener
```

这里没有新的 Agent loop。session 最终仍创建 `AgentHarness(AgentHarnessConfig(...))`，并将恢复出来的 `state.messages` 交给它。换言之，CodingSession 改变的是 Agent 的环境、初始状态与外围政策；Provider/工具循环的语义原封不动。

测试或自定义宿主可替换 storage、Provider、工具和 system prompt；`tau_agent` 不需要知道 `~/.tau`、项目根目录或 slash command。

## 项目资源怎样进入模型上下文

编码 Agent 不能只看当前输入。只看当前输入的模型，下一句就能把公共 API 改成自己喜欢的样子。Tau 的 `TauResourcePaths` 定义了用户级和项目级资源位置：默认有 `~/.tau` 与 `~/.agents`，工作目录里还会找出 `.tau/` 和 `.agents/`。

例如，`src/tau_coding/context.py` 会寻找 AGENTS 指令：全局资源中的 `AGENTS.md`、从项目根到当前目录的祖先 `AGENTS.md`，以及项目 `.tau/AGENTS.md`、`.agents/AGENTS.md`。找到的内容会包装成带路径的 `ProjectContextFile`。读不出来的资源只形成一条非致命 diagnostic，不至于让整个 session 起不来。

`build_system_prompt()` 再把工具说明、工具 guidelines、项目指令、可供模型调用的 skills、日期和当前工作目录组装成最终的 system prompt。项目指令位于 XML 风格的 `<project_context>` 区块中，skills 的名称、描述与路径位于 `<available_skills>` 中；只有在 read 工具存在时，模型才会收到读取 skill 文件的提示。没有 read，这条指令没法执行。

用户可以用替换型 `SYSTEM.md` 覆盖默认系统提示词，也可以用用户级和项目级 `APPEND_SYSTEM.md` 追加规则。替换和追加是不同语义：前者挑一个基础 prompt，后者按范围往上叠；显式启动参数的优先级还能更高。资源诊断会记录选择与遮蔽关系，让“模型为什么遵循了这条规则”不必全靠猜。

当前加载路径还会先解析项目 trust，再决定是否允许项目资源与项目扩展进入运行环境。这是产品层的信任边界：通用 Harness 不判断某个目录是否可信，也不执行项目扩展代码。审错了，锅在产品层。

## 一次用户输入在产品层发生什么

`CodingSession.prompt()` 是前端进入 Agent 的主入口。它先运行 input hook、展开 prompt template；Harness 要是正在跑，就把输入按 `steer` 或 `follow_up` 排队，或者干脆拒绝重叠运行。空闲时，它会刷新模型限制、尝试自动压缩、构造 `UserMessage`，再调用 `self._harness.prompt_message(...)`。

session 随后逐个转发 Harness 事件，同时插入应用层的事件与策略：上下文用量失效、消息持久化、自动会话命名、诊断记录、上下文 overflow 的压缩重试、Provider route failover，以及最后的 `AgentSettledEvent`。UI 应消费 `CodingSessionEvent`，而不是绕过 session 直接订阅 Harness：前者才代表一次完整的编码任务。

会话持久化也通过 Harness 事件接入。CodingSession 在构造时附加 listener，在消息结束时把消息及 leaf entry 写入 storage。这样 TUI、print mode 与恢复逻辑面对的是同一条消息事实，不用各自保存一份聊天副本。

**反例：** TUI 直接 `subscribe` Harness，或自己再调一次 `run_agent_loop`。您能看见模型增量和工具事件，但看不见 session 才有的东西：overflow 之后的压缩重试、route failover、`AgentSettledEvent`、以及已经按产品策略展开的 prompt。更糟的是第二套 loop 会绕过 CodingSession 的持久化 listener，屏幕上的对话和 JSONL 对不上。第 3 篇说内核不能有两个写者；这里对称：产品层也不能有两套循环。

## CLI 与 print mode：同一 session，不同渲染

`src/tau_coding/cli.py` 用 Typer 暴露 `tau` 命令。它负责解析 Provider、模型、工作目录和输出模式，然后准备并接管 CodingSession。对于 print mode，核心调用非常小：

```python
async for event in session.prompt(prompt):
    renderer.render(event)
return renderer.finish()
```

`create_event_renderer()` 选择三种消费者：`FinalTextRenderer` 只打印最终回答，`JsonEventRenderer` 逐行输出 Pi 兼容 JSON 事件，`TranscriptRenderer` 同时显示文本、工具活动与诊断。三者都不改变模型调用或会话状态；它们只是以不同方式读取事件。所以脚本可以挑 JSON，普通命令行可以挑 transcript，而上层自动化仍能依赖相同的 session 行为。

print mode 不是“阉割版 TUI”，而是那个事件驱动核心脱离交互组件也能跑的最小前端。

## Textual TUI 是适配器，不是核心依赖

交互式 TUI 的工作当然更复杂：维护输入框、转录视图、主题、快捷键、屏幕栈、通知和乐观用户消息。但在关键路径上，它仍是一个事件消费者。`src/tau_coding/tui/app.py` 的 `_run_prompt()` 遍历 `self.session.prompt(...)`，把每个事件交给 adapter，并对增量文本、思考块、错误和会话 settled 状态更新界面。

也就是说，换掉 Textual 不用动 `tau_agent`：一个 Web 前端、IDE 集成或 RPC host 只要创建同样的 CodingSession 并消费它的事件，就能拿到同一套工具循环、会话与资源加载。RPC 前端互换这条路的完整展开——信任、本地推理、成本与 RPC——放在第八篇：从能跑到可信。反过来，TUI 特有的焦点、动画和 widget 生命周期也不会倒灌进 Harness。脸可以明天换一张网页，循环不必重写。

“适配器边界”不等于说 UI 很薄。TUI 可以做乐观渲染、把流式 transcript 更新成组件、给后台任务发送终端通知；只是这些活儿都是把已经发生的 session 事件翻译成交互体验。Agent 是否调用模型、如何处理工具结果，不归 TUI 决定。

## 测试保护产品边界

`tests/test_system_prompt.py` 验证默认 prompt 的工具规则、项目上下文、skills、日期和 cwd，也验证自定义/追加 prompt 的优先级。`tests/test_rendering.py` 验证 transcript、最终文本和 JSON renderer 对同一事件的不同输出。其他 CodingSession 测试覆盖 session 恢复、资源加载、压缩与错误恢复。

这些测试共同保护一个边界：产品层可以添加资源发现、诊断、命令和 UI；但它得通过 CodingSession 去组合核心能力，而不是复制 Agent loop 或让核心依赖产品框架。

## 三分钟核对

1. `uv run pytest tests/test_system_prompt.py tests/test_rendering.py -q` — 应全部通过：默认 prompt 的组装规则与三种 renderer 对同一事件的输出差异，就是本篇讲的系统提示词和渲染分层。
2. `grep -n "self.session.prompt" src/tau_coding/tui/app.py` — 看 TUI 只在消费 `session.prompt(...)` 的事件流：没有哪个分支去自己跑一套 Agent loop。

## 小结

`CodingSession` 是 Tau 从“可移植 Agent 库”变成“终端编码产品”的那层适配。核心能一直小下去，产品却能接着长。

下一篇聊产品要继续长的时候，Tau 怎么用扩展、可观测性与测试方法加本事，又不让这条分层边界失控。
