"""OpenAI-compatible chat completions provider.

兼容 OpenAI 的聊天补全提供商。

Most OpenAI-compatible models are served over `/chat/completions`. Newer
reasoning models (e.g. ``gpt-5.5``/``gpt-5.4`` and the ``*-codex`` family)
reject the combination of function tools and ``reasoning_effort`` on that
endpoint and require ``/v1/responses`` instead. This adapter routes those
models to the Responses API at request time while leaving every other model on
the original chat-completions path unchanged.

大多数兼容 OpenAI 的模型通过 `/chat/completions` 提供服务。较新的推理模型
（例如 ``gpt-5.5``、``gpt-5.4`` 和 ``*-codex`` 系列）会拒绝在该端点上
同时使用函数工具和 ``reasoning_effort``，因此必须改用 ``/v1/responses``。
此适配器在请求时将这些模型路由到 Responses API，并让其他模型继续使用
原有的聊天补全路径。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from json import JSONDecodeError, dumps, loads
from typing import Any, Protocol

import httpx

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    AssistantMessageDiagnostic,
    ImageContent,
    ThinkingContent,
    ToolResultMessage,
    Usage,
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
    messages_have_images,
    text_and_images,
)
from tau_ai.env import OpenAICompatibleConfig
from tau_ai.events import AssistantMessageEvent
from tau_ai.http import create_async_client
from tau_ai.http_errors import provider_http_error_message
from tau_ai.openai_cache import is_direct_openai_url, openai_prompt_cache_key
from tau_ai.provider import CancellationToken
from tau_ai.retry import provider_retry_event, retry_delay_seconds, wait_for_retry
from tau_ai.stream import canonicalize_provider_stream
from tau_ai.tool_call_ids import portable_tool_call_id

# Models that reject function tools + reasoning_effort on /chat/completions and
# must use the /v1/responses endpoint instead.

# 拒绝在 /chat/completions 上同时使用函数工具和 reasoning_effort，因而必须
# 改用 /v1/responses 端点的模型。
_RESPONSES_ONLY_PREFIXES: tuple[str, ...] = ("gpt-5.5", "gpt-5.4")


def _use_responses_api(model: str) -> bool:
    """Return whether ``model`` must be served over the Responses API.

    返回 ``model`` 是否必须通过 Responses API 提供服务。
    """
    normalized = model.strip().lower()
    if "codex" in normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in _RESPONSES_ONLY_PREFIXES)


class OpenAICompatibleProvider:
    """Provider adapter for OpenAI-compatible `/chat/completions` APIs.

    兼容 OpenAI `/chat/completions` API 的提供商适配器。

    Models that require it are transparently served over `/v1/responses`.

    对有此要求的模型透明地改用 `/v1/responses` 提供服务。
    """

    # Initialize provider configuration and optional shared HTTP client ownership.

    # 初始化提供商配置，并记录可选共享 HTTP 客户端的所有权。
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

        如果底层 HTTP 客户端由此提供商创建，则将其关闭。
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

        将单次响应以兼容 Pi 的助手消息事件流形式输出。
        """
        raw = self._stream_provider_events(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
            session_id=session_id,
        )
        return canonicalize_provider_stream(
            raw,
            api=self._config.api,
            provider=getattr(self._config, "provider_name", "openai-compatible"),
            model=model,
            independent_channels=not (
                self._config.api == "openai-responses"
                or (self._config.infer_api_from_model and _use_responses_api(model))
            ),
        )

    def _stream_provider_events(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one model response as provider-neutral events.

        将单次模型响应以与具体提供商无关的事件流形式输出。
        """
        if self._config.api == "openai-responses" or (
            self._config.infer_api_from_model and _use_responses_api(model)
        ):
            return self._stream_responses(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                signal=signal,
                session_id=session_id,
            )
        return self._stream_chat_completions(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            signal=signal,
            session_id=session_id,
        )

    def _stream_chat_completions(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one chat completion response as provider-neutral events.

        将单次聊天补全响应以与具体提供商无关的事件流形式输出。
        """
        affinity_id = openai_prompt_cache_key(session_id)
        cache_key = self._prompt_cache_key(affinity_id)
        payload = _build_chat_payload(
            model=self._config.model_aliases.get(model, model),
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            reasoning_effort_parameter=self._config.reasoning_effort_parameter,
            thinking_format=self._config.thinking_format,
            compat=self._config.compat,
            max_tokens=self._config.max_tokens,
            include_reasoning_effort_none=self._config.include_reasoning_effort_none,
            supports_images=self._config.supports_images,
            prompt_cache_key=cache_key,
        )
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/chat/completions",
            payload=payload,
            parser_factory=_ChatStreamParser,
            session_id=affinity_id,
            session_affinity_format=self._session_affinity_format(responses=False),
            has_images=(self._config.supports_images and messages_have_images(messages)),
            signal=signal,
        )

    def _stream_responses(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Stream one `/v1/responses` response as provider-neutral events.

        将单次 `/v1/responses` 响应以与具体提供商无关的事件流形式输出。
        """
        affinity_id = openai_prompt_cache_key(session_id)
        cache_key = self._prompt_cache_key(affinity_id)
        payload = _build_responses_payload(
            model=self._config.model_aliases.get(model, model),
            system=system,
            messages=messages,
            tools=tools,
            reasoning_effort=self._config.reasoning_effort,
            max_tokens=self._config.max_tokens,
            supports_images=self._config.supports_images,
            prompt_cache_key=cache_key,
        )
        return self._stream(
            model=model,
            url=f"{self._config.base_url.rstrip('/')}/responses",
            payload=payload,
            parser_factory=_ResponsesStreamParser,
            session_id=affinity_id,
            session_affinity_format=self._session_affinity_format(responses=True),
            has_images=(self._config.supports_images and messages_have_images(messages)),
            signal=signal,
        )

    def _stream(
        self,
        *,
        model: str,
        url: str,
        payload: Mapping[str, JSONValue],
        parser_factory: Callable[[], _StreamParser],
        session_id: str | None = None,
        session_affinity_format: str | None = None,
        has_images: bool = False,
        signal: CancellationToken | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Run the shared streaming POST + retry envelope for a given endpoint.

        为指定端点运行共享的流式 POST 请求和重试封装。

        The per-endpoint differences (SSE chunk handling and final-message
        assembly) live in the ``_StreamParser`` produced by ``parser_factory``;
        everything else — HTTP, status/network retries, cancellation, the
        opening ``response_start`` event — is identical across endpoints.

        各端点的差异（SSE 数据块处理和最终消息组装）由
        ``parser_factory`` 生成的 ``_StreamParser`` 负责；其余 HTTP、状态码或
        网络重试、取消以及起始 ``response_start`` 事件在所有端点间完全一致。
        """

        # Execute one streaming request lifecycle and yield normalized provider events.

        # 执行一次流式请求生命周期，并逐个产出规范化的提供商事件。
        async def iterator() -> AsyncIterator[ProviderEvent]:
            client = self._get_client()
            api_key = self._config.api_key
            request_url = url
            headers = dict(self._config.headers or {})
            if self._config.provider_name == "github-copilot" and has_images:
                headers["Copilot-Vision-Request"] = "true"
            if self._config.credential_resolver is not None:
                auth = await self._config.credential_resolver()
                api_key = auth.api_key
                headers.update(auth.headers or {})
                if auth.base_url is not None:
                    endpoint = (
                        "/responses"
                        if url.rstrip("/").endswith("/responses")
                        else "/chat/completions"
                    )
                    request_url = f"{auth.base_url.rstrip('/')}{endpoint}"
            if not self._config.omit_authorization_header:
                has_authorization = any(key.casefold() == "authorization" for key in headers)
                if not has_authorization:
                    headers["Authorization"] = f"Bearer {api_key}"
            _apply_session_affinity_headers(headers, session_id, session_affinity_format)

            attempt = 0
            while True:
                parser = parser_factory()
                try:
                    async with client.stream(
                        "POST", request_url, json=payload, headers=headers
                    ) as response:
                        response_provider = _response_header_value(
                            response,
                            self._config.response_provider_header,
                        )
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
                                    data={
                                        "status_code": response.status_code,
                                        "body": body_text,
                                    },
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
                                data={
                                    "status_code": response.status_code,
                                    "body": body_text,
                                    "attempts": attempt + 1,
                                },
                                response_provider=response_provider,
                            )
                            return

                        yield ProviderResponseStartEvent(
                            model=model,
                            response_provider=response_provider,
                        )

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

                        if parser.fatal:
                            return
                        final_events = parser.finalize()
                        observer = self._config.response_headers_observer
                        if observer is not None:
                            try:
                                observer(dict(response.headers))
                            except Exception as exc:
                                # Observer reporting is also best-effort; response
                                # completion must never depend on metadata hooks.

                                # 观察器上报同样采用尽力而为策略；响应完成绝不能
                                # 依赖元数据钩子。
                                with suppress(Exception):
                                    _append_response_observer_diagnostic(final_events, exc)
                        for parser_event in final_events:
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
                            data={
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            },
                        )
                        attempt += 1
                        if not await wait_for_retry(delay, signal=signal):
                            return
                        continue
                    yield ProviderErrorEvent(
                        message=str(exc),
                        data={"attempts": attempt + 1},
                    )
                    return

        return iterator()

    # Return a cache-affinity key only when the provider supports it.

    # 仅在提供商支持时返回缓存亲和性键。
    def _prompt_cache_key(self, affinity_id: str | None) -> str | None:
        supports = self._config.compat.get("supportsPromptCacheKey")
        if supports is not True and not (
            supports is None and is_direct_openai_url(self._config.base_url)
        ):
            return None
        return affinity_id

    # Select the configured session-affinity header format for this endpoint.

    # 为当前端点选择配置的会话亲和性请求头格式。
    def _session_affinity_format(self, *, responses: bool) -> str | None:
        sends_headers = self._config.compat.get("sendSessionAffinityHeaders")
        if sends_headers is not True and not (
            sends_headers is None and responses and is_direct_openai_url(self._config.base_url)
        ):
            return None
        value = self._config.compat.get("sessionAffinityFormat")
        return value if isinstance(value, str) else "openai"

    # Lazily create and return the provider's reusable HTTP client.

    # 延迟创建并返回提供商可复用的 HTTP 客户端。
    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = create_async_client(timeout=self._config.timeout_seconds)
        return self._client

    # Decide whether the current failure remains eligible for retry.

    # 判断当前失败是否仍符合重试条件。
    def _should_retry(self, attempt: int, *, status_code: int | None = None) -> bool:
        if attempt >= self._config.max_retries:
            return False
        return status_code is None or _is_transient_status(status_code)


