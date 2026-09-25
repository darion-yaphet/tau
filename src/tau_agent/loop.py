"""Pure Pi-compatible provider/tool agent loop.

纯 Pi 兼容的模型提供者与工具代理循环。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from time import monotonic_ns

from tau_agent.events import (
    AgentEndEvent,
    AgentEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
    TurnEndEvent,
    TurnStartEvent,
)
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    ResponseTiming,
    TextContent,
    ToolCall,
    ToolResultMessage,
)
from tau_agent.provider import CancellationToken, ModelProvider
from tau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
    AssistantMessageEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from tau_agent.tool_history import repair_tool_history
from tau_agent.tools import AgentTool, AgentToolResult

BeforeToolCall = Callable[[ToolCall], Awaitable[tuple[bool, str | None]]]
AfterToolCall = Callable[
    [ToolCall, AgentToolResult, bool],
    Awaitable[tuple[AgentToolResult, bool]],
]


async def run_agent_loop(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    prompts: Sequence[AgentMessage] = (),
    prelude_messages: Sequence[AgentMessage] = (),
    max_turns: int | None = None,
    signal: CancellationToken | None = None,
    session_id: str | None = None,
    get_steering_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    get_follow_up_messages: Callable[[], Sequence[AgentMessage]] | None = None,
    before_tool_call: BeforeToolCall | None = None,
    after_tool_call: AfterToolCall | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run the provider/tool loop and emit Pi-compatible agent events.

    运行模型提供者与工具循环，并发出 Pi 兼容的代理事件。
    """
    # Initialize this run's message collection and announce its first turn.

    # 初始化本次运行的消息集合，并通知第一轮开始。
    new_messages = list(prompts)
    if prompts:
        messages.extend(prompts)

    yield AgentStartEvent()
    yield TurnStartEvent()
    for message in prelude_messages:
        yield MessageStartEvent(message=message)
        yield MessageEndEvent(message=message)
    for prompt in prompts:
        yield MessageStartEvent(message=prompt)
        yield MessageEndEvent(message=prompt)

    if max_turns is not None and max_turns < 1:
        error = _error_message(model, "max_turns must be at least 1")
        messages.append(error)
        new_messages.append(error)
        yield MessageStartEvent(message=error)
        yield MessageEndEvent(message=error)
        yield TurnEndEvent(message=error)
        yield AgentEndEvent(messages=new_messages)
        return

    tool_by_name = {tool.name: tool for tool in tools}
    turn = 1
    first_turn = True
    pending = tuple(get_steering_messages() if get_steering_messages else ())

    while True:
        has_more_tools = True
        while has_more_tools or pending:
            if not first_turn:
                yield TurnStartEvent()
            first_turn = False

            for message in pending:
                messages.append(message)
                new_messages.append(message)
                yield MessageStartEvent(message=message)
                yield MessageEndEvent(message=message)
            pending = ()

            if max_turns is not None and turn > max_turns:
                error = _error_message(model, f"Agent stopped after max_turns={max_turns}")
                messages.append(error)
                new_messages.append(error)
                yield MessageStartEvent(message=error)
                yield MessageEndEvent(message=error)
                yield TurnEndEvent(message=error)
                yield AgentEndEvent(messages=new_messages)
                return

            # Python async generators cannot pass a yielding callback through a
            # normal await cleanly, so consume the assistant sub-generator and
            # retain its final message through the terminal event.

            # Python 异步生成器无法通过普通 await 干净地传递会产生值的回调，
            # 因此要消费助手子生成器，并从终止事件中保留其最终消息。
            assistant = None
            async for event in _assistant_events(
                provider=provider,
                model=model,
                system=system,
                messages=_provider_context(messages),
                tools=tools,
                signal=signal,
                session_id=session_id,
            ):
                yield event
                if isinstance(event, MessageEndEvent) and isinstance(
                    event.message, AssistantMessage
                ):
                    assistant = event.message

            if assistant is None:  # defensive: _assistant_events always terminates

                # 防御性处理：_assistant_events 始终会终止。
                assistant = _error_message(model, "Provider produced no assistant message")
                yield MessageStartEvent(message=assistant)
                yield MessageEndEvent(message=assistant)

            messages.append(assistant)
            new_messages.append(assistant)
            if assistant.stop_reason in {"error", "aborted"}:
                yield TurnEndEvent(message=assistant)
                yield AgentEndEvent(messages=new_messages)
                return

            tool_results: list[ToolResultMessage] = []
            calls = list(assistant.tool_calls)
            has_more_tools = bool(calls)
            for call in calls:
                async for event in _execute_tool_call(
                    call,
                    tool_by_name,
                    signal,
                    before_tool_call,
                    after_tool_call,
                ):
                    yield event
                    if isinstance(event, MessageEndEvent) and isinstance(
                        event.message, ToolResultMessage
                    ):
                        tool_results.append(event.message)
                        messages.append(event.message)
                        new_messages.append(event.message)

            yield TurnEndEvent(message=assistant, tool_results=tool_results)
            turn += 1
            pending = tuple(get_steering_messages() if get_steering_messages else ())

        follow_ups = tuple(get_follow_up_messages() if get_follow_up_messages else ())
        if follow_ups:
            pending = follow_ups
            continue
        break

    yield AgentEndEvent(messages=new_messages)


