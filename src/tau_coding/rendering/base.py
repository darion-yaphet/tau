"""Shared event-rendering primitives for Tau coding modes.

Tau 编码模式共享的事件渲染基础组件。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from tau_coding.events import CodingSessionEvent


class PrintOutputMode(StrEnum):
    """Output modes supported by non-interactive print mode.

    非交互式打印模式支持的输出模式。
    """

    text = "text"
    json = "json"
    transcript = "transcript"
    rpc = "rpc"


class EventRenderer(Protocol):
    """Consumes agent events and renders them for a frontend or output mode.

    消费代理事件，并为前端或输出模式渲染这些事件。
    """

    def render(self, event: CodingSessionEvent) -> None:
        """Render one event.

        渲染一个事件。
        """

    def finish(self) -> bool:
        """Finish rendering and return whether the run succeeded.

        完成渲染并返回本次运行是否成功。
        """
