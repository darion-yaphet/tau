---
title: "阶段 5：内置编码工具"
---

阶段 5 在 `tau_coding` 中增加了 Tau 的第一批内置编码工具。

工具位于：

```text
src/tau_coding/tools.py
```

## 新增内容

Tau 现在为以下初始工具提供工厂函数：

- `create_read_tool()`
- `create_write_tool()`
- `create_edit_tool()`
- `create_bash_tool()`
- 用于默认工具集的 `create_coding_tools()`

每个工厂都有一个类似 Pi 的 `ToolDefinition` 对应项，其中包含描述、提示词片段、提示词指南、
JSON schema 和执行器。公共工厂返回来自 `tau_agent.tools` 的提供商中立 `AgentTool`。

## 工具为何位于 `tau_coding`

`tau_agent` 负责可移植的代理核心：消息、事件、作为抽象的工具、循环和 harness。

文件系统和 shell 访问是编码代理环境功能，因此具体实现位于 `tau_coding`。

```text
tau_agent:
  knows how to execute an AgentTool

tau_coding:
  provides read/write/edit/bash tools for local coding work
```

这让核心循环保持可复用，并独立于本地机器行为。

## 工具行为

### `read`

读取 UTF-8 文本文件。

参数：

- `path`：文件路径
- `offset`：可选的从 1 开始的起始行；接受 `0` 作为文件开头的别名
- `limit`：可选的返回行数

大量输出会使用 Pi 风格的截断元数据和继续读取提示进行截断。支持的图片文件（`jpg`、`png`、
`gif`、`webp`）会被检测并作为 base64 元数据返回，供后续 UI/提供商集成使用。

### `write`

写入完整 UTF-8 文本文件，并在需要时创建父目录。写入按路径通过文件变更队列串行化，避免对同一
文件的并发变更相互交错。

参数：

- `path`：文件路径
- `content`：完整文件内容

### `edit`

对 UTF-8 文件应用精确文本替换。

参数：

- `path`：文件路径
- `edits`：由 `{ oldText, newText }` 对象组成的数组

受 Pi 启发的重要行为：

- 每个 `oldText` 必须恰好匹配一次
- 编辑不得重叠
- 写入前进行验证
- 任一编辑失败时，文件保持不变
- 保留 UTF-8 BOM
- 保留现有换行符
- 结果包含差异、统一补丁和首个变更行元数据
- 旧式 `oldText`/`newText` 参数和 JSON 字符串形式的 `edits` 会被标准化

### `bash`

在配置的工作目录中执行 shell 命令。

参数：

- `command`：命令字符串
- `timeout`：可选超时秒数，默认不设超时

结果包含合并后的 stdout/stderr 输出、退出码、超时状态、持续时间、截断元数据，以及输出被截断时
保存完整输出的临时文件路径。

## 如何使用工具

```python
from tau_coding import create_coding_tools

tools = create_coding_tools(cwd="/path/to/project")
```

将这些工具传给 `AgentHarnessConfig` 或直接传给 `run_agent_loop()`。

## 测试

此阶段由以下测试覆盖：

```text
tests/test_coding_tools.py
```

测试验证：

- 默认工具注册
- 带行切片的文件读取
- 写入时创建父目录
- 多项精确替换
- 编辑失败时回滚
- 唯一匹配验证
- bash stdout 捕获
- bash 超时报告

## 下一阶段

下一阶段可以把这些工具接入非交互式打印模式 CLI，让用户从终端针对真实项目运行 Tau。
