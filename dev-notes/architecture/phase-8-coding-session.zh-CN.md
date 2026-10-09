---
title: "阶段 8：编码会话包装器"
---

阶段 8 增加了 Tau 的第一个编码会话环境包装器。

实现位于：

```text
src/tau_coding/session.py
```

## 新增内容

Tau 现在提供 `CodingSession`，它组合了：

- `AgentHarness`
- 只追加会话存储
- 恢复的对话消息
- 内置编码工具
- 模型/会话元数据
- 最小斜杠命令接缝

这是第一个开始类似 Pi `AgentSession` 的层，但它有意保持小巧。

## 为什么位于 `tau_coding`

`tau_agent` 负责可复用基础组件：

- 消息
- 事件
- 作为抽象的工具
- 纯循环
- `AgentHarness`
- 底层会话条目、存储和重放

`tau_coding` 负责围绕这些组件的编码代理环境：

- 本地当前工作目录
- 内置编码工具
- 存储选择
- 斜杠命令
- 后续的提示词/资源加载
- 后续的打印和交互式前端

因此 `CodingSession` 属于 `tau_coding`。

## 加载会话

会话从 `SessionStorage` 实现加载：

```python
from pathlib import Path
from tau_coding import CodingSession, CodingSessionConfig
from tau_agent.session import JsonlSessionStorage

session = await CodingSession.load(
    CodingSessionConfig(
        provider=provider,
        model="gpt-4.1-mini",
        system=system_prompt,
        storage=JsonlSessionStorage("session.jsonl"),
        cwd=Path.cwd(),
    )
)
```

加载会执行以下步骤：

1. 读取所有会话条目
2. 为新会话初始化元数据
3. 将条目重放到 `SessionState`
4. 使用恢复的消息创建 `AgentHarness`
5. 除非提供自定义工具，否则注册内置编码工具

对于新的空会话，Tau 会追加：

- `SessionInfoEntry`
- `ModelChangeEntry`

## 提示与继续

`CodingSession.prompt()` 通过 harness 运行新的用户提示词：

```python
async for event in session.prompt("Read README.md"):
    ...
```

运行完成后，所有新的 harness 消息会作为 `MessageEntry` 记录追加。每个条目作为文件中最后一个
非旧式叶子行成为当前尖端；Tau 不再写入单独的指针。

`CodingSession.continue_()` 从恢复的状态继续运行，不会先追加新的用户消息。

## 持久化模型

阶段 8 在运行完成后持久化消息。这让实现保持简单，并避免在 `AgentHarness` 更改内存对话记录时
重复保存消息。

如果 Tau 以后需要在流式响应期间进行崩溃恢复，后续阶段可以让持久化更为增量化。

## 最小命令

`CodingSession.handle_command()` 当前仅支持：

- `/help`
- `/exit`

未知斜杠命令会返回解释性消息。普通提示词返回 `handled=False`。

这是未来命令注册表的一个小接缝。完整的 `/model`、`/sessions`、`/fork` 和 `/compact`
等命令有意延后。

## 非目标

阶段 8 不增加：

- 自动的打印模式会话持久化
- 会话目录发现
- 模型切换命令
- 直接 bash 命令前缀
- 项目指令加载
- 技能或提示词模板
- Rich 渲染
- Textual UI
- 压缩

这些属于后续阶段。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_coding_session.py
```

测试验证：

- 空会话元数据初始化
- 提示词持久化
- 对话记录恢复
- 继续运行的持久化
- 工具结果持久化
- 最小命令处理

## 下一阶段

路线图的下一阶段是技能、提示词模板和系统提示词组装基础。这里创建的编码会话包装器为这些后续
系统提供了稳定的接入位置。
