---
title: "阶段 28：兼容 Pi 的 RPC 模式"
---

阶段 28 在 `tau_coding.rpc` 增加无界面前端。它消费与打印模式和 Textual 相同的
`CodingSession` 事件，并保持 `tau_coding → tau_agent → tau_ai` 的关系。

该进程在 stdin/stdout 上使用严格 JSONL。串行化写入器可防止响应和事件字节交错；提示词工作
在 AnyIO 任务组中运行，因此事件流式传输期间仍可取消并处理排队输入。EOF 会取消当前工作、
等待自有任务，并关闭会话。

## Pi 兼容性

| 范围 | 状态 |
| --- | --- |
| prompt、steer、follow-up、abort | 已支持 |
| state/messages | 已支持 |
| 模型和思考 | Pi 结构的模型，以及 set/cycle/list，均已支持 |
| 手动压缩 | 已支持；详细使用量仍为近似值 |
| 新建/切换会话、统计、树、分叉 | Pi 结构的线路响应；接受 Tau ID 或已索引路径 |
| 命令发现 | 已支持，带有合成的 Tau 来源元数据 |
| 直接 bash | 已支持 |
| abort_bash | 延后；需要可取消的公共会话 API |
| HTML 导出、会话命名、条目游标 | 已支持 |
| 自动压缩开关 | 已支持 |
| 扩展 UI RPC | 延后；Tau 当前使用前端桥接回调 |
| 队列/重试控制、克隆、图片提示 | 延后 |

协议有意只映射公共 `CodingSession` 行为。它不读取原始会话 JSONL、不依赖 Textual，也不重复
实现代理循环。

## 验证

`tests/test_rpc.py` 通过内存 JSONL 流，用 `FakeProvider` 驱动真实 `CodingSession`。
它覆盖有关联的接收响应、事件流、格式错误记录、CRLF 和 Unicode 行分隔符。CLI 测试另行覆盖
`--mode rpc` 路由。
