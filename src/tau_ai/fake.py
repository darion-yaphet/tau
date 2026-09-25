"""Deterministic Pi-compatible model provider for tests.

用于测试的确定性 Pi 兼容模型提供者。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from tau_agent.messages import AgentMessage
from tau_agent.tools import AgentTool
from tau_ai.events import AssistantMessageEvent
from tau_ai.provider import CancellationToken


class FakeProvider:
    """A provider that replays predefined assistant event streams.

    重放预定义助手事件流的模型提供者。
    """

    def __init__(self, streams: Iterable[Iterable[AssistantMessageEvent]]) -> None:
        """Store the event streams and initialize call history.

        保存事件流并初始化调用历史。
        """
        self._streams = [list(stream) for stream in streams]
        self.calls: list[tuple[str, str, list[AgentMessage], list[AgentTool]]] = []
        self.session_ids: list[str | None] = []

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Record a request and return its next cancellable event stream.

        记录一次请求，并返回下一个可取消的事件流。
        """
        self.calls.append((model, system, list(messages), list(tools)))
        self.session_ids.append(session_id)
        stream = self._streams.pop(0) if self._streams else []

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            """Yield the queued events until the stream ends or is cancelled.

            按序生成排队事件，直到事件流结束或收到取消请求。
            """
            for event in stream:
                if signal is not None and signal.is_cancelled():
                    return
                yield event

        return iterator()
