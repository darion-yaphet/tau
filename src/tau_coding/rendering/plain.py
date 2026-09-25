"""Pi-style final text renderer for print mode.

打印模式使用的 Pi 风格最终文本渲染器。
"""

import typer

from tau_agent.events import MessageEndEvent
from tau_agent.messages import AssistantMessage
from tau_coding.events import AutoRetryEndEvent, CodingSessionEvent


class FinalTextRenderer:
    def __init__(self) -> None:
        """Initialize final-message and success tracking.

        初始化最终消息与成功状态跟踪。
        """
        self._last_assistant_text = ""
        self._failed = False
        self._error_messages: list[str] = []

    def render(self, event: CodingSessionEvent) -> None:
        """Capture the final assistant text or terminal error from an event.

        从事件中捕获最终助手文本或终止错误。
        """
        if isinstance(event, AutoRetryEndEvent) and event.success:
            self._failed = False
            self._error_messages.clear()
            return
        if not isinstance(event, MessageEndEvent) or not isinstance(
            event.message, AssistantMessage
        ):
            return
        self._last_assistant_text = event.message.text
        if event.message.stop_reason in {"error", "aborted"}:
            self._failed = event.message.stop_reason == "error"
            if event.message.error_message:
                self._error_messages.append(event.message.error_message)

    def finish(self) -> bool:
        """Print the captured final text and return the session outcome.

        打印捕获的最终文本并返回会话结果。
        """
        if self._failed:
            for message in self._error_messages:
                typer.echo(f"Error: {message}", err=True)
            return False
        if self._last_assistant_text:
            typer.echo(self._last_assistant_text)
        return True