def _response_header_value(response: httpx.Response, header_name: str | None) -> str | None:
    """Return one normalized response metadata header when configured.

    在已配置时返回一个规范化的响应元数据请求头值。
    """
    if header_name is None:
        return None
    value = response.headers.get(header_name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


# Apply the selected session-affinity convention to outgoing headers.

# 按选定的会话亲和性约定设置出站请求头。
def _apply_session_affinity_headers(
    headers: dict[str, str],
    session_id: str | None,
    affinity_format: str | None,
) -> None:
    if session_id is None or affinity_format is None:
        return
    if affinity_format == "openrouter":
        headers["x-session-id"] = session_id
        return
    if affinity_format == "openai":
        headers["session_id"] = session_id


# Attach an observer failure diagnostic to the terminal response event.

# 将观察器失败诊断附加到终止响应事件。
def _append_response_observer_diagnostic(
    events: list[ProviderEvent],
    exc: Exception,
) -> None:
    for event in events:
        if isinstance(event, ProviderResponseEndEvent):
            diagnostic = AssistantMessageDiagnostic(
                type="response_headers_observer_error",
                details={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            event.message.diagnostics = [
                *(event.message.diagnostics or []),
                diagnostic,
            ]
            return


class _StreamParser(Protocol):
    """Per-endpoint SSE handler driven by the shared streaming envelope.

    由共享流式封装驱动、针对各端点的 SSE 处理器。
    """

    # True once any model output (text/thinking/tool args) has been emitted;
    # the envelope uses it to decide whether a mid-stream drop is retryable.

    # 一旦发出任何模型输出（文本、思考内容或工具参数）即为 True；封装层用它
    # 判断流中途断开是否可以重试。
    emitted_content: bool
    # True when the parser already emitted a terminal error event and the
    # envelope must not call finalize().

    # 当解析器已发出终止错误事件时为 True，此时封装层不得调用 finalize()。
    fatal: bool

    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        """Consume one SSE ``data:`` payload, returning (events, should_stop).

        消费一个 SSE ``data:`` 载荷，并返回（事件列表，是否停止）。
        """
        ...

    def finalize(self) -> list[ProviderEvent]:
        """Return the trailing tool-call and response-end events.

        返回末尾的工具调用事件和响应结束事件。
        """
        ...


class _ChatStreamParser:
    """Parser for OpenAI `/chat/completions` SSE chunks.

    OpenAI `/chat/completions` SSE 数据块解析器。
    """

    # Initialize state used to accumulate one streamed chat completion.

    # 初始化用于累积单次流式聊天补全的状态。
    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._thinking_signature: str | None = None
        self._tool_call_builders: dict[int, _ToolCallBuilder] = {}
        self._finish_reason: str | None = None
        self._usage: Usage | None = None

    # Parse one SSE payload and emit any immediately available provider events.

    # 解析一个 SSE 载荷，并发出当前可用的提供商事件。
    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        if event == "[DONE]":
            return [], True

        chunk = _loads_object(event)
        if chunk is None:
            self.fatal = True
            return [ProviderErrorEvent(message="Provider returned invalid JSON chunk")], True

        # The final usage chunk (from stream_options) carries usage at the top
        # level and often has empty choices.

        # 来自 stream_options 的最终用量数据块在顶层携带用量，且 choices 通常为空。
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, Mapping):
            self._usage = _parse_chunk_usage(chunk_usage)

        choice = _first_choice(chunk)
        if choice is None:
            return [], False

        # Fallback: some providers (e.g. Moonshot) attach usage to the choice
        # instead of the chunk. Matches Pi's per-chunk `!chunk.usage` guard: the
        # fallback applies whenever this chunk lacks top-level usage.

        # 回退逻辑：某些提供商（例如 Moonshot）将用量附加到 choice 而非数据块。
        # 这与 Pi 针对每个数据块的 `!chunk.usage` 守卫一致：只要当前数据块缺少
        # 顶层用量，就应用该回退逻辑。
        choice_usage = choice.get("usage")
        if not isinstance(chunk_usage, Mapping) and isinstance(choice_usage, Mapping):
            self._usage = _parse_chunk_usage(choice_usage)

        self._finish_reason = choice.get("finish_reason") or self._finish_reason
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            return [], False

        events: list[ProviderEvent] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.emitted_content = True
            self._content_parts.append(content)
            events.append(ProviderTextDeltaEvent(delta=content))

        thinking = _thinking_delta(delta)
        if thinking is not None:
            field_name, text = thinking
            self.emitted_content = True
            self._thinking_parts.append(text)
            self._thinking_signature = self._thinking_signature or field_name
            events.append(ProviderThinkingDeltaEvent(delta=text))

        for tool_call_delta in _tool_call_deltas(delta):
            self.emitted_content = True
            index = int(tool_call_delta.get("index", 0))
            builder = self._tool_call_builders.setdefault(index, _ToolCallBuilder())
            builder.add_delta(tool_call_delta)

        return events, False

    # Assemble accumulated content, tool calls, usage, and finish metadata.

    # 组装累积的内容、工具调用、用量和结束元数据。
    def finalize(self) -> list[ProviderEvent]:
        tool_calls = [
            builder.build(index) for index, builder in sorted(self._tool_call_builders.items())
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        content = assistant_content("".join(self._content_parts), tool_calls)
        if self._thinking_parts:
            content.insert(
                0,
                ThinkingContent(
                    thinking="".join(self._thinking_parts),
                    thinking_signature=self._thinking_signature,
                ),
            )
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(
                    content=content,
                    usage=self._usage or Usage(),
                ),
                finish_reason=self._finish_reason,
            )
        )
        return events


class _ResponsesStreamParser:
    """Parser for OpenAI `/v1/responses` SSE events.

    OpenAI `/v1/responses` SSE 事件解析器。
    """

    # Initialize state used to accumulate one streamed Responses API result.

    # 初始化用于累积单次 Responses API 流式结果的状态。
    def __init__(self) -> None:
        self.emitted_content = False
        self.fatal = False
        self._content_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._reasoning_items: dict[str, dict[str, JSONValue]] = {}
        self._tool_call_builders: dict[str, _ResponsesToolCallBuilder] = {}
        self._status: str | None = None
        self._usage: Usage | None = None

    # Route one Responses API SSE event into text, reasoning, tools, or termination.

    # 将一个 Responses API SSE 事件路由到文本、推理、工具或终止处理流程。
    def feed(self, event: str) -> tuple[list[ProviderEvent], bool]:
        # The Responses API has no [DONE] sentinel; it ends with a terminal
        # event (completed/incomplete/failed) handled below.

        # Responses API 没有 [DONE] 哨兵；它通过下方处理的终止事件
        # （completed、incomplete 或 failed）结束。
        if event == "[DONE]":
            return [], False

        chunk = _loads_object(event)
        if chunk is None:
            return [], False

        chunk_type = chunk.get("type")
        if not isinstance(chunk_type, str):
            return [], False

        if chunk_type in ("response.output_text.delta", "response.refusal.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self.emitted_content = True
                self._content_parts.append(delta)
                return [ProviderTextDeltaEvent(delta=delta)], False

        elif chunk_type in (
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        ):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self.emitted_content = True
                self._thinking_parts.append(delta)
                return [ProviderThinkingDeltaEvent(delta=delta)], False

        elif chunk_type == "response.reasoning_summary_part.done":
            if self._thinking_parts:
                separator = "\n\n"
                self._thinking_parts.append(separator)
                return [ProviderThinkingDeltaEvent(delta=separator)], False

        elif chunk_type == "response.output_item.added":
            item = chunk.get("item")
            _register_reasoning_item(self._reasoning_items, item)
            _register_responses_item(
                self._tool_call_builders,
                item,
                output_index=chunk.get("output_index"),
            )

        elif chunk_type == "response.function_call_arguments.delta":
            item_id = chunk.get("item_id")
            if isinstance(item_id, str):
                builder = self._tool_call_builders.setdefault(item_id, _ResponsesToolCallBuilder())
                builder.add_arguments_delta(chunk.get("delta"))
                self.emitted_content = True

        elif chunk_type == "response.function_call_arguments.done":
            item_id = chunk.get("item_id")
            if isinstance(item_id, str):
                builder = self._tool_call_builders.setdefault(item_id, _ResponsesToolCallBuilder())
                builder.set_final(arguments=chunk.get("arguments"))

        elif chunk_type == "response.output_item.done":
            item = chunk.get("item")
            _register_reasoning_item(self._reasoning_items, item)
            _finalize_responses_item(
                self._tool_call_builders,
                item,
                output_index=chunk.get("output_index"),
            )

        elif chunk_type in ("response.completed", "response.incomplete"):
            self._status = _responses_finish_reason(chunk)
            self._usage = _usage_from_responses_event(chunk) or self._usage
            return [], True

        elif chunk_type == "response.failed":
            self.fatal = True
            return [_responses_failure_event(chunk)], True

        elif chunk_type == "error":
            self.fatal = True
            return [
                ProviderErrorEvent(message=_responses_error_message(chunk), data={"event": chunk})
            ], True

        return [], False

    # Assemble accumulated Responses API items into terminal provider events.

    # 将累积的 Responses API 项组装为终止提供商事件。
    def finalize(self) -> list[ProviderEvent]:
        tool_calls = [
            builder.build(index)
            for index, builder in enumerate(_ordered_builders(self._tool_call_builders))
        ]
        events: list[ProviderEvent] = [
            ProviderToolCallEvent(tool_call=tool_call) for tool_call in tool_calls
        ]
        finish_reason = _normalize_finish_reason(self._status, has_tool_calls=bool(tool_calls))
        content = assistant_content("".join(self._content_parts), tool_calls)
        if self._thinking_parts:
            content.insert(
                0,
                ThinkingContent(
                    thinking="".join(self._thinking_parts),
                    thinking_signature=(
                        dumps(next(iter(self._reasoning_items.values())))
                        if self._reasoning_items
                        else None
                    ),
                ),
            )
        events.append(
            ProviderResponseEndEvent(
                message=AssistantMessage(
                    content=content,
                    usage=self._usage or Usage(),
                ),
                finish_reason=finish_reason,
            )
        )
        return events


class _ToolCallBuilder:
    # Initialize an accumulator for a chat-completions tool call.

    # 初始化聊天补全工具调用的累积器。
    def __init__(self) -> None:
        self.id = ""
        self.name = ""
        self.arguments_parts: list[str] = []

    # Merge one streamed tool-call delta into the accumulator.

    # 将一个流式工具调用增量合并到累积器中。
    def add_delta(self, delta: Mapping[str, Any]) -> None:
        call_id = delta.get("id")
        if isinstance(call_id, str):
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

    # Build the final tool call, preserving malformed arguments as raw text.

    # 构建最终工具调用，并将格式错误的参数保留为原始文本。
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


class _ResponsesToolCallBuilder:
    """Accumulates a streamed Responses-API ``function_call`` output item.

    累积流式 Responses API ``function_call`` 输出项。
    """

    # Initialize a Responses API tool-call accumulator with optional final metadata.

    # 使用可选的最终元数据初始化 Responses API 工具调用累积器。
    def __init__(
        self,
        *,
        call_id: str = "",
        name: str = "",
        output_index: int = 0,
    ) -> None:
        self.call_id = call_id
        self.name = name
        self.output_index = output_index
        self.arguments_parts: list[str] = []
        self.arguments_final: str | None = None

    # Append one streamed function-argument fragment when it is textual.

    # 当函数参数片段为文本时，将其追加到流式参数中。
    def add_arguments_delta(self, delta: object) -> None:
        if isinstance(delta, str):
            self.arguments_parts.append(delta)

    # Record authoritative fields received with a completed output item.

    # 记录随已完成输出项收到的权威字段。
    def set_final(
        self,
        *,
        call_id: str | None = None,
        name: str | None = None,
        arguments: object = None,
        output_index: int | None = None,
    ) -> None:
        if call_id:
            self.call_id = call_id
        if name:
            self.name = name
        if isinstance(arguments, str):
            self.arguments_final = arguments
        if output_index is not None:
            self.output_index = output_index

    # Build the final portable tool call from final or accumulated arguments.

    # 使用最终参数或累积参数构建可移植的最终工具调用。
    def build(self, index: int) -> ToolCall:
        arguments_text = (
            self.arguments_final
            if self.arguments_final is not None
            else "".join(self.arguments_parts)
        )
        arguments = _loads_object(arguments_text) if arguments_text else {}
        if arguments is None:
            arguments = {"_raw_arguments": arguments_text}

        return ToolCall(
            id=self.call_id or f"tool-call-{index}",
            name=self.name,
            arguments=arguments,
        )


# Build a chat-completions request payload from Tau messages and provider options.

# 根据 Tau 消息和提供商选项构建聊天补全请求载荷。
def _build_chat_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
    reasoning_effort_parameter: str = "reasoning_effort",
    thinking_format: str = "openai",
    compat: Mapping[str, JSONValue] | None = None,
    max_tokens: int | None = None,
    include_reasoning_effort_none: bool = False,
    supports_images: bool = False,
    prompt_cache_key: str | None = None,
) -> dict[str, JSONValue]:
    resolved_compat = dict(compat or {})
    supports_store = bool(resolved_compat.get("supportsStore", True))
    supports_usage = bool(resolved_compat.get("supportsUsageInStreaming", True))
    supports_reasoning_effort = bool(resolved_compat.get("supportsReasoningEffort", True))
    max_tokens_field = _string_compat(
        resolved_compat.get("maxTokensField"), default="max_completion_tokens"
    )
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        "messages": [
            _system_message(system),
            *_messages_to_openai_chat(messages, supports_images=supports_images),
        ],
    }
    if prompt_cache_key is not None:
        payload["prompt_cache_key"] = prompt_cache_key
    if supports_usage:
        payload["stream_options"] = {"include_usage": True}
    if supports_store:
        payload["store"] = False
    if max_tokens is not None:
        payload["max_tokens" if max_tokens_field == "max_tokens" else "max_completion_tokens"] = (
            max_tokens
        )
    openrouter_provider = resolved_compat.get("openrouterProvider")
    if isinstance(openrouter_provider, dict):
        payload["provider"] = openrouter_provider
    _apply_chat_reasoning(
        payload,
        reasoning_effort=(
            reasoning_effort if supports_reasoning_effort or thinking_format == "zai" else None
        ),
        reasoning_effort_parameter=reasoning_effort_parameter,
        thinking_format=thinking_format,
        include_reasoning_effort_none=include_reasoning_effort_none,
        supports_reasoning_effort=supports_reasoning_effort,
    )
    if tools:
        payload["tools"] = [_tool_to_openai(tool) for tool in tools]
        if resolved_compat.get("zaiToolStream") is True:
            payload["tool_stream"] = True
    return payload


