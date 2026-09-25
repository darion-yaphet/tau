"""Stateful reusable agent harness built on the Pi-compatible loop.

基于 Pi 兼容循环构建的有状态可复用代理框架。
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Literal

from tau_agent.events import AgentEvent, MessageEndEvent, MessageStartEvent
from tau_agent.loop import AfterToolCall, BeforeToolCall, run_agent_loop
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from tau_agent.provider import ModelProvider
from tau_agent.tools import AgentTool

EventListener = Callable[[AgentEvent], Awaitable[None] | None]
QueueMode = Literal["one_at_a_time", "all"]


@dataclass(frozen=True, slots=True)
class QueuedMessages:
    """Immutable snapshots of queued steering and follow-up messages.

    已排队的引导消息与后续消息的不可变快照。
    """
    steering: tuple[AgentMessage, ...] = ()
    follow_up: tuple[AgentMessage, ...] = ()

    @property
    def count(self) -> int:
        """Return the total number of queued messages.

        返回已排队消息的总数。
        """
        return len(self.steering) + len(self.follow_up)


@dataclass(slots=True)
class AgentHarnessConfig:
    """Configuration required to construct and run an agent harness.

    构建并运行代理框架所需的配置。
    """
    provider: ModelProvider
    model: str
    system: str
    tools: list[AgentTool] = field(default_factory=list)
    max_turns: int | None = None
    queue_mode: QueueMode = "one_at_a_time"
    session_id: str | None = None
    before_tool_call: BeforeToolCall | None = None
    after_tool_call: AfterToolCall | None = None


class SimpleCancellationToken:
    """Minimal mutable cancellation signal shared with providers and tools.

    与模型提供者和工具共享的最小可变取消信号。
    """
    def __init__(self) -> None:
        """Initialize the token in the active state.

        将令牌初始化为活动状态。
        """
        self._cancelled = False

    def cancel(self) -> None:
        """Mark the current operation as cancelled.

        将当前操作标记为已取消。
        """
        self._cancelled = True

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested.

        返回是否已请求取消。
        """
        return self._cancelled


