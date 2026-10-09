---
title: "Phase 14：会话管理器与恢复"
---

Phase 14 使 Tau 会话成为存储在 Tau 主目录下的一等记录。

实现位于：

```text
src/tau_coding/session_manager.py
```

## 新增内容

Tau 现在具有一个 `SessionManager`，可以：

- 创建新会话
- 在用户主目录元数据中索引会话
- 按最新优先列出会话
- 按 ID 查找会话
- 在持久化新消息后更新会话
- 为现有 TUI 行为返回默认项目会话

会话元数据由以下类型表示：

```python
CodingSessionRecord
```

包含：

- `id`
- `path`
- `cwd`
- `model`
- `title`
- `created_at`
- `updated_at`

## 存储布局

会话对话记录仍然是仅追加 JSONL 文件。

会话元数据最初存储在全局兼容性索引中：

```text
~/.tau/sessions/index.jsonl
```

当前会话元数据和对话记录文件位于项目专用目录：

```text
~/.tau/sessions/<cleaned-path-suffix>-<short-hash>/
```

默认项目会话仍为：

```text
~/.tau/sessions/<cleaned-path-suffix>-<short-hash>/default.jsonl
```

但现在使用稳定 ID 建立索引：

```text
default-<cleaned-path-suffix>-<short-hash>
```

新会话存储为：

```text
~/.tau/sessions/<cleaned-path-suffix>-<short-hash>/<session-id>.jsonl
```

## CodingSession 集成

`CodingSessionConfig` 现在接受：

```python
session_id: str | None
session_manager: SessionManager | None
```

会话持久化新消息时，会更新会话管理器记录，使 `updated_at` 保持最新。

这让会话对话记录和会话索引保持松耦合：`tau_agent` 仍然只了解仅追加会话存储，而 `tau_coding` 负责面向用户的会话元数据。

## TUI 集成

TUI 现在通过 `SessionManager` 创建会话。

默认行为：

```bash
tau
```

会创建一个新会话。

为清晰起见，也接受显式新建会话标志：

```bash
tau --new-session
```

恢复已索引会话：

```bash
tau --resume <session-id>
```

如果会话 ID 未知，Tau 会以清晰错误退出。

后续 TUI 优化还通过以下命令添加了进程内会话切换：

```text
/resume <session-id>
```

单独的 `/resume` 会打开限定于项目的会话选择器。`/resume <session-id>` 会重新加载所选已索引会话并重建可见对话记录，无需重启 Tau。

## CLI 会话列表

Tau 可以列出已索引会话：

```bash
tau sessions
```

首次实现打印制表符分隔的行：

```text
<id>    <title>    <model>    <cwd>
```

更丰富的模态会话选择器现在使用与 `/resume` 相同的项目作用域会话记录。

## 恢复时修复被中断的工具调用

Tau 通过添加合成工具结果来修复被取消的工具调用：

```text
Tool call interrupted by user
```

这使 OpenAI 兼容对话记录保持有效，因为如果历史中助手工具调用没有匹配的工具输出，这些提供商会拒绝它。旧版本中存在一个隐蔽的恢复 bug：修复可能只添加到内存中的 `AgentHarness`，使当前会话能继续，却没有写回仅追加 JSONL 文件。执行 `/resume` 后，Tau 从磁盘重建的分支不包含该合成工具结果，因此下一个提供商请求会失败并出现类似错误：

```text
No tool output found for function call call_...
```

`CodingSession.load()` 现在会在返回已恢复会话前检查活动分支是否存在未匹配的助手工具调用。如果发现，它会追加持久修复条目并将叶节点推进到修复后的分支。这也涵盖历史损坏会话：其中悬空工具调用之后可能已经追加了用户消息。

## 测试

本阶段由以下测试覆盖：

```text
tests/test_session_manager.py
tests/test_coding_session.py
tests/test_cli.py
```

测试验证：

- 会话创建和索引
- 默认会话记录
- 最新优先列表
- 提示持久化后的元数据更新
- TUI 恢复/新建会话 CLI 连接
- 会话列表 CLI 输出

## 下一阶段

下一阶段应使用命令注册表替换硬编码斜杠命令处理。该注册表随后可驱动 TUI 自动补全和 `/sessions`、`/resume`、`/new` 等会话命令。