# Apply provider-specific reasoning controls to a chat-completions payload.

# 将提供商特定的推理控制参数应用到聊天补全载荷。
def _apply_chat_reasoning(
    payload: dict[str, JSONValue],
    *,
    reasoning_effort: str | None,
    reasoning_effort_parameter: str,
    thinking_format: str,
    include_reasoning_effort_none: bool,
    supports_reasoning_effort: bool = True,
) -> None:
    reasoning_enabled = reasoning_effort is not None and reasoning_effort != "none"
    if thinking_format == "zai":
        # Z.AI's OpenAI-compatible API uses the provider-specific ``thinking``
        # object for every GLM model.  Only GLM-5.2+ accepts the separate
        # reasoning_effort field, so keep that decision model-specific via
        # supportsReasoningEffort instead of dropping the logical toggle.

        # Z.AI 的 OpenAI 兼容 API 对所有 GLM 模型都使用提供商特定的
        # ``thinking`` 对象。只有 GLM-5.2 及以上版本接受独立的
        # reasoning_effort 字段，因此通过 supportsReasoningEffort 保持按模型
        # 决策，而不是丢弃逻辑开关。
        payload["thinking"] = {"type": "enabled" if reasoning_enabled else "disabled"}
        if supports_reasoning_effort and reasoning_enabled:
            payload["reasoning_effort"] = reasoning_effort
        return
    if thinking_format == "qwen":
        payload["enable_thinking"] = reasoning_enabled
        return
    if thinking_format == "qwen-chat-template":
        payload["chat_template_kwargs"] = {
            "enable_thinking": reasoning_enabled,
            "preserve_thinking": True,
        }
        return
    if thinking_format == "deepseek":
        payload["thinking"] = {"type": "enabled" if reasoning_enabled else "disabled"}
        if reasoning_enabled:
            payload["reasoning_effort"] = reasoning_effort
        return
    if thinking_format == "openrouter" or reasoning_effort_parameter == "reasoning.effort":
        if reasoning_enabled:
            payload["reasoning"] = {"effort": reasoning_effort}
        elif include_reasoning_effort_none:
            payload["reasoning"] = {"effort": "none"}
        return
    if thinking_format == "together":
        payload["reasoning"] = {"enabled": reasoning_enabled}
        if reasoning_enabled:
            payload["reasoning_effort"] = reasoning_effort
        return
    if reasoning_enabled or include_reasoning_effort_none:
        payload["reasoning_effort"] = reasoning_effort or "none"


