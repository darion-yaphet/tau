---
title: "阶段 20.4：会话导出与可视化"
---

阶段 20.4 增加了一种可在 TUI 之外持久检查 Tau 会话的方式。

## 新增内容

Tau 可以把任何已索引的会话 ID 或 JSONL 会话文件导出为独立 HTML 文档：

```bash
tau export <session-id>
tau export <session-id> session.html
tau export <session-id> --format jsonl
tau export ~/.tau/sessions/<project>/<session-id>.jsonl
```

未提供目标位置时，`tau export` 会写入当前工作目录，而不是 Tau 的内部会话存储目录。
交互式会话通过以下命令提供相同的导出流程：

```text
/export [--format html|jsonl] [destination]
```

导出内容包含两个相互协调的视图：

- 会话树，保留父子关系、分支、叶子指针和当前分支路径
- 按存储顺序排列的对话记录与详情视图，包含消息、工具调用、工具结果、压缩、标签、模型变更、
  思考设置变更和自定义条目

生成的文件自包含 HTML、CSS 和 JavaScript，因此无需运行 Tau 或 Textual 应用即可打开。
对话条目渲染为紧凑、默认折叠的手风琴行（图标、标题、单行预览、时间戳），思考块、工具调用参数
和结果详情则作为内层手风琴嵌套。工具行标题为 `Tool: <name>`，会话树中的工具条目只显示工具名，
以提高可读性。带条目计数的胶囊样式页头筛选器可以在两个视图中隐藏工具调用和结果，或移除
非消息会话事件，得到以用户和助手为中心的对话记录；一个按钮可以一次展开或折叠全部手风琴。
下载按钮可以根据页面内嵌且经 base64 编码的条目数据重新生成 JSONL 导出，因此仅凭 HTML 文件
即可完整往返还原会话。筛选器只改变导出视图；完整会话仍嵌入文档中。

## 存在原因

Tau 会话是只追加树，而不是单一的扁平聊天日志。这对未来的分叉和分支工作流很重要，因为多个
候选分支可以共享同一根节点。普通对话记录会隐藏这种形状，导致难以调试重放、压缩或分支选择。

导出器保留在 `tau_coding` 中，因为它是在持久化会话数据之上的应用工作流。可复用的
`tau_agent` 会话模型仍保持提供商中立和前端中立。

## 与 Pi 的对应关系

Pi 提供 HTML 会话导出流程，用于在交互界面之外检查对话状态。Tau 复现了核心产品行为，同时让
实现保持更小：导出器直接根据现有 `SessionEntry` JSONL 记录渲染静态 HTML，而不增加独立的
客户端应用。

## 如何测试

运行针对性测试：

```bash
uv run pytest tests/test_session_export.py tests/test_cli.py -k export
```

发布前运行完整门禁：

```bash
uv run pytest
uv run ruff check src tests
uv run mypy
uv run --group docs mkdocs build --strict
```
