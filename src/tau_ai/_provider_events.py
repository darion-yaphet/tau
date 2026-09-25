"""Provider-neutral streaming events emitted by model adapters.

模型适配器发出的、与提供者无关的流式事件。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from tau_agent.messages import AssistantMessage
from tau_agent.tools import ToolCall
from tau_agent.types import JSONValue


class ProviderResponseStartEvent(BaseModel):
    """The provider has started a model response.

    模型提供者已开始生成响应。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["response_start"] = "response_start"
    model: str
    response_provider: str | None = None


class ProviderRetryEvent(BaseModel):
    """The provider adapter is retrying a transient request failure.

    模型提供者适配器正在重试暂时性的请求失败。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["retry"] = "retry"
    attempt: int
    max_attempts: int
    delay_seconds: float
    message: str
    data: dict[str, JSONValue] | None = None


class ProviderTextDeltaEvent(BaseModel):
    """A streamed text fragment from the provider.

    模型提供者流式返回的一段文本。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ProviderThinkingDeltaEvent(BaseModel):
    """A streamed thinking/reasoning fragment from the provider.

    模型提供者流式返回的一段思考或推理内容。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["thinking_delta"] = "thinking_delta"
    delta: str


class ProviderToolCallEvent(BaseModel):
    """A complete tool call requested by the model.

    模型请求的一次完整工具调用。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"] = "tool_call"
    tool_call: ToolCall


class ProviderResponseEndEvent(BaseModel):
    """The provider has completed a model response.

    模型提供者已完成响应。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["response_end"] = "response_end"
    message: AssistantMessage
    finish_reason: str | None = None


class ProviderErrorEvent(BaseModel):
    """A provider-level error that can be surfaced by the agent layer.

    可由代理层向上传递的模型提供者级错误。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["error"] = "error"
    message: str
    data: dict[str, JSONValue] | None = None
    response_provider: str | None = None


type ProviderEvent = (
    ProviderResponseStartEvent
    | ProviderRetryEvent
    | ProviderTextDeltaEvent
    | ProviderThinkingDeltaEvent
    | ProviderToolCallEvent
    | ProviderResponseEndEvent
    | ProviderErrorEvent
)