# Return a non-empty string compatibility option or its default.

# 返回非空字符串兼容性选项，否则返回默认值。
def _string_compat(value: object, *, default: str) -> str:
    return value if isinstance(value, str) and value else default


# Build a Responses API request payload from Tau messages and provider options.

# 根据 Tau 消息和提供商选项构建 Responses API 请求载荷。
def _build_responses_payload(
    *,
    model: str,
    system: str,
    messages: list[AgentMessage],
    tools: list[AgentTool],
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    supports_images: bool = False,
    prompt_cache_key: str | None = None,
) -> dict[str, JSONValue]:
    payload: dict[str, JSONValue] = {
        "model": model,
        "stream": True,
        # Stay stateless: the full transcript is resent every turn, so there is
        # no need for server-side retention. ``store: false`` also keeps the
        # path usable for zero-data-retention orgs, which reject ``store: true``.

        # 保持无状态：每轮都会重新发送完整对话记录，因此无需服务端保留。
        # ``store: false`` 也使该路径可用于拒绝 ``store: true`` 的零数据保留组织。
        "store": False,
        "instructions": system,
        "input": _messages_to_responses_input(messages, supports_images=supports_images),
    }
    if prompt_cache_key is not None:
        payload["prompt_cache_key"] = prompt_cache_key
    if max_tokens is not None:
        payload["max_output_tokens"] = max_tokens
    effort = _normalize_responses_effort(reasoning_effort)
    if effort is not None:
        # ``summary: auto`` streams ``response.reasoning_summary_text.delta``
        # events so the agent's thinking is visible, mirroring the reasoning
        # deltas surfaced on the chat-completions path.

        # ``summary: auto`` 会流式输出 ``response.reasoning_summary_text.delta``
        # 事件，使智能体的思考过程可见，与聊天补全路径呈现的推理增量保持一致。
        payload["reasoning"] = {"effort": effort, "summary": "auto"}
    if tools:
        payload["tools"] = [_tool_to_responses(tool) for tool in tools]
    return payload