class AgentHarness:
    """Reusable stateful agent brain independent of coding/UI policy.

    独立于编码与用户界面策略的可复用有状态代理大脑。
    """

    def __init__(
        self,
        config: AgentHarnessConfig,
        *,
        messages: Sequence[AgentMessage] = (),
    ) -> None:
        """Initialize harness state, listeners, and message queues.

        初始化框架状态、监听器与消息队列。
        """
        self._config = config
        self._messages = list(messages)
        self._listeners: list[EventListener] = []
        self._current_signal: SimpleCancellationToken | None = None
        self._running = False
        self._steering_queue: deque[AgentMessage] = deque()
        self._follow_up_queue: deque[AgentMessage] = deque()

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        """Return an immutable snapshot of conversation messages.

        返回会话消息的不可变快照。
        """
        return tuple(self._messages)

    @property
    def config(self) -> AgentHarnessConfig:
        """Return the active harness configuration.

        返回当前生效的代理框架配置。
        """
        return self._config

    @property
    def is_running(self) -> bool:
        """Return whether the harness is currently processing a run.

        返回代理框架当前是否正在处理运行。
        """
        return self._running

    @property
    def queued_messages(self) -> QueuedMessages:
        """Return snapshots of all queued steering and follow-up messages.

        返回所有已排队引导消息与后续消息的快照。
        """
        return QueuedMessages(tuple(self._steering_queue), tuple(self._follow_up_queue))

    @property
    def pending_message_count(self) -> int:
        """Return the number of messages waiting in both queues.

        返回两个队列中等待处理的消息数量。
        """
        return self.queued_messages.count

    def has_queued_messages(self) -> bool:
        """Return whether either input queue contains messages.

        返回任一输入队列是否包含消息。
        """
        return bool(self._steering_queue or self._follow_up_queue)

    def append_message(self, message: AgentMessage) -> None:
        """Append one message to durable conversation state.

        向持久会话状态追加一条消息。
        """
        self._messages.append(message)

    def replace_messages(self, messages: Sequence[AgentMessage]) -> None:
        """Replace the complete conversation history.

        替换完整的会话历史。
        """
        self._messages = list(messages)

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """Register an event listener and return its unsubscribe callback.

        注册事件监听器，并返回其取消订阅回调。
        """
        self._listeners.append(listener)

        def unsubscribe() -> None:
            """Remove the registered listener if it is still present.

            如果已注册的监听器仍存在，则将其移除。
            """
            with suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def cancel(self) -> None:
        """Request cancellation of the active run, if any.

        如果存在活动运行，则请求取消它。
        """
        if self._current_signal is not None:
            self._current_signal.cancel()

    def steer(self, content: str) -> QueuedMessages:
        """Queue steering text for the active agent turn.

        将引导文本加入当前代理轮次的队列。
        """
        return self.steer_message(UserMessage(content=content))

    def steer_message(self, message: AgentMessage) -> QueuedMessages:
        """Queue a structured steering message.

        将一条结构化引导消息加入队列。
        """
        self._steering_queue.append(message)
        return self.queued_messages

    def follow_up(self, content: str) -> QueuedMessages:
        """Queue follow-up text to run after tool work finishes.

        将后续文本加入队列，以便在工具工作完成后运行。
        """
        return self.follow_up_message(UserMessage(content=content))

    def follow_up_message(self, message: AgentMessage) -> QueuedMessages:
        """Queue a structured follow-up message.

        将一条结构化后续消息加入队列。
        """
        self._follow_up_queue.append(message)
        return self.queued_messages

    def clear_queues(self) -> QueuedMessages:
        """Clear both queues and return their previous contents.

        清空两个队列，并返回其原有内容。
        """
        snapshot = self.queued_messages
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        return snapshot

    def pop_latest_follow_up(self) -> AgentMessage | None:
        """Remove and return the newest queued follow-up message.

        移除并返回最新的已排队后续消息。
        """
        return self._follow_up_queue.pop() if self._follow_up_queue else None

    def pop_latest_steering(self) -> AgentMessage | None:
        """Remove and return the newest queued steering message.

        移除并返回最新的已排队引导消息。
        """
        return self._steering_queue.pop() if self._steering_queue else None

    def prompt_message(self, message: AgentMessage) -> AsyncIterator[AgentEvent]:
        """Start a run from one structured prompt message.

        使用一条结构化提示消息启动运行。
        """
        self._ensure_not_running()
        self._running = True
        return self._run(prompts=(message,))

    def prompt(self, content: str) -> AsyncIterator[AgentEvent]:
        """Start a run from plain user text.

        使用普通用户文本启动运行。
        """
        return self.prompt_message(UserMessage(content=content))

    def continue_(self) -> AsyncIterator[AgentEvent]:
        """Continue the existing conversation without a new prompt.

        在不添加新提示的情况下继续现有会话。
        """
        self._ensure_not_running()
        self._running = True
        return self._run()

    async def _run(
        self,
        *,
        prompts: Sequence[AgentMessage] = (),
    ) -> AsyncIterator[AgentEvent]:
        """Run the loop, notify subscribers, and restore idle state on exit.

        运行代理循环、通知订阅者，并在退出时恢复空闲状态。
        """
        signal = SimpleCancellationToken()
        self._current_signal = signal
        try:
            # Repair dangling tool calls here, not in prompt()/continue_(),
            # so the synthetic results flow through events and reach push
            # subscribers (persistence) as well as the consumer.

            # 在此处而非 prompt()/continue_() 中修复悬空的工具调用，
            # 从而让合成结果流经事件，同时到达推送订阅者（持久化）和消费者。
            repaired_from = len(self._messages)
            self._append_interrupted_tool_results()
            repairs = self._messages[repaired_from:]
            async for event in run_agent_loop(
                provider=self._config.provider,
                model=self._config.model,
                system=self._config.system,
                messages=self._messages,
                prompts=prompts,
                prelude_messages=repairs,
                tools=self._config.tools,
                max_turns=self._config.max_turns,
                signal=signal,
                session_id=self._config.session_id,
                get_steering_messages=self._drain_steering_messages,
                get_follow_up_messages=self._drain_follow_up_messages,
                before_tool_call=self._config.before_tool_call,
                after_tool_call=self._config.after_tool_call,
            ):
                await self._notify(event)
                yield event
        finally:
            if signal.is_cancelled():
                repaired_from = len(self._messages)
                self._append_interrupted_tool_results()
                # The consumer is usually gone here; push the repairs to
                # subscribers. Listener errors are suppressed; cancellation
                # itself is not.

                # 此时消费者通常已经离开，因此将修复结果推送给订阅者。
                # 监听器错误会被抑制，但取消本身不会。
                for message in self._messages[repaired_from:]:
                    with suppress(Exception):
                        await self._notify(MessageStartEvent(message=message))
                        await self._notify(MessageEndEvent(message=message))
            if self._current_signal is signal:
                self._current_signal = None
            self._running = False

    async def _notify(self, event: AgentEvent) -> None:
        """Deliver an event to a stable snapshot of listeners.

        将事件传递给监听器的稳定快照。
        """
        for listener in list(self._listeners):
            result = listener(event)
            if isawaitable(result):
                await result

    def _ensure_not_running(self) -> None:
        """Reject attempts to start a second concurrent run.

        拒绝启动第二个并发运行的尝试。
        """
        if self._running:
            raise RuntimeError(
                "AgentHarness is already running; use steer() or follow_up() to queue messages."
            )

    def _drain_steering_messages(self) -> tuple[AgentMessage, ...]:
        """Drain steering messages according to the configured queue mode.

        按照配置的队列模式取出引导消息。
        """
        return self._drain_queue(self._steering_queue)

    def _drain_follow_up_messages(self) -> tuple[AgentMessage, ...]:
        """Drain follow-up messages according to the configured queue mode.

        按照配置的队列模式取出后续消息。
        """
        return self._drain_queue(self._follow_up_queue)

    def _drain_queue(self, queue: deque[AgentMessage]) -> tuple[AgentMessage, ...]:
        """Take either one message or all messages from a queue.

        从队列中取出一条或全部消息。
        """
        if not queue:
            return ()
        if self._config.queue_mode == "all":
            messages = tuple(queue)
            queue.clear()
            return messages
        return (queue.popleft(),)

    def append_interrupted_tool_results(self) -> int:
        """Append missing interruption results and return the number added.

        追加缺失的中断结果，并返回新增数量。
        """
        before = len(self._messages)
        self._append_interrupted_tool_results()
        return len(self._messages) - before

    def _append_interrupted_tool_results(self) -> None:
        """Complete unresolved tool calls with deterministic error results.

        使用确定性错误结果补全未解决的工具调用。
        """
        returned_ids = {
            message.tool_call_id
            for message in self._messages
            if isinstance(message, ToolResultMessage)
        }
        for message in tuple(self._messages):
            if not isinstance(message, AssistantMessage):
                continue
            for call in message.tool_calls:
                if call.id in returned_ids:
                    continue
                returned_ids.add(call.id)
                self._messages.append(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        content=[TextContent(text="Tool call interrupted by user")],
                        is_error=True,
                    )
                )
