"""Mistral Conversations provider.

Mistral Conversations 提供方。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from json import JSONDecodeError, dumps, loads
from typing import Any, Protocol

import httpx

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    ImageContent,
    ThinkingContent,
    ToolResultMessage,
    UserMessage,
    assistant_content,
    message_to_user,
)
from tau_agent.tools import AgentTool, ToolCall
from tau_agent.types import JSONValue
from tau_ai._provider_events import (
    ProviderErrorEvent,
    ProviderEvent,
    ProviderResponseEndEvent,
    ProviderResponseStartEvent,
    ProviderTextDeltaEvent,
    ProviderThinkingDeltaEvent,
    ProviderToolCallEvent,
)
from tau_ai.content import (
    NON_VISION_TOOL_IMAGE_PLACEHOLDER,
    NON_VISION_USER_IMAGE_PLACEHOLDER,
    text_and_images,
)
from tau_ai.env import OpenAICompatibleConfig
from tau_ai.events import AssistantMessageEvent
from tau_ai.http import create_async_client
from tau_ai.http_errors import provider_http_error_message
from tau_ai.provider import CancellationToken
from tau_ai.retry import provider_retry_event, retry_delay_seconds, wait_for_retry
from tau_ai.stream import canonicalize_provider_stream
from tau_ai.tool_call_ids import portable_tool_call_id


class MistralConversationsProvider:
    """Provider adapter for Mistral's streaming chat API.

    Mistral 流式聊天 API 的提供方适配器。
    """

    # Initialize the provider configuration and optional HTTP client.
    #
    # 初始化提供方配置和可选的 HTTP 客户端。
    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._owns_client = client is None

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this provider created it.

        如果底层 HTTP 客户端由当前提供方创建，则将其关闭。
        """
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

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
        """Stream one response as Pi-compatible assistant message events.

        将一次响应以兼容 Pi 的助手消息事件形式进行流式传输。
        """
        del session_id
        raw = self._stream_provider_events(
            model=model, system=system, messages=messages, tools=tools, signal=signal
        )
        return canonicalize_provider_stream(
            raw, api="mistral-conversations", provider="mistral", model=model
        )

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one Mistral response as provider-neutral events.

        将一次 Mistral 响应转换为与提供方无关的事件流。
        """
        payload = _build_mistral_payload(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            max_tokens=self._config.max_tokens,
            supports_images=self._config.supports_images,
        )
        return self._stream(
            model=model,
            url=f"{_mistral_base_url(self._config.base_url)}/chat/completions",
            payload=payload,
            signal=signal,
        )

    def _stream(
        self,
        *,
        model: str,
        url: str,
        payload: Mapping[str, JSONValue],
        signal: CancellationToken | None,
    ) -> AsyncIterator[ProviderEvent]:
        # Run the retrying HTTP stream and translate parsed chunks into provider events.
        #
        # 执行带重试的 HTTP 流，并将解析后的数据块转换为提供方事件。
        async def iterator() -> AsyncIterator[ProviderEvent]:
            # Prepare the client and authentication headers before opening the stream.
            #
            # 在打开流之前准备客户端和身份验证请求头。
            client = self._get_client()
            headers = {
                **dict(self._config.headers or {}),
                "Authorization": f"Bearer {self._config.api_key}",
            }
            attempt = 0
            while True:
                # Create a fresh parser for each attempt so retries do not reuse partial state.
                #
                # 每次尝试都创建新的解析器，避免重试复用不完整状态。
                parser = _MistralStreamParser()
                try:
                    async with client.stream(
                        "POST", url, json=payload, headers=headers
                    ) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            body_text = body.decode(errors="replace")
                            if self._should_retry(attempt, status_code=response.status_code):
                                delay = retry_delay_seconds(
                                    attempt,
                                    max_delay_seconds=self._config.max_retry_delay_seconds,
                                )
                                yield provider_retry_event(
                                    attempt=attempt,
                                    max_retries=self._config.max_retries,
                                    delay_seconds=delay,
                                    reason=f"HTTP {response.status_code}",
                                    data={"status_code": response.status_code, "body": body_text},
                                )
                                attempt += 1
                                if not await wait_for_retry(delay, signal=signal):
                                    return
                                continue
                            yield ProviderErrorEvent(
                                message=provider_http_error_message(
                                    provider_name=self._config.provider_name,
                                    status_code=response.status_code,
                                    body=body_text,
                                    model=model,
                                ),
                                data={"status_code": response.status_code, "body": body_text},
                            )
                            return

                        yield ProviderResponseStartEvent(model=model)
                        # Parse each SSE line and forward its incremental events.
                        #
                        # 解析每一行 SSE，并转发其中的增量事件。
                        async for line in response.aiter_lines():
                            if signal is not None and signal.is_cancelled():
                                return
                            event = _parse_sse_line(line)
                            if event is None:
                                continue
                            events, stop = parser.feed(event)
                            for parser_event in events:
                                yield parser_event
                            if stop:
                                break
                        for parser_event in parser.finalize():
                            yield parser_event
                        return
                except httpx.HTTPError as exc:
                    if not parser.emitted_content and self._should_retry(attempt):
                        delay = retry_delay_seconds(
                            attempt,
                            max_delay_seconds=self._config.max_retry_delay_seconds,
                        )
                        yield provider_retry_event(
                            attempt=attempt,
                            max_retries=self._config.max_retries,
                            delay_seconds=delay,
                            reason="network error",
                            data={"error": str(exc), "error_type": type(exc).__name__},
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(message=str(exc), data={"attempts": attempt + 1})
                    return

        return iterator()

    # Return the configured HTTP client, creating the owned client lazily.
    #
    # 返回已配置的 HTTP 客户端，并按需延迟创建自有客户端。
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client

    # Decide whether another request attempt is allowed for the current failure.
    #
    # 判断当前失败是否允许再次尝试请求。
    def _should_retry(self, attempt: int, *, status_code: int | None = None) -> bool:
        if attempt >= self._config.max_retries:
            return False
        return status_code is None or status_code in {408, 409, 425, 429} or status_code >= 500


class _StreamParser(Protocol):
    emitted_content: bool

    # Consume one SSE payload and report emitted events and stream completion.
    #
    # 消费一个 SSE 载荷，并返回生成的事件及流是否完成。
    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]: ...

    # Finish parsing and emit the terminal provider events.
    #
    # 完成解析并生成终止提供方事件。
    def finalize(self) -> list[ProviderEvent]: ...


class _MistralStreamParser:
    # Initialize accumulators for text, reasoning, tool calls, and finish state.
    #
    # 初始化文本、推理、工具调用和结束状态的累积器。
    def __init__(self) -> None:
        self.emitted_content = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None

    # Parse one Mistral SSE payload into incremental provider events.
    #
    # 将一个 Mistral SSE 载荷解析为增量提供方事件。
    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True
        chunk = _loads_object(event)
        if chunk is None:
            return [], False
        choice = _first_choice(chunk)
        if choice is None:
            return [], False
        self._finish_reason = (
            choice.get("finish_reason") or choice.get("finishReason") or self._finish_reason
        )
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False
        events: list[ProviderEvent] = []
        for content in _content_deltas(delta):
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(ProviderTextDeltaEvent(delta=content))
        for thinking in _thinking_deltas(delta):
            self.emitted_content = True
            self._thinking_parts.append(thinking)
            events.append(ProviderThinkingDeltaEvent(delta=thinking))
        for tool_call_delta in _tool_call_deltas(delta):
            self.emitted_content = True
            index = int(tool_call_delta.get("index", 0))
            builder = self._tool_call_builders.setdefault(index, _ToolCallBuilder())
            builder.add_delta(tool_call_delta)
        return events, False

    # Assemble accumulated content into tool-call and response-end events.
    #
    # 将累积内容组装为工具调用事件和响应结束事件。
    def finalize(self) -> list[ProviderEvent]:
        tool_calls = [
            builder.build(index) for index, builder in sorted(self._tool_call_builders.items())
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        content = assistant_content("".join(self._content_parts), tool_calls)
        if self._thinking_parts:
            content.insert(0, ThinkingContent(thinking="".join(self._thinking_parts)))
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(content=content),
                finish_reason=self._finish_reason or ("tool_calls" if tool_calls else "stop"),
            )
        )
        return events


class _ToolCallBuilder:
    # Initialize fragmented tool-call fields collected from stream deltas.
    #
    # 初始化从流式增量中收集的工具调用分段字段。
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    # Merge one tool-call delta into the accumulated call state.
    #
    # 将一个工具调用增量合并到累积的调用状态中。
    def add_delta(self, delta: Mapping[str, Any]) -> None:
        call_id = delta.get("id")
        if isinstance(call_id, str) and call_id != "null":
            self.id = call_id
        function = delta.get("function")
        if not isinstance(function, Mapping):
            return
        name = function.get("name")
        if isinstance(name, str):
            self.name = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            self.arguments_parts.append(arguments)
        elif isinstance(arguments, Mapping):
            self.arguments_parts.append(dumps(arguments))

    # Build a complete tool call from the accumulated stream fragments.
    #
    # 根据累积的流式片段构建完整工具调用。
    def build(self, index: int) -> ToolCall:
        arguments_text = "".join(self.arguments_parts)
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}
        return ToolCall(
            id=self.id or f"tool-call-{index}",
            name=self.name,
            arguments=arguments,
        )


# Build the Mistral request payload from model settings, messages, and tools.
#
# 根据模型设置、消息和工具构建 Mistral 请求载荷。
def _build_mistral_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None,
    max_tokens: int | None,
    supports_images: bool = False,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "messages": [
            *_system_messages(system),
            *_messages_to_mistral(messages, supports_images=supports_images),
        ],
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = [_tool_to_mistral(tool) for tool in tools]
    if reasoning_effort is not None and reasoning_effort != "none":
        if _uses_reasoning_effort(model):
            payload["reasoning_effort"] = "high"
        else:
            payload["prompt_mode"] = "reasoning"
    return payload


# Convert an optional system prompt into Mistral message form.
#
# 将可选的系统提示词转换为 Mistral 消息格式。
def _system_messages(system: str) -> list[dict[str, JSONValue]]:
    return [{"role": "system", "content": system}] if system else []


# Convert agent messages to Mistral messages while preserving supported images.
#
# 将代理消息转换为 Mistral 消息，同时保留受支持的图片。
def _messages_to_mistral(
    messages: list[AgentMessage], *, supports_images: bool
) -> list[dict[str, JSONValue]]:
    converted: list[dict[str, JSONValue]] = []
    pending_tool_images: list[ImageContent] = []
    for message in messages:
        if pending_tool_images and not isinstance(message, ToolResultMessage):
            converted.append(_mistral_tool_image_message(pending_tool_images))
            pending_tool_images = []
        if isinstance(message, UserMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_USER_IMAGE_PLACEHOLDER,
            )
            if images:
                content: list[JSONValue] = []
                if text:
                    content.append({"type": "text", "text": text})
                content.extend(
                    {
                        "type": "image_url",
                        "image_url": f"data:{image.mime_type};base64,{image.data}",
                    }
                    for image in images
                )
                converted.append({"role": "user", "content": content})
            else:
                converted.append({"role": "user", "content": text})
            continue
        if isinstance(message, ToolResultMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_TOOL_IMAGE_PLACEHOLDER,
            )
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": portable_tool_call_id(message.tool_call_id),
                    "name": message.tool_name,
                    "content": text or ("(see attached image)" if images else "(no tool output)"),
                }
            )
            pending_tool_images.extend(images)
            continue
        converted.append(_message_to_mistral(message))
    if pending_tool_images:
        converted.append(_mistral_tool_image_message(pending_tool_images))
    return converted


# Wrap tool-result images in a Mistral user message.
#
# 将工具结果图片封装为 Mistral 用户消息。
def _mistral_tool_image_message(images: list[ImageContent]) -> dict[str, JSONValue]:
    content: list[JSONValue] = [{"type": "text", "text": "Attached image(s) from tool result:"}]
    content.extend(
        {
            "type": "image_url",
            "image_url": f"data:{image.mime_type};base64,{image.data}",
        }
        for image in images
    )
    return {"role": "user", "content": content}


# Convert one agent message into Mistral's chat message shape.
#
# 将一条代理消息转换为 Mistral 聊天消息结构。
def _message_to_mistral(message: AgentMessage) -> dict[str, JSONValue]:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.text}
    if isinstance(message, AssistantMessage):
        item: dict[str, JSONValue] = {"role": "assistant", "content": message.text}
        if message.thinking_text:
            item["reasoning_content"] = message.thinking_text
        if message.tool_calls:
            item["tool_calls"] = [
                _tool_call_to_mistral(tool_call) for tool_call in message.tool_calls
            ]
        return item
    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": portable_tool_call_id(message.tool_call_id),
            "name": message.tool_name,
            "content": message.text,
        }
    return _message_to_mistral(message_to_user(message))


# Convert an agent tool definition into Mistral's function-tool schema.
#
# 将代理工具定义转换为 Mistral 的函数工具模式。
def _tool_to_mistral(tool: AgentTool) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
            "strict": False,
        },
    }


# Convert a tool call into Mistral's serialized function-call shape.
#
# 将工具调用转换为 Mistral 的序列化函数调用结构。
def _tool_call_to_mistral(tool_call: ToolCall) -> dict[str, JSONValue]:
    return {
        "id": portable_tool_call_id(tool_call.id),
        "type": "function",
        "function": {"name": tool_call.name, "arguments": dumps(tool_call.arguments)},
    }


# Normalize a Mistral base URL so it ends with the API version path.
#
# 规范化 Mistral 基础 URL，使其以 API 版本路径结尾。
def _mistral_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


# Return whether the model accepts the reasoning_effort request field.
#
# 返回该模型是否接受 reasoning_effort 请求字段。
def _uses_reasoning_effort(model: str) -> bool:
    return model in {"mistral-small-2603", "mistral-small-latest", "mistral-medium-3.5"}


# Extract the data payload from one server-sent event line.
#
# 从一行服务器发送事件中提取 data 载荷。
def _parse_sse_line(line: str) -> str | None:
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


# Decode a JSON string only when its top-level value is an object.
#
# 仅当 JSON 字符串的顶层值为对象时才返回解码结果。
def _loads_object(value: str) -> dict[str, JSONValue] | None:
    try:
        loaded = loads(value)
    except JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


# Return the first valid completion choice from a response chunk.
#
# 从响应数据块中返回第一个有效的补全选项。
def _first_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    return choice if isinstance(choice, Mapping) else None


# Extract visible text fragments from a Mistral content delta.
#
# 从 Mistral 内容增量中提取可见文本片段。
def _content_deltas(delta: Mapping[str, Any]) -> list[str]:
    content = delta.get("content")
    if isinstance(content, str) and content:
        return [content]
    if not isinstance(content, list):
        return []
    output: list[str] = []
    for item in content:
        if isinstance(item, str) and item:
            output.append(item)
        elif isinstance(item, Mapping) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                output.append(text)
    return output


# Extract reasoning text fragments from a Mistral content delta.
#
# 从 Mistral 内容增量中提取推理文本片段。
def _thinking_deltas(delta: Mapping[str, Any]) -> list[str]:
    content = delta.get("content")
    if not isinstance(content, list):
        return []
    output: list[str] = []
    for item in content:
        if not isinstance(item, Mapping) or item.get("type") != "thinking":
            continue
        thinking = item.get("thinking")
        if isinstance(thinking, str) and thinking:
            output.append(thinking)
        elif isinstance(thinking, list):
            for part in thinking:
                if isinstance(part, Mapping):
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        output.append(text)
    return output


# Extract valid tool-call fragments from a Mistral delta.
#
# 从 Mistral 增量中提取有效的工具调用片段。
def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tool_calls = delta.get("tool_calls") or delta.get("toolCalls")
    if not isinstance(tool_calls, list):
        return []
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, Mapping)]
