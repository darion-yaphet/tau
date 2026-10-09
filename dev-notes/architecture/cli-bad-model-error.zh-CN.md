# CLI：将错误的 `--model` 显示为清晰错误（issue #265）

## 变更内容

当模型未由所选提供商声明时，`tau --model <bad-model>` 不再因 `anyio`/`asyncio` 回溯而崩溃。`src/tau_coding/cli.py` 中的两个 `except` 处理器从 `RuntimeError` 扩展为 `(RuntimeError, ValueError)`：

- **TUI** 启动分支（`main()` → `anyio.run(run_openai_tui, …)`）；以及
- **打印模式**分支（`main()` → `anyio.run(run_openai_print_mode, …)`）。

`ProviderConfigError(ValueError)` 由事件循环深处的 `validate_provider_model()` 抛出（调用路径为 `resolve_provider_selection()` → `_resolve_tui_startup_selection()` / `run_openai_print_mode`）。由于处理器只捕获 `RuntimeError`，这个 `ValueError` 子类会作为未处理异常逸出，并打印多帧回溯。捕获 `ValueError` 后，`typer.BadParameter` 可以原样显示已有的友好消息：

> Invalid value: Model is not configured for provider local: llama. Available
> models: qwen

同一文件中的 `export` 命令已经使用正确模式（`except (RuntimeError, ValueError)`），因此可作为参考。

## 存在原因

问题报告见 https://github.com/huggingface/tau/issues/265。提供商目录有意按提供商划分，因此未知或拼写错误的模型名称属于常见用户错误。列出提供商有效模型的可操作消息已经存在于 `provider_config.validate_provider_model` 中，只是此前未能通过 CLI 入口清晰展示。

这让验证逻辑继续以 `provider_config.py` 为消息的单一事实来源，并恢复所有 `--model` 入口（TUI、打印模式和导出）之间的一致性。

## 与设计的对应关系

这是一个纯 CLI 层修复。它不涉及：

- 可移植的代理框架（`tau_agent`）；
- 提供商/模型流式处理层（`tau_ai`）；或
- Textual TUI 渲染。

验证本身已经位于 `tau_coding.provider_config`；错误仅存在于 CLI 的错误边界。这与架构原则一致：CLI 错误处理应消费来自较低层的错误，而不是让其以原始回溯形式泄露。

## 测试方法

回归测试（已验证修复前失败、修复后通过）：

```bash
uv run pytest tests/test_cli.py -k "bad_model" -v
```

- `test_tui_surfaces_bad_model_as_clean_error`
- `test_print_mode_surfaces_bad_model_as_clean_error`

两个测试都会在 `cli` 和 `tui.app` 命名空间中 monkeypatch `load_provider_settings`（因为两者都会导入它），使其返回一个唯一模型为 `qwen` 的提供商；随后分别使用 `--model llama --provider local`（TUI）和额外带有 `-p hello` 的参数（打印模式）调用 CLI runner。测试断言 `exit_code == 2`（Typer 的 `BadParameter` 约定），并且输出面板包含 `"Model is not configured for provider local: llama. Available models: qwen"`。

完整测试套件：

```bash
uv run pytest -q
uv run ruff check src/tau_coding/cli.py tests/test_cli.py
uv run ruff format --check src/tau_coding/cli.py tests/test_cli.py
```

## 备注/后续工作

- 原始 issue 草稿错误地声称打印模式路径已经处理 `ValueError`；其中的 `except (RuntimeError, ValueError)` 片段实际上来自 `export` 命令。实际复现确认打印模式也存在问题，因此两个路径一并修复。
- 一个范围更大的后续工作（不在此处范围内）：审查其余 `anyio.run(...)` 调用点，并提取共享错误处理辅助函数，使所有入口使用同一个处理器，避免再次产生差异。另一个可选优化是在进入事件循环前完成验证（在进入事件循环之前解析提供商/模型）。