def _provider_context(messages: list[AgentMessage]) -> list[AgentMessage]:
    """Return replayable messages while retaining failures in durable history.

    返回可重放的消息，同时在持久历史中保留失败记录。

    Providers cannot consistently accept an assistant turn with no content. Tau
    persists terminal failures for diagnostics, but an empty failed or aborted
    turn is not model context and must not poison the next request.

    模型提供者无法稳定接受内容为空的助手轮次。Tau 会持久化终止失败以便诊断，
    但空的失败或已中止轮次不属于模型上下文，不应影响下一个请求。
    """
    replayable = tuple(
        message
        for message in messages
        if not (
            isinstance(message, AssistantMessage)
            and message.stop_reason in {"error", "aborted"}
            and not message.content
        )
    )
    return list(repair_tool_history(replayable).messages)


async def _assistant_events(
    *,
    provider: ModelProvider,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    signal: CancellationToken | None,
    session_id: str | None,
) -> AsyncIterator[AgentEvent]:
    """Translate provider stream events into portable agent message events.

    将模型提供者的流式事件转换为可移植的代理消息事件。
    """
    # Open the provider stream and track provider wait time for response metrics.

    # 打开模型提供者事件流，并记录等待时间以生成响应指标。
    source: AsyncIterator[AssistantMessageEvent] = provider.stream_response(
        model=model,
        system=system,
        messages=messages,
        tools=tools,
        signal=signal,
        session_id=session_id,
    )
    started = False
    provider_elapsed_ns = 0
    first_output_elapsed_ns: int | None = None
    source_iterator = source.__aiter__()
    while True:
        wait_started_ns = monotonic_ns()
        try:
            event = await anext(source_iterator)
        except StopAsyncIteration:
            break
        provider_elapsed_ns += max(0, monotonic_ns() - wait_started_ns)
        if first_output_elapsed_ns is None and isinstance(
            event,
            (
                TextDeltaEvent,
                ThinkingDeltaEvent,
                ToolCallStartEvent,
                ToolCallDeltaEvent,
                ToolCallEndEvent,
            ),
        ):
            first_output_elapsed_ns = provider_elapsed_ns
        if isinstance(event, AssistantStartEvent):
            started = True
            yield MessageStartEvent(message=event.partial)
        elif isinstance(event, AssistantDoneEvent):
            event.message.timing = _response_timing(
                first_output_elapsed_ns,
                provider_elapsed_ns,
            )
            if not started:
                yield MessageStartEvent(message=event.message)
            yield MessageEndEvent(message=event.message)
        elif isinstance(event, AssistantErrorEvent):
            event.error.timing = _response_timing(
                first_output_elapsed_ns,
                provider_elapsed_ns,
            )
            if not started:
                yield MessageStartEvent(message=event.error)
            yield MessageEndEvent(message=event.error)
        else:
            yield MessageUpdateEvent(
                message=event.partial,
                assistant_message_event=event,
            )


