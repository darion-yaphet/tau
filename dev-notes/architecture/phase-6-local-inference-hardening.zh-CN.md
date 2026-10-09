---
title: "阶段 6：本地推理加固与第二后端验证"
---

阶段 6 完成 issue #602 中通用本地提供商的验证闭环。已发布实现有意保持小巧：llama.cpp
仍是唯一内置本地后端，而确定性的第二虚假后端和仅用于测试的 Ollama 适配器用于检验公共接缝。

## 变更内容

- 一个永久的第二虚假后端覆盖不同的普通、机密和选择配置字段、与来源绑定的提供商/后端层、
  失败配置的原子性、取消、过期结果拒绝、进度脱敏和可选能力。
- Ollama 适配器实验位于 `tests/fixtures/`；Tau 生产代码不会导入它，也不会注册 Ollama 后端。
  它使用官方 OpenAI 兼容的 `/v1/models` 结构进行提供商发现，并使用原生 `/api/tags` 和
  `/api/ps` 结构获取已安装/运行中状态。
- 刷新/重新加载压力测试覆盖合并、代际退役、恶意取消、有界清理和迟到结果隔离。凭据测试覆盖
  代际引用、离线设置、状态文件脱敏和孤儿清理行为。
- 缺失的 llama.cpp 选择仍会保持为稳定引用，不会被新发现的模型覆盖。状态和模型选择会把该快照
  标记为过期。显式 Doctor 可以出于诊断目的探测当前唯一报告的模型，但绝不会改变当前选择。

## Ollama 带来的契约压力

该实验不要求通用 API 使用 llama.cpp 词汇，但确认了三个有用边界：

1. 提供商发现与本地状态可以使用不同的协议端点。`DynamicProvider.refresh_models` 返回一份完整
   的提供商快照，而 `LocalBackend.status` 通过开放式 `LocalModel.state` 字段返回已安装/运行状态。
2. `NoAuth` 是真实用例。Ollama 的本地 OpenAI 兼容端点不需要虚假密钥或 `Authorization` 请求头。
3. 后端能力操作保持可选，并可由宿主渲染。宿主负责字段、确认、取消和渲染；协议适配器返回结构化值，
   绝不构造 Textual 组件。

观察所依据的官方 API 参考为：

- <https://docs.ollama.com/api/openai-compatibility>
- <https://docs.ollama.com/api/tags>
- <https://docs.ollama.com/api/ps>

该实验明确不承诺原生 Ollama 模型管理。它只验证使用独立发现/状态端点的适配器符合提供商中立契约。

## 生命周期与安全决策

注册表保持代际局部性。退役会使来源/层 token 失效，只取消一次自有工作，只在记录的有界取消窗口内
等待，并继续监督仍在运行的回调直至结束。迟到的回调无法发布到替代代际。准备好的运行时拥有其
提供商和后端资源；失败的候选项只关闭自己的资源，最终关闭具有幂等性。

内置 llama.cpp 状态是用户级、带版本、加锁、私有且原子替换的。它只存储标准化端点、精确模型 ID、
白名单元数据、所选引用和时间戳。API 密钥仍留在凭据存储或环境中。旧 `llama-cpp` 目录提供商不会
被迁移或覆盖，Ollama 仍是文档中说明的自定义提供商选项，而不是已发布后端。

此阶段不包含阶段 7 路由器变更。没有生产级 Ollama 后端、Hugging Face 搜索/下载、llama.cpp
加载/卸载实现，也没有隐式模型文件操作。

## 迁移指南

现有手动配置的 OpenAI 兼容本地提供商可以继续工作。要把 llama.cpp 配置迁移到内置集成：

1. 独立启动服务器。
2. 打开 `/local`，选择并确认推荐的 llama.cpp 后端，然后输入端点和可选密钥。
3. 在 `/model` 中或通过 `--provider llama.cpp --model <id>` 使用 `/v1/models` 返回的精确模型 ID。
4. 仅在新会话正常工作后移除旧 `llama-cpp` 目录条目。

Tau 不会把旧的虚假密钥、虚假模型 ID、目录定义、项目设置或环境端点复制到内置状态中。重置只会
移除内置设置；服务器进程和模型文件绝不会被触碰。

## 验证

```bash
uv run pytest tests/test_local_backends.py tests/test_extension_providers.py \
  tests/test_llama_cpp_extension.py tests/test_phase6_hardening.py \
  tests/test_ollama_adapter_spike.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```