def _normalize_responses_effort(reasoning_effort: str | None) -> str | None:
    """Map an internal reasoning level to a Responses-API effort, or drop it.

    将内部推理级别映射为 Responses API 的 effort 值，或将其舍弃。
    """
    if reasoning_effort is None:
        return None
    normalized = reasoning_effort.strip().lower()
    if normalized in ("", "none"):
        return None
    return normalized


# Convert Tau conversation messages into Responses API input items.

# 将 Tau 对话消息转换为 Responses API 输入项。
def _messages_to_responses_input(
    messages: list[AgentMessage], *, supports_images: bool = False
) -> list[JSONValue]:
    items: list[JSONValue] = []
    for message in messages:
        if isinstance(message, UserMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_USER_IMAGE_PLACEHOLDER,
            )
            if images:
                content: list[JSONValue] = []
                if text:
                    content.append({"type": "input_text", "text": text})
                content.extend(_openai_input_image(image) for image in images)
                items.append({"role": "user", "content": content})
            else:
                items.append({"role": "user", "content": text})
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ThinkingContent) and block.thinking_signature:
                    try:
                        reasoning_item = loads(block.thinking_signature)
                    except (TypeError, ValueError):
                        reasoning_item = None
                    if isinstance(reasoning_item, dict):
                        items.append(reasoning_item)
            if message.text:
                items.append({"role": "assistant", "content": message.text})
            for tool_call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": portable_tool_call_id(tool_call.id),
                        "name": tool_call.name,
                        "arguments": dumps(tool_call.arguments),
                    }
                )
        elif isinstance(message, ToolResultMessage):
            text, images = text_and_images(
                message.content,
                supports_images=supports_images,
                image_placeholder=NON_VISION_TOOL_IMAGE_PLACEHOLDER,
            )
            output: JSONValue
            if images:
                output_parts: list[JSONValue] = []
                if text:
                    output_parts.append({"type": "input_text", "text": text})
                output_parts.extend(_openai_input_image(image) for image in images)
                output = output_parts
            else:
                output = text or "(no tool output)"
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": portable_tool_call_id(message.tool_call_id),
                    "output": output,
                }
            )
    return items


