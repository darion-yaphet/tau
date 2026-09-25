"""Pi-compatible JSON event stream renderer.

兼容 Pi 的 JSON 事件流渲染器。
"""

import typer

from tau_agent.events import MessageEndEvent
from tau_agent.messages import AssistantMessage
from tau_coding.events import AutoRetryEndEvent, CodingSessionEvent


class JsonEventRenderer:
    def __init__(self) -> None:
        """Initialize success tracking for the JSON event stream.

        初始化 JSON 事件流的成功状态跟踪。
        """
        self._failed = False

    def render(self, event: CodingSessionEvent) -> None:
        """Serialize and print one coding-session event as JSON.

        将一个编码会话事件序列化并打印为 JSON。
        """
        if isinstance(event, AutoRetryEndEvent) and event.success:
            self._failed = False
        if (
            isinstance(event, MessageEndEvent)
            and isinstance(event.message, AssistantMessage)
            and event.message.stop_reason == "error"
        ):
            self._failed = True
        typer.echo(event.model_dump_json(by_alias=True, exclude_none=True))

    def finish(self) -> bool:
        """Return whether the rendered session completed successfully.

        返回已渲染会话是否成功完成。
        """
        return not self._failed
