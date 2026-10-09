---
title: "阶段 25：系统提示词显示命令"
---

阶段 25 增加 `/system`，这是一个仅在本地执行的斜杠命令，用来显示 Tau 将发送给提供商的
实际系统提示词。

## 变更内容

- 编码命令注册表现在包含 `/system`。
- `CodingSession.system_prompt` 向命令处理器公开 harness 当前实际使用的系统提示词。
- 打印模式会在开始提供商调用前处理本地斜杠命令。
- TUI 像 `/reload` 一样在可见对话中内联渲染 `/system` 输出。

## 存在原因

系统提示词是重要的调试上下文，尤其是在组合项目指令、技能、工具和自定义提示词时。用户需要一种
直接检查它的方式，同时不改变模型看到的对话。

## 持久化与上下文行为

`/system` 返回 `CommandResult.message`；它不会追加用户消息、助手消息或自定义持久条目。这意味着：

- 该命令不会发起提供商请求；
- 显示的系统提示词不会加入 `CodingSession.messages`；
- 除了已经存在的常规初始会话元数据外，JSONL 会话存储保持不变。

这保留了用户可见 UI 输出、模型上下文和只追加会话历史之间的分离。

## 如何测试

运行：

```bash
uv run pytest tests/test_commands.py tests/test_coding_session.py tests/test_cli.py
```
