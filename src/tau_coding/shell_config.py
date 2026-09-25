"""Durable shell execution settings for Tau terminal commands.

Tau 终端命令的持久化 Shell 执行设置。
"""

from __future__ import annotations

from dataclasses import dataclass
from json import JSONDecodeError, loads
from pathlib import Path
from typing import Any

from tau_coding.paths import TauPaths
from tau_coding.project_trust import TrustDefault


class ShellConfigError(ValueError):
    """Raised when Tau shell settings are invalid.

    Tau Shell 设置无效时抛出的异常。
    """


@dataclass(frozen=True, slots=True)
class ShellSettings:
    """Shell execution settings loaded from Tau home.

    从 Tau 主目录加载的 Shell 执行设置。
    """

    shell_command_prefix: str | None = None
    default_project_trust: TrustDefault = "ask"

    def to_json(self) -> dict[str, str]:
        """Serialize these settings to JSON-compatible data.

        将这些设置序列化为 JSON 兼容数据。
        """
        result: dict[str, str] = {}
        if self.default_project_trust != "ask":
            result["defaultProjectTrust"] = self.default_project_trust
        if self.shell_command_prefix is not None:
            result["shellCommandPrefix"] = self.shell_command_prefix
        return result


def shell_settings_path(paths: TauPaths | None = None) -> Path:
    """Return the durable shell settings path.

    返回持久化 Shell 设置路径。
    """
    return (paths or TauPaths()).home / "settings.json"


def load_shell_settings(paths: TauPaths | None = None) -> ShellSettings:
    """Load durable shell settings, falling back to built-in defaults.

    加载持久化 Shell 设置，缺失时回退到内置默认值。
    """
    path = shell_settings_path(paths)
    if not path.exists():
        return ShellSettings()
    try:
        raw = loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise ShellConfigError(f"Shell settings are not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ShellConfigError("Shell settings must be a JSON object")
    return shell_settings_from_json(raw)


def shell_settings_from_json(data: dict[str, Any]) -> ShellSettings:
    """Parse shell settings from JSON-compatible data.

    从 JSON 兼容数据解析 Shell 设置。
    """
    # Read only settings this version understands so fields written by a newer
    # Tau installation cannot prevent an older installation from starting.
    # 仅读取当前版本理解的设置，避免新版 Tau 写入的字段阻止旧版启动。
    if "shellCommandPrefix" in data and "shell_command_prefix" in data:
        raise ShellConfigError("Use only one of shellCommandPrefix or shell_command_prefix")

    raw_default = data.get("defaultProjectTrust", "ask")
    if raw_default not in {"ask", "always", "never"}:
        raise ShellConfigError("defaultProjectTrust must be ask, always, or never")

    raw_prefix = data.get("shellCommandPrefix", data.get("shell_command_prefix"))
    if raw_prefix is None:
        return ShellSettings(default_project_trust=raw_default)
    if not isinstance(raw_prefix, str):
        raise ShellConfigError("shellCommandPrefix must be a string")
    prefix = raw_prefix.strip()
    return ShellSettings(
        shell_command_prefix=prefix or None,
        default_project_trust=raw_default,
    )
