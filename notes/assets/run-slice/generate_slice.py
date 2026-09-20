"""Generate a real Tau agent-loop event slice for the article.

Drives ``run_agent_loop`` with ``FakeProvider`` replaying two assistant
streams: the first asks to call the ``read`` tool, the second gives the
final answer. Every ``AgentEvent`` is serialized with the same Pi-compatible
camelCase JSON the CLI's ``JsonEventRenderer`` emits
(``event.model_dump_json(by_alias=True, exclude_none=True)``).

Run from the repo root:

    uv run python notes/assets/run-slice/generate_slice.py
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from tau_agent import AgentMessage, AgentTool, AgentToolResult, UserMessage
from tau_agent.loop import run_agent_loop
from tau_agent.messages import AssistantMessage, TextContent, ToolCall
from tau_agent.provider_events import (
    AssistantDoneEvent,
    AssistantStartEvent,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from tau_agent.types import JSONValue
from tau_ai import FakeProvider

MODEL = "fake-model"
SYSTEM = "You are Tau, a minimalist coding agent."


async def read_file(
    tool_call_id: str,
    arguments: Mapping[str, JSONValue],
    signal: object = None,
    on_update: object = None,
) -> AgentToolResult:
    del tool_call_id, signal, on_update
    path = str(arguments.get("path", ""))
    if path == "hello.txt":
        return AgentToolResult(content="hello from tau\n", details={"path": path, "lines": 1})
    return AgentToolResult(content=f"read: {path}: no such file", details={})


READ_TOOL = AgentTool(
    name="read",
    label="Read",
    description="Read a file from disk.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path to read."}},
        "required": ["path"],
    },
    execute_fn=read_file,
)


def first_stream() -> list:
    """Assistant turn 1: short text, then a `read` tool call."""
    call = ToolCall(id="call_read_1", name="read", arguments={"path": "hello.txt"})
    partial = AssistantMessage(model=MODEL)
    return [
        AssistantStartEvent(partial=partial),
        TextDeltaEvent(
            content_index=0,
            delta="Let me read that file.",
            partial=AssistantMessage(model=MODEL, content="Let me read that file."),
        ),
        ToolCallStartEvent(
            content_index=1,
            partial=AssistantMessage(
                model=MODEL,
                content=[TextContent(text="Let me read that file.")],
            ),
        ),
        ToolCallDeltaEvent(
            content_index=1,
            delta='{"path": "hello.txt"}',
            partial=AssistantMessage(model=MODEL, content=[call]),
        ),
        ToolCallEndEvent(
            content_index=1,
            tool_call=call,
            partial=AssistantMessage(model=MODEL, content=[call]),
        ),
        AssistantDoneEvent(
            reason="toolUse",
            message=AssistantMessage(
                model=MODEL,
                provider="fake",
                api="fake-messages",
                content=[TextContent(text="Let me read that file."), call],
                stop_reason="toolUse",
            ),
        ),
    ]


def second_stream() -> list:
    """Assistant turn 2: the final answer after seeing the tool result."""
    answer = "The file says: hello from tau"
    return [
        AssistantStartEvent(partial=AssistantMessage(model=MODEL)),
        TextDeltaEvent(
            content_index=0,
            delta="The file says: ",
            partial=AssistantMessage(model=MODEL, content="The file says: "),
        ),
        TextDeltaEvent(
            content_index=0,
            delta="hello from tau",
            partial=AssistantMessage(model=MODEL, content=answer),
        ),
        AssistantDoneEvent(
            reason="stop",
            message=AssistantMessage(
                model=MODEL,
                provider="fake",
                api="fake-messages",
                content=answer,
                stop_reason="stop",
            ),
        ),
    ]


async def main() -> None:
    provider = FakeProvider([first_stream(), second_stream()])
    messages: list[AgentMessage] = []
    stream = run_agent_loop(
        provider=provider,
        model=MODEL,
        system=SYSTEM,
        messages=messages,
        tools=[READ_TOOL],
        prompts=[UserMessage(content="What does hello.txt say?")],
        session_id="run-slice-demo",
    )
    async for event in stream:
        print(event.model_dump_json(by_alias=True, exclude_none=True))


if __name__ == "__main__":
    asyncio.run(main())
