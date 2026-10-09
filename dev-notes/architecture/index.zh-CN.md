---
title: "架构笔记"
---

本节按照各个构建阶段说明 Tau 的架构。

Tau 有意采用小步分层方式开发。每一层都应回答三个问题：

1. 新增了什么？
2. 它为何存在？
3. 后续阶段会如何使用它？

## 当前架构分层

```text
tau_ai       provider/model streaming layer
tau_agent    portable agent harness, loop, tools, events, sessions
tau_coding   CLI app, resources, skills, extensions, commands, UI integration
```

最重要的边界是 `tau_agent` 应保持可移植。它可以定义可复用的代理“大脑”，但不应了解 VS Code、Textual、Rich 渲染、本地配置目录、斜杠命令或项目专用提示。

有关实际前端契约，请参阅[构建自定义 TUI](../custom-tui.md)。

## 阶段笔记

- [Phase 1：核心类型与事件](./phase-1-core-types-and-events.zh-CN.md)
- [Phase 2：AI 提供商层](./phase-2-ai-provider-layer.zh-CN.md)
- [Phase 3：纯代理循环](./phase-3-agent-loop.zh-CN.md)
- [Phase 4：AgentHarness](./phase-4-agent-harness.zh-CN.md)
- [Phase 5：内置编码工具](./phase-5-coding-tools.zh-CN.md)
- [多模态读取工具结果](./multimodal-read-results.zh-CN.md)
- [有界读取图像处理](./read-image-processing.zh-CN.md)
- [Phase 6：非交互式打印模式 CLI](./phase-6-print-mode-cli.zh-CN.md)
- [Phase 7：会话树和 JSONL 持久化](./phase-7-session-tree.zh-CN.md)
- [Phase 8：编码会话包装器](./phase-8-coding-session.zh-CN.md)
- [Phase 9：技能和提示模板](./phase-9-skills-prompts.zh-CN.md)
- [Phase 10：系统提示组装](./phase-10-system-prompt.zh-CN.md)
- [Phase 11：打印与事件渲染模式](./phase-11-print-event-rendering.zh-CN.md)
- [Phase 12：Textual TUI](./phase-12-textual-tui.zh-CN.md)
- [Phase 13：Tau 主目录、路径和 `.agents` 资源](./phase-13-paths-agents-resources.zh-CN.md)
- [Phase 14：会话管理器与恢复](./phase-14-session-manager-resume.zh-CN.md)
- [Phase 15：斜杠命令注册表](./phase-15-slash-command-registry.zh-CN.md)
- [Phase 16：稳健的资源发现](./phase-16-resource-discovery.zh-CN.md)
- [Phase 17：TUI 斜杠命令自动补全](./phase-17-tui-autocomplete.zh-CN.md)
- [Phase 17.5：TUI 对话记录换行](./phase-17-5-transcript-wrapping.zh-CN.md)
- [Phase 18：提供商配置基础](./phase-18-provider-config-foundation.zh-CN.md)
- [配置驱动的提供商目录](./config-driven-provider-catalog.zh-CN.md)
- [Codex 运行时模型限制](./codex-runtime-model-limits.zh-CN.md)
- [提供商重试事件](./provider-retries.zh-CN.md)
- [提供商/模型安全和 HTTP 错误详情](./provider-model-safety.zh-CN.md)
- [Phase 19：项目上下文发现与重新加载](./phase-19-context-discovery.zh-CN.md)
- [Phase 20：安装和配置文档](./phase-20-installation-docs.zh-CN.md)
- [Phase 20.1：上下文计数刷新](./phase-20-1-context-accounting.zh-CN.md)
- [Phase 20.2：思考模式控制](./phase-20-2-thinking-controls.zh-CN.md)
- [Phase 20.3：技能调用可靠性](./phase-20-3-skill-invocation.zh-CN.md)
- [Phase 20.4：会话导出与可视化](./phase-20-4-session-export.zh-CN.md)
- [排队的引导和跟进](./queued-steering-follow-ups.zh-CN.md)
- [扩展前加固总结](./pre-extension-hardening.zh-CN.md)
- [Phase 21：扩展](./phase-21-extensions.zh-CN.md)
- [可信隐藏内置扩展](./trusted-built-in-extensions.zh-CN.md)
- [Phase 6：本地推理加固和第二后端验证](./phase-6-local-inference-hardening.zh-CN.md)
- [扩展安装器](./extension-installer.zh-CN.md)
- [Phase 22：压缩重放基础](./phase-22-compaction-foundation.zh-CN.md)
- [Phase 23：高级 TUI 和产品优化](./phase-23-tui-polish.zh-CN.md)
- [有界 TUI 对话记录渲染](./tui-long-transcript-performance.zh-CN.md)
- [Phase 24：会话树分支](./phase-24-session-tree-branching.zh-CN.md)

Phase 21 扩展已经实现；请参阅对应阶段笔记和用户指南 `website/content/guides/extensions.md`。
