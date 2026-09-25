"""Pi-compatible provider-neutral content and transcript message models."""

# 与 Pi 兼容且不绑定提供商的内容与会话消息模型。

from __future__ import annotations

from collections.abc import Iterable
from time import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau_agent.types import JSONValue


# Convert a snake-case field name to the camel-case wire format.
#
# 将蛇形字段名转换为线上的驼峰格式。
def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def current_timestamp_ms() -> int:
    """Return the current Unix timestamp in milliseconds."""

    # 返回以毫秒为单位的当前 Unix 时间戳。
    return int(time() * 1000)


class WireModel(BaseModel):
    """Strict model with Python field names and Pi-compatible JSON aliases."""

    # 使用 Python 字段名及与 Pi 兼容的 JSON 别名的严格模型。

    model_config = ConfigDict(
        extra="forbid",
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        alias_generator=_to_camel,
    )


class UsageCost(WireModel):
    """Billed response cost in USD."""

    # 以美元计费的响应成本。

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0
    total: float = 0.0


class Usage(WireModel):
    """Provider-reported token usage for one assistant response."""

    # 提供商报告的单次助手响应令牌用量。

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cache_write_1h: int | None = None
    reasoning: int | None = None
    total_tokens: int = 0
    cost: UsageCost = UsageCost()


def sum_usage(usages: Iterable[Usage]) -> Usage:
    """Return the field-wise total for one or more provider requests."""

    # 按字段汇总一个或多个提供商请求的用量。
    items = tuple(usages)

    # Sum an optional usage field while preserving an all-missing result.
    #
    # 汇总可选用量字段，并在所有值均缺失时保留缺失结果。
    def optional_total(field: Literal["cache_write_1h", "reasoning"]) -> int | None:
        values = [getattr(item, field) for item in items]
        return (
            sum(value for value in values if value is not None)
            if any(value is not None for value in values)
            else None
        )

    return Usage(
        input=sum(item.input for item in items),
        output=sum(item.output for item in items),
        cache_read=sum(item.cache_read for item in items),
        cache_write=sum(item.cache_write for item in items),
        cache_write_1h=optional_total("cache_write_1h"),
        reasoning=optional_total("reasoning"),
        total_tokens=sum(item.total_tokens for item in items),
        cost=UsageCost(
            input=sum(item.cost.input for item in items),
            output=sum(item.cost.output for item in items),
            cache_read=sum(item.cost.cache_read for item in items),
            cache_write=sum(item.cost.cache_write for item in items),
            total=sum(item.cost.total for item in items),
        ),
    )


class ResponseTiming(WireModel):
    """Monotonic request durations for one assistant response."""

    # 单次助手响应的单调时钟请求耗时。

    time_to_first_output_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int = Field(ge=0)


class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str
    text_signature: str | None = None


class ThinkingContent(WireModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    thinking_signature: str | None = None
    redacted: bool = False


class ImageContent(WireModel):
    type: Literal["image"] = "image"
    data: str
    mime_type: str


class ToolCall(WireModel):
    """A tool call content block requested by the assistant."""

    # 助手请求的工具调用内容块。

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)
    thought_signature: str | None = None


type UserContent = str | list[TextContent | ImageContent]
type AssistantContent = TextContent | ThinkingContent | ToolCall
type ToolResultContent = TextContent | ImageContent


class UserMessage(WireModel):
    role: Literal["user"] = "user"
    content: UserContent
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    # Return the visible text carried by the user message.
    #
    # 返回用户消息携带的可见文本。
    def text(self) -> str:
        return content_text(self.content)


class AssistantDiagnosticError(WireModel):
    name: str | None = None
    message: str
    stack: str | None = None
    code: str | int | None = None


class AssistantMessageDiagnostic(WireModel):
    type: str
    timestamp: int = Field(default_factory=current_timestamp_ms)
    error: AssistantDiagnosticError | None = None
    details: dict[str, JSONValue] | None = None


StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]