# Encode one image as a Responses API input-image item.

# 将一张图片编码为 Responses API 输入图片项。
def _openai_input_image(image: ImageContent) -> dict[str, JSONValue]:
    return {
        "type": "input_image",
        "detail": "auto",
        "image_url": f"data:{image.mime_type};base64,{image.data}",
    }


# Convert a Tau agent tool into a Responses API function definition.

# 将 Tau 智能体工具转换为 Responses API 函数定义。
def _tool_to_responses(tool: AgentTool) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.input_schema),
    }


# Store a reasoning output item by identifier for later replay metadata.

# 按标识符存储推理输出项，供后续重放元数据使用。
def _register_reasoning_item(
    items: dict[str, dict[str, JSONValue]],
    item: object,
) -> None:
    if not isinstance(item, Mapping) or item.get("type") != "reasoning":
        return
    item_id = item.get("id")
    if isinstance(item_id, str):
        items[item_id] = dict(item)


# Register an added Responses API function-call item with its builder.

# 将新增的 Responses API 函数调用项注册到对应构建器。
def _register_responses_item(
    builders: dict[str, _ResponsesToolCallBuilder],
    item: object,
    *,
    output_index: object,
) -> None:
    if not isinstance(item, Mapping) or item.get("type") != "function_call":
        return
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return
    raw_arguments = item.get("arguments")
    builder = builders.setdefault(item_id, _ResponsesToolCallBuilder())
    builder.set_final(
        call_id=_str_or_none(item.get("call_id")),
        name=_str_or_none(item.get("name")),
        arguments=raw_arguments if isinstance(raw_arguments, str) and raw_arguments else None,
        output_index=_int_or_none(output_index),
    )


# Merge a completed Responses API function-call item into its builder.

# 将已完成的 Responses API 函数调用项合并到对应构建器。
def _finalize_responses_item(
    builders: dict[str, _ResponsesToolCallBuilder],
    item: object,
    *,
    output_index: object,
) -> None:
    if not isinstance(item, Mapping) or item.get("type") != "function_call":
        return
    item_id = item.get("id")
    if not isinstance(item_id, str):
        return
    builder = builders.setdefault(item_id, _ResponsesToolCallBuilder())
    builder.set_final(
        call_id=_str_or_none(item.get("call_id")),
        name=_str_or_none(item.get("name")),
        arguments=item.get("arguments"),
        output_index=_int_or_none(output_index),
    )


