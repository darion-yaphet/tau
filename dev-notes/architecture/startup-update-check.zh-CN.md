# 启动更新检查

Tau 现在会在启动产品体验的 CLI 路径中执行小型、尽力而为的更新检查：Textual TUI 和文本打印模式。

## 新增内容

- `tau_coding.update_check` 获取已发布包（`tau-ai`）的 PyPI 元数据。
- 使用 `packaging.version.Version` 比较版本，确保 PEP 440 发行版正确排序。
- 结果缓存在 `~/.tau/cache/update-check.json` 下，最多每天刷新一次。
- 失败会静默视为空操作：网络错误、格式错误的 JSON、缺失字段和无效版本都不会阻止启动。
- `TAU_NO_UPDATE_CHECK=1` 会禁用检查；设置 `CI` 时也会自动跳过检查。
- `tau update` 使用拥有当前 Tau 环境的包管理器升级 `tau-ai`。

## 所属位置

此功能位于 `tau_coding` 而不是 `tau_agent`，因为更新通知属于 CLI 应用行为。可复用的 Agent harness 仍独立于 PyPI、Rich/Textual UI 事项和 Tau 的主目录布局。

## 输出策略

- TUI 启动时将更新通知渲染为第一条对话项，采用固定的亮黄色粗体样式，位于发行说明、提供商错误、主题诊断或会话历史之前。
- 普通文本输出的打印模式会将通知写入 stderr。
- 结构化打印输出（`--mode json`）会抑制通知，以免破坏脚本输出。
- 实用命令（`tau --version`、`tau update`、`tau sessions`、`tau export`、`tau providers`、`tau setup`）不会运行更新检查。

## 更新命令

`tau update` 会在运行任何命令前检查当前环境：

- `uv-receipt.toml` 表示该工具由 uv 管理。Tau 获取最新稳定 PyPI 版本并运行 `uv tool install tau-ai@<latest-version>`，显式替换工具安装时记录的任何版本固定值。在 Windows 上，Tau 将此命令交给一个分离的 PowerShell 进程。辅助程序会等待原始 Tau PID 退出后再调用 uv，避免 Windows 部分替换仍在运行的工具环境。Tau 报告更新已安排，而非已经完成，并给出包含最终命令输出和退出码的日志路径。辅助程序将进程检查或等待错误视为致命错误：除非确认原始 Tau 进程不存在或观察到的进程对象已经退出，否则 uv 绝不会启动。更新可执行文件和按 Microsoft 运行时规则引用的参数行会暂存为 base64 编码的 JSON。辅助程序通过非 shell 的 `ProcessStartInfo` 启动它们，可在 Windows PowerShell 5.1 和 PowerShell 7 上保留空格、元字符、嵌入引号、空参数和尾部反斜杠，且不执行插值 shell 命令。
- `pipx_metadata.json` 表示它由 pipx 管理，因此 Tau 运行 `pipx upgrade tau-ai`。
- 发行版的标准 `INSTALLER` 元数据用于识别普通 uv 和 pip 安装。Tau 运行 `uv pip install --python <current-python> --upgrade tau-ai` 或 `<current-python> -m pip install --upgrade tau-ai`，目标是当前运行 Tau 的环境。

所选命令失败时，Tau 不会回退到另一个安装器。直接 URL 安装和可编辑安装会被引导回其原始来源；Conda/Pixi 管理的环境和无法识别的环境会收到手动说明，而不会被 pip 修改。可编辑检出安装可通过 `uv tool install --editable --force .` 刷新。

## 测试

运行：

```bash
uv run pytest tests/test_updater.py tests/test_update_check.py tests/test_cli.py tests/test_tui_app.py
```

`tests/test_updater.py` 还包含仅限 Windows 和 PowerShell 的集成覆盖。在 Windows 上，它会针对一个实时伪父进程和伪更新器启动生成的辅助程序，检查阻塞、可执行文件和参数的精确传递、退出码日志以及等待错误时的关闭式失败，全程不会安装 uv 或替换 Tau。每个已安装且受支持的引擎（`powershell.exe` 和 `pwsh.exe`）都会被测试；个别不可用的引擎会省略，只有当 Windows 两者皆无时，运行时测试才跳过。跨平台测试覆盖 Windows 引用算法、编码负载、脚本生成、分离启动选项、暂存失败和清理。当前 Ubuntu CI 无法执行 Windows 运行时用例。