def _response_timing(
    first_output_elapsed_ns: int | None,
    total_elapsed_ns: int,
) -> ResponseTiming:
    """Build persistable durations from time spent awaiting provider events.

    根据等待模型提供者事件的耗时，构建可持久化的时长数据。
    """
    return ResponseTiming(
        time_to_first_output_ms=(
            first_output_elapsed_ns // 1_000_000 if first_output_elapsed_ns is not None else None
        ),
        total_duration_ms=total_elapsed_ns // 1_000_000,
    )


async def _execute_tool_call(
    call: ToolCall,
    tools: Mapping[str, AgentTool],
    signal: CancellationToken | None,
    before_tool_call: BeforeToolCall | None,
    after_tool_call: AfterToolCall | None,
) -> AsyncIterator[AgentEvent]:
    """Execute one tool call and emit its progress and result events.

    执行一次工具调用，并发出其进度与结果事件。
    """
    # Announce the call, then apply pre-execution policy and cancellation checks.

    # 先通知工具调用开始，再执行调用前策略与取消检查。
    yield ToolExecutionStartEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        args=call.arguments,
    )

    blocked = False
    block_reason: str | None = None
    if before_tool_call is not None:
        blocked, block_reason = await before_tool_call(call)

    if blocked:
        result = _error_result(block_reason or "Tool execution was blocked")
        is_error = True
    elif signal is not None and signal.is_cancelled():
        result = _error_result("Operation aborted")
        is_error = True
    else:
        tool = tools.get(call.name)
        if tool is None:
            result = _error_result(f"Tool {call.name} not found")
            is_error = True
        else:
            result, is_error, updates = await _run_tool(tool, call, signal)
            for update in updates:
                yield ToolExecutionUpdateEvent(
                    tool_call_id=call.id,
                    tool_name=call.name,
                    args=call.arguments,
                    partial_result=update,
                )

    # Allow post-execution policy to transform the final result and error state.

    # 允许调用后策略转换最终结果及错误状态。
    if after_tool_call is not None:
        result, is_error = await after_tool_call(call, result, is_error)

    yield ToolExecutionEndEvent(
        tool_call_id=call.id,
        tool_name=call.name,
        result=result,
        is_error=is_error,
    )
    message = ToolResultMessage(
        tool_call_id=call.id,
        tool_name=call.name,
        content=result.content,
        details=result.details,
        added_tool_names=result.added_tool_names,
        is_error=is_error,
    )
    yield MessageStartEvent(message=message)
    yield MessageEndEvent(message=message)


async def _run_tool(
    tool: AgentTool,
    call: ToolCall,
    signal: CancellationToken | None,
) -> tuple[AgentToolResult, bool, list[AgentToolResult]]:
    """Run a tool while collecting immutable snapshots of progress updates.

    运行工具，同时收集进度更新的不可变快照。
    """
    updates: list[AgentToolResult] = []
    accepting = True

    def on_update(partial: AgentToolResult) -> None:
        """Capture a progress update while the tool call is active.

        在工具调用处于活动状态时捕获进度更新。
        """
        if accepting:
            updates.append(partial.model_copy(deep=True))

    try:
        result = await tool.execute(call.id, call.arguments, signal, on_update)
        return result, False, updates
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - tools are an isolation boundary

        # noqa: BLE001 - 工具是异常隔离边界。
        return _error_result(str(exc)), True, updates
    finally:
        accepting = False


def _error_result(message: str) -> AgentToolResult:
    """Build a standardized error result for a failed tool call.

    为失败的工具调用构建标准化错误结果。
    """
    return AgentToolResult(content=[TextContent(text=message)], details={})


def _error_message(model: str, message: str) -> AssistantMessage:
    """Build an assistant message that records a terminal error.

    构建一条记录终止错误的助手消息。
    """
    return AssistantMessage(
        model=model,
        content=[],
        stop_reason="error",
        error_message=message,
    )