# Return tool-call builders ordered by their response output index.

# 按响应输出索引返回有序的工具调用构建器。
def _ordered_builders(
    builders: dict[str, _ResponsesToolCallBuilder],
) -> list[_ResponsesToolCallBuilder]:
    return [
        builder for _, builder in sorted(builders.items(), key=lambda pair: pair[1].output_index)
    ]


# Extract the response status used as the provisional finish reason.

# 提取用作暂定结束原因的响应状态。
def _responses_finish_reason(chunk: Mapping[str, Any]) -> str | None:
    response = chunk.get("response")
    if isinstance(response, Mapping):
        status = response.get("status")
        if isinstance(status, str):
            return status
    return None


def _normalize_finish_reason(status: str | None, *, has_tool_calls: bool) -> str:
    """Map a Responses-API status to chat-completions-style finish reasons.

    将 Responses API 状态映射为聊天补全风格的结束原因。
    """
    if has_tool_calls:
        return "tool_calls"
    if status == "incomplete":
        return "length"
    return "stop"


# Convert a failed Responses API event into a provider error event.

# 将失败的 Responses API 事件转换为提供商错误事件。
def _responses_failure_event(chunk: Mapping[str, Any]) -> ProviderErrorEvent:
    message = "Provider response failed"
    response = chunk.get("response")
    if isinstance(response, Mapping):
        error = response.get("error")
        if isinstance(error, Mapping):
            error_message = error.get("message")
            if isinstance(error_message, str) and error_message:
                message = error_message
    return ProviderErrorEvent(message=message, data={"event": dict(chunk)})


# Extract the best available message from a Responses API error event.

# 从 Responses API 错误事件中提取最合适的消息。
def _responses_error_message(chunk: Mapping[str, Any]) -> str:
    message = chunk.get("message")
    if isinstance(message, str) and message:
        return message
    error = chunk.get("error")
    if isinstance(error, Mapping):
        nested = error.get("message")
        if isinstance(nested, str) and nested:
            return nested
    return "Provider stream error"


# Return a non-empty string value, otherwise None.

# 返回非空字符串值，否则返回 None。
def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


# Wrap system instructions as an OpenAI chat message.

# 将系统指令封装为 OpenAI 聊天消息。
def _system_message(system: str) -> dict[str, JSONValue]:
    return {"role": "system", "content": system}


# Convert Tau messages into OpenAI chat-completions messages.

# 将 Tau 消息转换为 OpenAI 聊天补全消息。
def _messages_to_openai_chat(
    messages: list[AgentMessage], *, supports_images: bool
) -> list[dict[str, JSONValue]]:
    converted: list[dict[str, JSONValue]] = []
    pending_tool_images: list[ImageContent] = []
    for message in messages:
        if pending_tool_images and not isinstance(message, ToolResultMessage):
            converted.append(_openai_tool_image_message(pending_tool_images))
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
                        "image_url": {"url": f"data:{image.mime_type};base64,{image.data}"},
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
        converted.append(_message_to_openai(message))
    if pending_tool_images:
        converted.append(_openai_tool_image_message(pending_tool_images))
    return converted


# Wrap tool-result images in a user message accepted by chat completions.

# 将工具结果图片封装为聊天补全可接受的用户消息。
def _openai_tool_image_message(images: list[ImageContent]) -> dict[str, JSONValue]:
    content: list[JSONValue] = [{"type": "text", "text": "Attached image(s) from tool result:"}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image.mime_type};base64,{image.data}"},
        }
        for image in images
    )
    return {"role": "user", "content": content}


# Convert one Tau message into its OpenAI chat representation.

# 将一条 Tau 消息转换为对应的 OpenAI 聊天表示。
def _message_to_openai(message: AgentMessage) -> dict[str, JSONValue]:
    if isinstance(message, UserMessage):
        return {"role": "user", "content": message.text}

    if isinstance(message, AssistantMessage):
        item: dict[str, JSONValue] = {"role": "assistant", "content": message.text}
        thinking = [block for block in message.content if isinstance(block, ThinkingContent)]
        if thinking:
            signature = thinking[0].thinking_signature or "reasoning_content"
            if signature in {"reasoning_content", "reasoning", "thinking"}:
                item[signature] = "".join(block.thinking for block in thinking)
        if message.tool_calls:
            item["tool_calls"] = [
                _tool_call_to_openai(tool_call) for tool_call in message.tool_calls
            ]
        return item

    if isinstance(message, ToolResultMessage):
        return {
            "role": "tool",
            "tool_call_id": portable_tool_call_id(message.tool_call_id),
            "name": message.tool_name,
            "content": message.text,
        }
    return _message_to_openai(message_to_user(message))


# Convert a Tau agent tool into an OpenAI chat function definition.

# 将 Tau 智能体工具转换为 OpenAI 聊天函数定义。
def _tool_to_openai(tool: AgentTool) -> dict[str, JSONValue]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


# Convert a Tau tool call into an OpenAI chat tool-call object.

# 将 Tau 工具调用转换为 OpenAI 聊天工具调用对象。
def _tool_call_to_openai(tool_call: ToolCall) -> dict[str, JSONValue]:
    return {
        "id": portable_tool_call_id(tool_call.id),
        "type": "function",
        "function": {
            "name": tool_call.name,
            "arguments": dumps(tool_call.arguments),
        },
    }


# Extract the payload from one SSE data line.

# 从一行 SSE 数据中提取载荷。
def _parse_sse_line(line: str) -> str | None:
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    return line.removeprefix("data:").strip()


