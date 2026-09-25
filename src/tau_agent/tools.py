"""Pi-compatible provider-neutral tool definitions and execution results.

Pi 兼容且与模型提供者无关的工具定义与执行结果。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import Field, model_validator

from tau_agent.messages import ImageContent, TextContent, ToolCall, WireModel
from tau_agent.types import JSONValue


class ToolCancellationToken(Protocol):
    """Cancellation contract available to tool implementations.

    提供给工具实现的取消协议。
    """
    def is_cancelled(self) -> bool:
        """Return whether tool execution should stop.

        返回是否应停止工具执行。
        """
        ...


class AgentToolResult(WireModel):
    """Final or partial result produced by a tool.

    工具生成的最终或部分结果。
    """

    content: list[TextContent | ImageContent] = Field(default_factory=list)
    details: JSONValue = None
    added_tool_names: list[str] | None = None
    terminate: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_text_content(cls, value: object) -> object:
        """Normalize legacy string content into structured text blocks.

        将旧式字符串内容规范化为结构化文本块。
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        """Concatenate all text blocks in the result.

        拼接结果中的所有文本块。
        """
        return "".join(block.text for block in self.content if isinstance(block, TextContent))


class ToolCallRenderer(Protocol):
    """Render tool arguments for a frontend.

    为前端渲染工具参数。
    """
    def __call__(self, arguments: Mapping[str, JSONValue]) -> str | None:
        """Return a frontend-friendly tool invocation, or ``None``.

        返回对前端友好的工具调用表示，或返回 ``None``。
        """
        ...


class ToolResultRenderer(Protocol):
    """Render tool results for a frontend.

    为前端渲染工具结果。
    """
    def __call__(self, result: AgentToolResult, *, expanded: bool) -> str | None:
        """Return frontend markup for a tool result, or ``None``.

        返回工具结果的前端标记，或返回 ``None``。
        """
        ...


ToolUpdateCallback = Callable[[AgentToolResult], None]


class ToolExecutor(Protocol):
    """Callable contract for asynchronous tool execution.

    异步工具执行的可调用协议。
    """
    def __call__(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> Awaitable[AgentToolResult]:
        """Execute one validated tool call.

        执行一次已验证的工具调用。
        """
        ...


ToolExecutionMode = Literal["sequential", "parallel"]
ToolArgumentPreparer = Callable[[object], Mapping[str, JSONValue]]


@dataclass(frozen=True, slots=True)
class AgentTool:
    """A tool exposed to the portable agent loop.

    暴露给可移植代理循环的工具。
    """

    name: str
    label: str
    description: str
    parameters: Mapping[str, JSONValue]
    execute_fn: ToolExecutor
    prompt_snippet: str | None = None
    prompt_guidelines: tuple[str, ...] = ()
    prepare_arguments: ToolArgumentPreparer | None = None
    execution_mode: ToolExecutionMode = "parallel"
    render_call: ToolCallRenderer | None = None
    render_result: ToolResultRenderer | None = None

    @property
    def input_schema(self) -> Mapping[str, JSONValue]:
        """Alias used by provider payload builders.

        供模型提供者负载构建器使用的别名。
        """
        return self.parameters

    async def execute(
        self,
        tool_call_id: str,
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
        on_update: ToolUpdateCallback | None = None,
    ) -> AgentToolResult:
        """Execute a tool with Pi-compatible call-id and progress semantics.

        使用 Pi 兼容的调用 ID 与进度语义执行工具。
        """
        return await self.execute_fn(tool_call_id, arguments, signal, on_update)


__all__ = [
    "AgentTool",
    "AgentToolResult",
    "ToolCall",
    "ToolCallRenderer",
    "ToolCancellationToken",
    "ToolExecutionMode",
    "ToolResultRenderer",
    "ToolExecutor",
    "ToolUpdateCallback",
]
