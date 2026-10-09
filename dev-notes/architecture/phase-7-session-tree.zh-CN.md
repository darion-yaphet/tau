---
title: "阶段 7：会话树与 JSONL 持久化"
---

阶段 7 增加了 Tau 的第一个持久化会话层。

会话基础组件位于：

```text
src/tau_agent/session/
```

## 新增内容

Tau 现在具备：

- 带类型的只追加会话条目
- JSONL 序列化辅助函数
- 本地 JSONL 会话存储
- 内存会话重放
- 分支路径遍历辅助函数

## 会话为何只追加

Tau 遵循 Pi 的核心会话理念：持久化状态不会原地编辑。Tau 会追加不可变条目，并通过重放它们
重建当前视图。

这让会话文件：

- 易于检查
- 易于安全追加
- 适合未来的分支和分叉
- 即使以后加入压缩或摘要，也能保留历史

会话文件每行包含一个 JSON 对象：

```jsonl
{"type":"message","id":"...","message":{"role":"user","content":"Hello"}}
{"type":"message","id":"...","parent_id":"...","message":{"role":"assistant","content":"Hi"}}
{"type":"label","id":"...","parent_id":"...","target_id":"...","label":"Greeting"}
```

## 条目类型

阶段 7 定义以下条目：

- `message`
- `model_change`
- `thinking_level_change`
- `compaction`
- `branch_summary`
- `label`
- `leaf`
- `session_info`
- `custom`

`compaction` 和 `branch_summary` 等条目作为后续阶段的占位符引入。阶段 22 让 `compaction`
参与重放，同时保持会话文件只追加。

## JSONL 存储

`JsonlSessionStorage` 提供第一个本地存储后端：

```python
from tau_agent.session import JsonlSessionStorage, MessageEntry
from tau_agent import UserMessage

storage = JsonlSessionStorage("session.jsonl")
await storage.append(MessageEntry(message=UserMessage(content="Hello")))
entries = await storage.read_all()
```

读取不存在的文件会得到空会话。追加时会自动创建父目录。

## 重放

`SessionState.from_entries()` 重建内存状态：

```python
from tau_agent.session import SessionState

state = SessionState.from_entries(entries)
```

重建后的状态包括：

- 对话消息
- 当前模型
- 思考级别
- 已解析的每条目标签及其最近变更时间戳
- 当前叶子节点 ID
- 会话信息
- 自定义条目

## 树路径

每个条目都包含：

```python
id: str
parent_id: str | None
```

这为 Tau 提供了重建分支所需的结构：

```python
state = SessionState.from_entries(entries, leaf_id="entry-id")
```

提供 `leaf_id` 时，只会重放从根到叶的路径。这可以防止兄弟分支泄漏到当前对话记录中。

## 边界

会话层位于 `tau_agent`，因为它属于可复用的代理核心。它不了解 CLI 参数、Textual 组件、
Rich 渲染、斜杠命令或 Tau 本地资源目录。

后续的 `tau_coding` 会话包装器会决定何时追加条目、会话文件存放在哪里，以及 `/sessions`
或 `/fork` 等命令应如何运行。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_session.py
```

测试验证：

- 条目 JSONL 往返转换
- 格式错误的 JSONL 所产生的错误
- 追加/读取存储行为
- 文件不存在时的行为
- 线性状态重放
- 分支路径重建
- 缺失父节点的验证

## 下一阶段

路线图的下一阶段是 Tau 编码会话包装器。该层可以在只追加基础之上组合会话持久化、斜杠命令、
提示词展开、资源以及打印/交互式前端。
