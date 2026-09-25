"""Human-readable Pi-compatible streaming transcript renderer.

便于阅读且兼容 Pi 的流式记录渲染器。
"""

import typer
from rich.console import Console
from rich.text import Text

from tau_agent.events import (
    AgentEndEvent,
    MessageEndEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from tau_agent.messages import AssistantMessage, CustomMessage, ToolCall
from tau_ai.events import TextDeltaEvent
from tau_coding.events import AutoRetryEndEvent, AutoRetryStartEvent, CodingSessionEvent
from tau_coding.extensions.api import CustomMessageMarkup
from tau_coding.tui.state import format_tool_call_block


class TranscriptRenderer:
    def __init__(
        self,
        *,
        custom_message_renderer: CustomMessageMarkup | None = None,
        **_: object,
    ) -> None:
        """Configure transcript detail and initialize streaming state.

        配置记录详细程度并初始化流式状态。
        """
        self._assistant_started = False
        self._assistant_ended = False
        self._failed = False
        self._console = Console(stderr=True, highlight=False)
        self._custom_message_renderer = custom_message_renderer

    def render(self, event: CodingSessionEvent) -> None:
        """Render one session event into a readable streaming transcript.

        将一个会话事件渲染为易读的流式记录。
        """
        if isinstance(event, MessageUpdateEvent):
            nested = event.assistant_message_event
            if isinstance(nested, TextDeltaEvent):
                self._assistant_started = True
                typer.echo(nested.delta, nl=False)
            return
        if isinstance(event, ToolExecutionStartEvent):
            self._newline()
            call = ToolCall(id=event.tool_call_id, name=event.tool_name, arguments=event.args)
            self._console.print(Text(format_tool_call_block(call, compact=False), style="cyan"))
            return
        if isinstance(event, ToolExecutionUpdateEvent):
            self._newline()
            if event.partial_result.text:
                self._console.print(Text(f"… {event.partial_result.text}", style="bright_black"))
            return
        if isinstance(event, AutoRetryStartEvent):
            self._newline()
            self._console.print(Text(f"… {event.error_message}", style="bright_black"))
            return
        if isinstance(event, AutoRetryEndEvent) and event.success:
            self._failed = False
            return
        if isinstance(event, ToolExecutionEndEvent):
            status = "✗" if event.is_error else "✓"
            style = "red" if event.is_error else "green"
            self._console.print(Text(f"{status} {event.tool_name}", style=style))
            if event.result.text:
                for line in event.result.text.splitlines():
                    self._console.print(Text(f"  {line}"))
            return
        if isinstance(event, MessageEndEvent) and isinstance(event.message, CustomMessage):
            if self._custom_message_renderer is None or not event.message.display:
                return
            rendered = self._custom_message_renderer(
                event.message.custom_type,
                event.message.text,
                event.message.details if isinstance(event.message.details, dict) else None,
                False,
            )
            if rendered:
                self._newline()
                self._console.print(rendered)
            return
        if isinstance(event, MessageEndEvent) and isinstance(event.message, AssistantMessage):
            if event.message.stop_reason == "error":
                self._failed = True
                self._newline()
                self._console.print(
                    Text(f"Error: {event.message.error_message or 'Error'}", style="red")
                )
            self._newline(final=True)
            return
        if isinstance(event, AgentEndEvent):
            self._newline(final=True)

    def finish(self) -> bool:
        """Finish the transcript and return whether the session succeeded.

        完成记录并返回会话是否成功。
        """
        return not self._failed

    def _newline(self, *, final: bool = False) -> None:
        """Terminate the active output line when needed.

        在需要时结束当前输出行。
        """
        if self._assistant_started and not self._assistant_ended:
            typer.echo()
            self._assistant_ended = True
        elif final and not self._assistant_started:
            self._assistant_ended = True
