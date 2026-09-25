"""Detect and normalize files dragged into the terminal.

检测并规范化拖入终端的文件。

Terminals do not deliver OS drag-and-drop as a dedicated event. When a file is
dropped onto the terminal window, the terminal emulator types the file's path
into the running program instead. Because Textual enables bracketed-paste mode,
that typed path usually arrives as a single :class:`textual.events.Paste`
message.

终端不会将操作系统拖放作为专用事件传递。文件拖入终端窗口时，终端模拟器会把文件路径
键入正在运行的程序。由于 Textual 启用了括号粘贴模式，该路径通常会作为单个
:class:`textual.events.Paste` 消息到达。

The exact text depends on the terminal:

确切文本取决于终端：

- most terminals shell-escape paths (``/tmp/my\\ file.png``) and separate
  multiple dropped files with spaces;
- some quote paths with spaces (``"/tmp/my file.png"``);
- some VTE-based terminals emit ``file://`` URIs;
- a few emit the bare path, even when it contains spaces.

- 大多数终端会对路径进行 Shell 转义，并用空格分隔多个文件；
- 一些终端会用引号括起含空格的路径；
- 一些基于 VTE 的终端会发送 ``file://`` URI；
- 少数终端会发送裸路径，即使路径中包含空格。

This module recognizes pasted text that consists solely of one or more existing
absolute paths and normalizes it to clean, space-separated filesystem paths,
quoting any path that contains whitespace.

此模块识别完全由一个或多个现有绝对路径组成的粘贴文本，并将其规范化为干净、以空格
分隔的文件系统路径；任何含空白字符的路径都会加引号。
"""

from __future__ import annotations

import shlex
from pathlib import Path
from urllib.parse import unquote, urlparse

__all__ = ["normalize_dropped_paths"]


def normalize_dropped_paths(text: str) -> str | None:
    """Return normalized prompt text when *text* looks like a file drop.

    当 *text* 看起来像文件拖放内容时，返回规范化的提示词文本。

    The pasted text is treated as a drop only when it consists exclusively of
    one or more absolute paths that exist on disk (shell-escaped, quoted, or
    ``file://`` URI forms are accepted). Anything else returns ``None`` so the
    paste falls through to default handling.

    仅当粘贴文本完全由一个或多个磁盘上存在的绝对路径组成时，才将其视为拖放；接受
    Shell 转义、引号或 ``file://`` URI 形式。其他内容返回 ``None``，交由默认逻辑处理。
    """
    stripped = text.strip()
    if not stripped:
        return None

    # A single dropped file may arrive as a bare path with unescaped spaces.
    # 单个拖放文件可能以包含未转义空格的裸路径形式到达。
    whole = _token_to_path(stripped)
    if whole is not None:
        return _quote_path(whole)

    try:
        tokens = shlex.split(stripped, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    paths: list[str] = []
    for token in tokens:
        path = _token_to_path(token)
        if path is None:
            return None
        paths.append(path)
    return " ".join(_quote_path(path) for path in paths)


def _token_to_path(token: str) -> str | None:
    """Resolve one dropped token to an existing absolute path, if possible.

    尽可能将一个拖放令牌解析为现有绝对路径。
    """
    candidate = token
    if candidate.startswith("file://"):
        parsed = urlparse(candidate)
        if parsed.netloc not in ("", "localhost"):
            return None
        candidate = unquote(parsed.path)
    path = Path(candidate)
    if not path.is_absolute() or not path.exists():
        return None
    return candidate


def _quote_path(path: str) -> str:
    """Quote *path* with double quotes when it contains whitespace.

    当 *path* 包含空白字符时用双引号括起。
    """
    if not any(char.isspace() for char in path):
        return path
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