# Parse a JSON string only when its top-level value is an object.

# 仅当 JSON 字符串的顶层值是对象时才返回解析结果。
def _loads_object(value: str) -> dict[str, JSONValue] | None:
    try:
        loaded = loads(value)
    except JSONDecodeError:
        return None
    if isinstance(loaded, dict):
        return loaded
    return None


# Return the first valid choice object from a chat-completions chunk.

# 从聊天补全数据块中返回第一个有效 choice 对象。
def _first_choice(chunk: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    return choice


# Return an integer value while excluding booleans, otherwise zero.

# 返回整数值并排除布尔值，否则返回零。
def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# Return an integer value while excluding booleans, otherwise None.

# 返回整数值并排除布尔值，否则返回 None。
def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_chunk_usage(raw: Mapping[str, Any]) -> Usage:
    """Parse an OpenAI-compatible ``usage`` payload into a Usage.

    将兼容 OpenAI 的 ``usage`` 载荷解析为 Usage。

    Ports Pi's openai-completions.ts parseChunkUsage: ``cached_tokens`` are
    cache reads, writes are subtracted from the prompt to leave the fresh input,
    and ``completion_tokens`` already includes reasoning tokens. Cost is left
    unset (None) because Tau has no per-model pricing table.

    移植自 Pi 的 openai-completions.ts parseChunkUsage：``cached_tokens``
    计为缓存读取量，从提示词令牌中减去写入量以得到新增输入量，而
    ``completion_tokens`` 已包含推理令牌。由于 Tau 没有按模型划分的价格表，
    成本保持未设置（None）。
    """
    prompt_tokens = _int_or_zero(raw.get("prompt_tokens"))
    prompt_details = raw.get("prompt_tokens_details")
    cached_tokens: int | None = None
    cache_write = 0
    if isinstance(prompt_details, Mapping):
        cached_tokens = _int_or_none(prompt_details.get("cached_tokens"))
        cache_write = _int_or_zero(prompt_details.get("cache_write_tokens"))
    # Nullish fallback, matching Pi's `cached_tokens ?? prompt_cache_hit_tokens
    # ?? 0` (DeepSeek reports cache hits in prompt_cache_hit_tokens): a reported
    # 0 does not fall through.

    # 空值回退，与 Pi 的 `cached_tokens ?? prompt_cache_hit_tokens ?? 0` 一致
    # （DeepSeek 在 prompt_cache_hit_tokens 中报告缓存命中）：已报告的 0 不会
    # 继续回退。
    if cached_tokens is None:
        cached_tokens = _int_or_none(raw.get("prompt_cache_hit_tokens"))
    cache_read = cached_tokens or 0
    fresh_input = max(0, prompt_tokens - cache_read - cache_write)
    output = _int_or_zero(raw.get("completion_tokens"))
    reasoning = None
    completion_details = raw.get("completion_tokens_details")
    if isinstance(completion_details, Mapping):
        reasoning = _int_or_zero(completion_details.get("reasoning_tokens"))
    return Usage(
        input=fresh_input,
        output=output,
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
        total_tokens=fresh_input + output + cache_read + cache_write,
    )


def _usage_from_responses_event(chunk: Mapping[str, Any]) -> Usage | None:
    """Parse billed usage from a `/v1/responses` terminal event.

    从 `/v1/responses` 终止事件中解析计费用量。

    Mirrors the Codex adapter's ``_usage_from_response``: cache reads and
    writes are subtracted from ``input_tokens`` to leave fresh input. Cost is
    left unset because Tau has no per-model pricing table.

    与 Codex 适配器的 ``_usage_from_response`` 保持一致：从
    ``input_tokens`` 中减去缓存读取和写入量，以得到新增输入量。由于 Tau
    没有按模型划分的价格表，成本保持未设置。
    """
    response = chunk.get("response")
    if not isinstance(response, Mapping):
        return None
    raw = response.get("usage")
    if not isinstance(raw, Mapping):
        return None
    input_details = raw.get("input_tokens_details")
    cache_read = (
        _int_or_zero(input_details.get("cached_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    cache_write = (
        _int_or_zero(input_details.get("cache_write_tokens"))
        if isinstance(input_details, Mapping)
        else 0
    )
    output_details = raw.get("output_tokens_details")
    # Leave reasoning None (not 0) when the provider reports no breakdown,
    # honoring the "None = not reported" contract on Usage.

    # 当提供商未报告细分数据时，将 reasoning 保持为 None（而不是 0），遵循
    # Usage 中“None 表示未报告”的约定。
    reasoning = (
        _int_or_zero(output_details.get("reasoning_tokens"))
        if isinstance(output_details, Mapping)
        else None
    )
    return Usage(
        input=max(0, _int_or_zero(raw.get("input_tokens")) - cache_read - cache_write),
        output=_int_or_zero(raw.get("output_tokens")),
        cache_read=cache_read,
        cache_write=cache_write,
        reasoning=reasoning,
        total_tokens=_int_or_zero(raw.get("total_tokens")),
    )


# Extract valid tool-call delta objects from one chat delta.

# 从一个聊天增量中提取有效的工具调用增量对象。
def _tool_call_deltas(delta: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [tool_call for tool_call in tool_calls if isinstance(tool_call, Mapping)]


# Find the first supported reasoning-text field in a chat delta.

# 在聊天增量中查找第一个受支持的推理文本字段。
def _thinking_delta(delta: Mapping[str, Any]) -> tuple[str, str] | None:
    for field_name in ("reasoning_content", "reasoning", "thinking"):
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            return field_name, value
    return None


# Return whether an HTTP status represents a transient provider failure.

# 返回 HTTP 状态码是否表示临时性的提供商故障。
def _is_transient_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500
