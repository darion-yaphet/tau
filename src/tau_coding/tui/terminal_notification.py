"""Best-effort terminal attention notifications for completed Tau turns.

为已完成的 Tau 轮次尽力发送终端注意力通知。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Literal, TextIO, cast

from tau_coding.tui.config import TurnNotificationMode
from tau_coding.tui.terminal_title import sanitize_terminal_title

OSC_TERMINATOR = "\a"
TURN_FINISHED_MESSAGE = "Tau turn finished"
type DesktopNotificationProtocol = Literal["osc9", "osc99"]


def terminal_notification_supported(
    *,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> bool:
    """Return whether Tau may write attention sequences to this terminal.

    返回 Tau 是否可以向此终端写入注意力序列。
    """
    env = os.environ if environ is None else environ
    target = sys.__stdout__ if stream is None else stream
    if not getattr(target, "isatty", lambda: False)():
        return False
    if env.get("TERM", "") == "dumb":
        return False
    return not bool(env.get("CI", ""))


def desktop_notification_protocol(
    *, environ: Mapping[str, str] | None = None
) -> DesktopNotificationProtocol | None:
    """Select the notification protocol explicitly supported by this terminal.

    选择此终端明确支持的通知协议。
    """
    env = os.environ if environ is None else environ
    term = env.get("TERM", "").lower()
    term_program = env.get("TERM_PROGRAM", "").lower()
    if env.get("KITTY_WINDOW_ID") or term == "xterm-kitty" or term_program == "kitty":
        return "osc99"
    if term_program in {"ghostty", "iterm.app", "iterm2", "mintty"} or env.get("MINTTY_SHORTCUT"):
        return "osc9"
    return None


def osc9_notification_sequence(message: str) -> str:
    """Build a sanitized OSC 9 desktop-notification sequence.

    构建经过清理的 OSC 9 桌面通知序列。
    """
    return f"\x1b]9;{sanitize_terminal_title(message)}{OSC_TERMINATOR}"


def osc99_notification_sequence(message: str) -> str:
    """Build a sanitized Kitty OSC 99 desktop-notification sequence.

    构建经过清理的 Kitty OSC 99 桌面通知序列。
    """
    return f"\x1b]99;;{sanitize_terminal_title(message)}\x1b\\"


def desktop_notification_sequence(
    message: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Build the desktop-notification sequence appropriate for this terminal.

    构建适合此终端的桌面通知序列。
    """
    protocol = desktop_notification_protocol(environ=environ)
    if protocol == "osc99":
        return osc99_notification_sequence(message)
    if protocol == "osc9":
        return osc9_notification_sequence(message)
    return None


class TerminalNotificationController:
    """Write a configured terminal notification without affecting core agent code.

    写入配置的终端通知，而不影响核心代理代码。
    """

    def __init__(
        self,
        mode: TurnNotificationMode,
        *,
        enabled: bool | None = None,
        writer: Callable[[str], object] | None = None,
        stream: TextIO | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Configure notification mode, environment, terminal, and output writer.

        配置通知模式、环境、终端及输出写入器。
        """
        self.mode = mode
        self._environ = os.environ if environ is None else environ
        self._stream = cast(TextIO, sys.__stdout__) if stream is None else stream
        self.enabled = (
            terminal_notification_supported(environ=environ, stream=self._stream)
            if enabled is None
            else enabled
        )
        self._writer = writer or self._default_write

    def notify_turn_finished(self) -> None:
        """Request attention for a completed turn, if notifications are enabled.

        启用通知时，为已完成轮次请求用户注意。
        """
        if not self.enabled or self.mode == "off":
            return
        sequence = (
            "\a"
            if self.mode == "bell"
            else desktop_notification_sequence(
                TURN_FINISHED_MESSAGE,
                environ=self._environ,
            )
        )
        if sequence is None:
            return
        with suppress(OSError, ValueError):
            self._writer(sequence)
            return
        self.enabled = False

    def _default_write(self, sequence: str) -> None:
        """Write and flush one notification sequence to the terminal stream.

        向终端流写入并刷新一个通知序列。
        """
        self._stream.write(sequence)
        self._stream.flush()