class AssistantMessage(WireModel):
    """A Pi-compatible assistant message with ordered content blocks."""

    # 包含有序内容块且与 Pi 兼容的助手消息。

    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list)
    api: str = "unknown"
    provider: str = "unknown"
    model: str = "unknown"
    response_model: str | None = None
    response_provider: str | None = None
    response_id: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    usage: Usage = Usage()
    timing: ResponseTiming | None = None
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        """Accept a string only as a Python construction convenience.

        仅为方便在 Python 中构造对象而接受字符串。

        The stored model and serialized protocol are always block based. This
        keeps provider and test construction concise without creating a second
        message representation.

        存储模型和序列化协议始终以内容块为基础。这样既能简化提供商与测试中的
        对象构造，又不会引入第二种消息表示形式。
        """
        # Normalize shorthand input before Pydantic validates the canonical model.
        #
        # 在 Pydantic 校验规范模型前，将简写输入规范化。
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        usage = data.get("usage")
        if usage is None:
            data["usage"] = Usage()
        return data

    @property
    # Concatenate all visible text blocks in their original order.
    #
    # 按原始顺序拼接所有可见文本块。
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))

    @property
    # Concatenate all thinking blocks in their original order.
    #
    # 按原始顺序拼接所有思考内容块。
    def thinking_text(self) -> str:
        return "".join(
            block.thinking for block in self.content if isinstance(block, ThinkingContent)
        )

    @property
    # Return the ordered tool calls from the assistant content.
    #
    # 返回助手内容中按顺序排列的工具调用。
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCall))


class ToolResultMessage(WireModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent] = Field(default_factory=list)
    details: JSONValue = None
    added_tool_names: list[str] | None = None
    is_error: bool = False
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    # Normalize string shorthand into canonical tool-result content blocks.
    #
    # 将字符串简写规范化为标准工具结果内容块。
    def _normalize_convenient_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    # Return the visible text carried by the tool result.
    #
    # 返回工具结果携带的可见文本。
    def text(self) -> str:
        return content_text(self.content)


class BashExecutionMessage(WireModel):
    role: Literal["bashExecution"] = "bashExecution"
    command: str
    output: str
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)
    exclude_from_context: bool = False


class CustomMessage(WireModel):
    role: Literal["custom"] = "custom"
    custom_type: str
    content: UserContent
    display: bool = True
    details: JSONValue = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    # Return the visible text carried by the custom message.
    #
    # 返回自定义消息携带的可见文本。
    def text(self) -> str:
        return content_text(self.content)


class BranchSummaryMessage(WireModel):
    role: Literal["branchSummary"] = "branchSummary"
    summary: str
    from_id: str
    timestamp: int = Field(default_factory=current_timestamp_ms)


class CompactionSummaryMessage(WireModel):
    role: Literal["compactionSummary"] = "compactionSummary"
    summary: str
    tokens_before: int
    timestamp: int = Field(default_factory=current_timestamp_ms)


type AgentMessage = Annotated[
    UserMessage
    | AssistantMessage
    | ToolResultMessage
    | BashExecutionMessage
    | CustomMessage
    | BranchSummaryMessage
    | CompactionSummaryMessage,
    Field(discriminator="role"),
]


def assistant_content(
    text: str,
    tool_calls: list[ToolCall] | tuple[ToolCall, ...] = (),
) -> list[AssistantContent]:
    """Build canonical ordered assistant blocks from parser accumulators."""

    # 根据解析器累积结果构建标准的有序助手内容块。
    blocks: list[AssistantContent] = [TextContent(text=text)] if text else []
    blocks.extend(tool_calls)
    return blocks


def content_text(content: str | list[Any]) -> str:
    """Return visible text from string or text/image content."""

    # 从字符串或文本/图像内容中返回可见文本。
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, TextContent))


def message_to_user(message: AgentMessage) -> UserMessage:
    """Convert custom/session-only messages to provider-compatible user context."""

    # 将自定义或仅用于会话的消息转换为提供商兼容的用户上下文。
    return UserMessage(content=message_text(message), timestamp=message.timestamp)


def message_text(message: AgentMessage) -> str:
    """Return the user-visible text represented by an agent message."""

    # 返回智能体消息所表示的用户可见文本。
    if isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage, CustomMessage)):
        return message.text
    if isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
        return message.summary
    if isinstance(message, BashExecutionMessage):
        return message.output
    return ""
