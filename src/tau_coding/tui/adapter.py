"""Translate Pi-compatible session events into Textual display state.

将兼容 Pi 的会话事件转换为 Textual 显示状态。
"""

from tau_agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from tau_agent.messages import AssistantMessage, CustomMessage, ToolCall, UserMessage
from tau_ai.events import TextDeltaEvent, ThinkingDeltaEvent
from tau_coding.events import (
    AutoRetryStartEvent,
    CodingSessionEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    QueueUpdateEvent,
    SessionAgentEndEvent,
)
from tau_coding.session import is_context_overflow_error
from tau_coding.tui.state import TuiState, _is_file_mutation_only_message


class TuiEventAdapter:
    def __init__(self, state: TuiState) -> None:
        """Bind the adapter to mutable TUI display state.

        将适配器绑定到可变 TUI 显示状态。
        """
        self.state = state
        self._assistant_start_item_index: int | None = None
        self._pending_overflow_error: AssistantMessage | None = None
        self._tool_batch_ids: dict[str, int] = {}
        self._file_mutation_continuation_calls: set[str] = set()

    def apply(self, event: CodingSessionEvent) -> None:
        """Apply one coding-session event to the current display state.

        将一个编码会话事件应用到当前显示状态。
        """
        if isinstance(event, AgentStartEvent):
            self.state.running = True
            self.state.error = None
            return
        if isinstance(event, AgentEndEvent):
            # A bare harness event is terminal for legacy/direct adapter callers.
            # 对旧版或直接适配器调用方而言，裸 harness 事件是终止事件。
            self._flush()
            self.state.running = False
            return
        if isinstance(event, SessionAgentEndEvent):
            # Session orchestration may still compact, retry, or drain queued work.
            # 会话编排仍可能执行压缩、重试或清空排队工作。
            self._flush()
            return
        if event.type == "agent_settled":
            self._flush()
            if self._pending_overflow_error is not None:
                self.state.add_assistant_error(self._pending_overflow_error)
                self._pending_overflow_error = None
            self.state.running = False
            return
        if isinstance(event, QueueUpdateEvent):
            self.state.update_queue(steering=event.steering, follow_up=event.follow_up)
            return
        if isinstance(event, MessageStartEvent):
            if isinstance(event.message, AssistantMessage):
                self.state.assistant_buffer = event.message.text
                self._assistant_start_item_index = len(self.state.items)
            return
        if isinstance(event, MessageUpdateEvent):
            nested = event.assistant_message_event
            if isinstance(nested, TextDeltaEvent):
                self.state.assistant_buffer += nested.delta
            elif isinstance(nested, ThinkingDeltaEvent):
                self.state.add_thinking_delta(nested.delta)
            return
        if isinstance(event, MessageEndEvent):
            message = event.message
            if isinstance(message, UserMessage):
                self.state.add_user_message(message.text)
            elif isinstance(message, CustomMessage) and message.display:
                self.state.add_user_message(
                    message.text,
                    custom_type=message.custom_type,
                    details=message.details if isinstance(message.details, dict) else None,
                )
            elif isinstance(message, AssistantMessage):
                # Replace provisional delta rows with the final canonical
                # message so persisted block boundaries and ordering win.
                # 使用最终规范消息替换临时增量行，使持久化块边界和顺序优先。
                start = self._assistant_start_item_index
                if start is not None:
                    del self.state.items[start:]
                if message.stop_reason in {"error", "aborted"}:
                    if is_context_overflow_error(message):
                        # Keep the provider failure provisional while session-level
                        # overflow compaction and retry are still in progress.
                        # 会话级溢出压缩和重试仍在进行时，将提供商故障保持为临时状态。
                        self._pending_overflow_error = message
                    else:
                        # Successful overflow compaction makes the retry failure the
                        # only terminal error worth presenting.
                        # 溢出压缩成功后，重试故障成为唯一值得展示的终止错误。
                        self._pending_overflow_error = None
                        self.state.add_assistant_error(message)
                        self.state.running = False
                else:
                    self._pending_overflow_error = None
                    self.state.add_assistant_message(message, include_tool_calls=False)
                    previous_was_tool = False
                    batch_id: int | None = None
                    allows_mutation_continuation = _is_file_mutation_only_message(message)
                    for block in message.content:
                        if isinstance(block, ToolCall):
                            if not previous_was_tool:
                                batch_id = self.state.new_tool_batch_id()
                            if batch_id is not None:
                                self._tool_batch_ids[block.id] = batch_id
                            if allows_mutation_continuation:
                                self._file_mutation_continuation_calls.add(block.id)
                            previous_was_tool = True
                        else:
                            previous_was_tool = False
                self.state.assistant_buffer = ""
                self._assistant_start_item_index = None
            return
        if isinstance(event, ToolExecutionStartEvent):
            self._flush()
            self.state.add_tool_call(
                ToolCall(id=event.tool_call_id, name=event.tool_name, arguments=event.args),
                batch_id=self._tool_batch_ids.pop(event.tool_call_id, None),
                allows_file_mutation_continuation=(
                    event.tool_call_id in self._file_mutation_continuation_calls
                ),
            )
            self._file_mutation_continuation_calls.discard(event.tool_call_id)
            return
        if isinstance(event, ToolExecutionUpdateEvent):
            self.state.record_tool_update(event.tool_call_id, event.partial_result.text)
            return
        if isinstance(event, ToolExecutionEndEvent):
            self.state.record_tool_result(
                event.tool_call_id,
                event.tool_name,
                event.result,
                event.is_error,
            )
            return
        if isinstance(event, CompactionStartEvent) and event.reason == "overflow":
            self.state.add_item("status", "… Context limit reached; compacting and retrying")
            return
        if isinstance(event, CompactionEndEvent) and event.reason == "overflow":
            if (event.aborted or event.error_message) and self._pending_overflow_error is not None:
                self.state.add_assistant_error(self._pending_overflow_error)
                self._pending_overflow_error = None
            return
        if isinstance(event, AutoRetryStartEvent):
            if self.state.items and self.state.items[-1].role == "error":
                self.state.items.pop()
            self.state.error = None
            self.state.add_item("status", f"… {event.error_message}")

    def _flush(self) -> None:
        """Notify the UI that accumulated state changes are ready to render.

        通知 UI 已可渲染累计的状态变更。
        """
        if self.state.assistant_buffer:
            self.state.add_item("assistant", self.state.assistant_buffer)
            self.state.assistant_buffer = ""
