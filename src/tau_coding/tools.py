"""Built-in filesystem and shell tools for Tau coding sessions.

Tau 编码会话的内置文件系统和 Shell 工具。

The module exposes factory functions that create provider-neutral `AgentTool`
objects plus richer `ToolDefinition` objects for callers that need prompt
metadata and JSON schemas. The tools operate relative to a configurable working
directory, return structured `AgentToolResult` values, and keep local
filesystem/shell behavior outside the reusable `tau_agent` package.

此模块提供工厂函数，用于创建提供商无关的 `AgentTool` 对象，以及供需要提示词元数据和
JSON 模式的调用方使用的丰富 `ToolDefinition` 对象。工具相对于可配置工作目录运行，
返回结构化结果，并将本地文件系统与 Shell 行为保留在可复用 `tau_agent` 包之外。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from tau_agent.messages import ImageContent, TextContent
from tau_agent.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from tau_agent.types import JSONValue
from tau_coding.image_processing import (
    DEFAULT_MAX_SOURCE_IMAGE_BYTES,
    ImageProcessingFailure,
    detect_image_family_mime_type,
    detect_supported_image_mime_type,
    process_image,
    unsupported_image_reason,
)

DEFAULT_MAX_OUTPUT_BYTES = 50 * 1024
DEFAULT_MAX_OUTPUT_LINES = 2_000
IMAGE_SNIFF_BYTES = 64 * 1024
UTF8_BOM = "\ufeff"


class ToolInputError(ValueError):
    """Raised when a tool receives invalid structured arguments.

    工具收到无效结构化参数时抛出的异常。
    """


@dataclass(slots=True)
class ImageSupportState:
    """Mutable active-model image capability shared with built-in tools.

    内置工具共享的可变活动模型图像能力状态。
    """

    supported: bool | None = None


@dataclass(frozen=True, slots=True)
class ReadOperations:
    """Pluggable filesystem operations used by the read tool.

    read 工具使用的可插拔文件系统操作。
    """

    validate_path: Callable[[Path], None]
    read_bytes: Callable[[Path], bytes]
    size_bytes: Callable[[Path], int] | None = None
    read_prefix: Callable[[Path, int], bytes] | None = None


@dataclass(frozen=True, slots=True)
class TruncationResult:
    """Metadata describing how a tool output was shortened.

    描述工具输出如何缩短的元数据。

    `content` contains the returned slice. The remaining fields record whether
    truncation happened, whether the line or byte limit was responsible, the
    total size of the original output, the size of the returned output, and
    edge cases such as partial-line output or a first line that is too large to
    display safely.

    `content` 包含返回切片。其余字段记录是否发生截断、触发行或字节限制、原始与返回输出
    的大小，以及部分行输出或首行过大等边界情况。
    """

    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int

    def to_json(self) -> dict[str, JSONValue]:
        """Serialize truncation metadata to JSON-compatible data.

        将截断元数据序列化为 JSON 兼容数据。
        """
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Complete definition for a coding tool before provider conversion.

    转换为提供商工具前的完整编码工具定义。

    A definition contains the tool name, user-facing description, prompt
    metadata, JSON input schema, and async executor. `to_agent_tool()` converts
    it into the smaller `AgentTool` type consumed by the provider-neutral agent
    loop while preserving prompt metadata for clients that render tool guidance.

    定义包含工具名称、面向用户的描述、提示词元数据、JSON 输入模式和异步执行器。
    `to_agent_tool()` 会将其转换为代理循环使用的较小 AgentTool，同时为渲染指导的客户端
    保留提示词元数据。
    """

    name: str
    description: str
    prompt_snippet: str
    prompt_guidelines: tuple[str, ...]
    input_schema: Mapping[str, JSONValue]
    executor: Callable[
        [Mapping[str, JSONValue], ToolCancellationToken | None], Awaitable[AgentToolResult]
    ]

    def to_agent_tool(self) -> AgentTool:
        """Convert the coding definition to the Pi-compatible core tool.

        将编码定义转换为兼容 Pi 的核心工具。
        """

        async def execute(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: ToolUpdateCallback | None = None,
        ) -> AgentToolResult:
            """Forward validated execution arguments to the coding-tool executor.

            将已校验的执行参数转发给编码工具执行器。
            """
            del tool_call_id, on_update
            return await self.executor(arguments, signal)

        return AgentTool(
            name=self.name,
            label=self.name,
            description=self.description,
            parameters=self.input_schema,
            execute_fn=execute,
            prompt_snippet=self.prompt_snippet,
            prompt_guidelines=self.prompt_guidelines,
        )


_file_locks: dict[Path, asyncio.Lock] = {}


def _validate_local_read_path(path: Path) -> None:
    """Reject paths that are missing or are directories.

    拒绝缺失路径或目录路径。
    """
    if not path.exists():
        raise ToolInputError(f"File not found: {path}")
    if path.is_dir():
        raise ToolInputError(f"Path is a directory: {path}")


def _local_file_size(path: Path) -> int:
    """Return the byte size of a local file.

    返回本地文件的字节大小。
    """
    return path.stat().st_size


def _local_read_prefix(path: Path, limit: int) -> bytes:
    """Read at most a bounded byte prefix from a local file.

    从本地文件读取有界字节前缀。
    """
    with path.open("rb") as file:
        return file.read(limit)


DEFAULT_READ_OPERATIONS = ReadOperations(
    validate_path=_validate_local_read_path,
    read_bytes=Path.read_bytes,
    size_bytes=_local_file_size,
    read_prefix=_local_read_prefix,
)


def create_coding_tools(
    *,
    cwd: str | Path | None = None,
    shell_command_prefix: str | None = None,
    image_support: ImageSupportState | None = None,
) -> list[AgentTool]:
    """Create the default coding-tool set for a local project.

    为本地项目创建默认编码工具集合。

    The returned tools are ordered as `read`, `write`, `edit`, and `bash`.
    Relative paths used with those tools are resolved against `cwd`; when `cwd`
    is omitted, the process current working directory at factory-call time is
    used. The tools share per-path write/edit locks within this process so
    concurrent mutations of the same file do not interleave. When configured,
    `shell_command_prefix` is prepended to every bash tool command.

    工具按 read、write、edit、bash 排序。相对路径以 cwd 为基准；未提供 cwd 时使用工厂
    调用时的当前目录。同一进程内工具共享按路径的写入和编辑锁，避免并发变更交错；配置
    Shell 前缀后，它会添加到每条 bash 命令之前。
    """
    root = Path.cwd() if cwd is None else Path(cwd)
    return [
        create_read_tool(cwd=root, image_support=image_support),
        create_write_tool(cwd=root),
        create_edit_tool(cwd=root),
        create_bash_tool(cwd=root, shell_command_prefix=shell_command_prefix),
    ]


def create_read_tool_definition(
    *,
    cwd: str | Path | None = None,
    operations: ReadOperations | None = None,
    image_support: ImageSupportState | None = None,
) -> ToolDefinition:
    """Create a definition for the `read` tool.

    为 `read` 工具创建定义。

    The tool reads a file resolved relative to `cwd` unless an absolute path is
    supplied. Text files are decoded as UTF-8 and may be sliced with optional
    1-indexed `offset` and positive integer `limit` arguments. Returned text is
    truncated to `DEFAULT_MAX_OUTPUT_LINES` lines or `DEFAULT_MAX_OUTPUT_BYTES`
    bytes, whichever comes first, and continuation hints are appended when more
    lines remain. Supported images (`jpg`, `png`, `gif`, `webp`, and `bmp`) are
    detected from file content and returned as provider-neutral image blocks.
    Images are validated and resized or converted when needed to fit inline limits.

    The executor raises `ToolInputError` for invalid arguments, missing files,
    directories, and offsets beyond the end of the file. Successful results
    include the resolved path and truncation metadata in `data`.

    工具读取相对于 cwd 解析的文件；文本按 UTF-8 解码并可按行切片和截断，支持的图像会
    根据内容检测并作为提供商无关图像块返回。执行器对无效参数和路径抛出 ToolInputError，
    成功结果在 data 中包含解析路径和截断元数据。
    """
    root = Path.cwd() if cwd is None else Path(cwd)
    read_operations = operations or DEFAULT_READ_OPERATIONS

    async def execute(
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
    ) -> AgentToolResult:
        """Read, classify, bound, and return one text or image file.

        读取、分类、限制并返回一个文本或图像文件。
        """
        del signal
        raw_path = _str_arg(arguments, "path")
        path = _path_arg(arguments, "path", cwd=root)
        offset = _optional_int_arg(arguments, "offset")
        limit = _optional_int_arg(arguments, "limit")

        if offset is not None and offset < 0:
            raise ToolInputError("offset must be at least 0")
        if limit is not None and limit < 1:
            raise ToolInputError("limit must be at least 1")
        read_operations.validate_path(path)
        if read_operations.size_bytes is not None and read_operations.read_prefix is not None:
            source_size = read_operations.size_bytes(path)
            if source_size > DEFAULT_MAX_SOURCE_IMAGE_BYTES:
                prefix = read_operations.read_prefix(path, IMAGE_SNIFF_BYTES)
                image_family = detect_image_family_mime_type(prefix)
                if image_family is not None:
                    reason = unsupported_image_reason(prefix) or (
                        f"source is {format_size(source_size)}, exceeding the "
                        f"{format_size(DEFAULT_MAX_SOURCE_IMAGE_BYTES)} processing limit"
                    )
                    return _omitted_image_result(
                        path=path,
                        source_mime_type=image_family,
                        source_bytes=source_size,
                        reason=reason,
                    )

        data = read_operations.read_bytes(path)
        unsupported_reason = unsupported_image_reason(data)
        if unsupported_reason is not None:
            image_family = detect_image_family_mime_type(data)
            assert image_family is not None
            return _omitted_image_result(
                path=path,
                source_mime_type=image_family,
                source_bytes=len(data),
                reason=unsupported_reason,
            )

        source_mime_type = detect_supported_image_mime_type(data)
        if source_mime_type is not None:
            if image_support is not None and image_support.supported is False:
                return _omitted_image_result(
                    path=path,
                    source_mime_type=source_mime_type,
                    source_bytes=len(data),
                    reason=(
                        "current model does not support image input. Image contents are "
                        "unavailable; do not infer or describe them. Ask the user to switch "
                        "to a vision-capable model"
                    ),
                )
            processed = await asyncio.to_thread(process_image, data, source_mime_type)
            image_details: dict[str, JSONValue] = {
                "path": str(path),
                "source_mime_type": source_mime_type,
                "bytes": len(data),
            }
            if isinstance(processed, ImageProcessingFailure):
                return AgentToolResult(
                    content=[
                        TextContent(
                            text=(
                                f"Read image file [{source_mime_type}]\n"
                                f"[Image omitted: {processed.message}.]"
                            )
                        )
                    ],
                    details=image_details,
                )

            image_details.update(
                {
                    "mime_type": processed.mime_type,
                    "processed_bytes": len(processed.data),
                    "width": processed.width,
                    "height": processed.height,
                }
            )
            note_lines = "".join(f"\n[{note}]" for note in processed.notes)
            return AgentToolResult(
                content=[
                    TextContent(text=f"Read image file [{processed.mime_type}]{note_lines}"),
                    ImageContent(data=_base64_text(processed.data), mime_type=processed.mime_type),
                ],
                details=image_details,
            )

        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        all_lines = text.split("\n")
        start_line = 0 if offset is None or offset == 0 else offset - 1
        if start_line >= len(all_lines):
            raise ToolInputError(
                f"Offset {offset} is beyond end of file ({len(all_lines)} lines total)"
            )

        user_limited_lines: int | None = None
        if limit is not None:
            end_line = min(start_line + limit, len(all_lines))
            selected = "\n".join(all_lines[start_line:end_line])
            user_limited_lines = end_line - start_line
        else:
            selected = "\n".join(all_lines[start_line:])

        truncation = truncate_head(selected)
        start_display = start_line + 1
        details: dict[str, JSONValue] = {"path": str(path), "truncation": truncation.to_json()}

        if truncation.first_line_exceeds_limit:
            first_line_size = format_size(len(all_lines[start_line].encode()))
            output = (
                f"[Line {start_display} is {first_line_size}, exceeds "
                f"{format_size(DEFAULT_MAX_OUTPUT_BYTES)} limit. Use bash: sed -n "
                f"'{start_display}p' {raw_path} | head -c {DEFAULT_MAX_OUTPUT_BYTES}]"
            )
        elif truncation.truncated:
            end_display = start_display + truncation.output_lines - 1
            next_offset = end_display + 1
            output = truncation.content
            if truncation.truncated_by == "lines":
                output += (
                    f"\n\n[Showing lines {start_display}-{end_display} of {len(all_lines)}. "
                    f"Use offset={next_offset} to continue.]"
                )
            else:
                output += (
                    f"\n\n[Showing lines {start_display}-{end_display} of {len(all_lines)} "
                    f"({format_size(DEFAULT_MAX_OUTPUT_BYTES)} limit). "
                    f"Use offset={next_offset} to continue.]"
                )
        elif user_limited_lines is not None and start_line + user_limited_lines < len(all_lines):
            remaining = len(all_lines) - (start_line + user_limited_lines)
            next_offset = start_line + user_limited_lines + 1
            output = (
                f"{truncation.content}\n\n[{remaining} more lines in file. "
                f"Use offset={next_offset} to continue.]"
            )
        else:
            output = truncation.content

        return AgentToolResult(
            content=[TextContent(text=output)],
            details=details,
        )

    return ToolDefinition(
        name="read",
        description=(
            "Read the contents of a file. Supports text files and images "
            "(jpg, png, gif, webp, bmp). "
            "Images are sent to vision-capable models as attachments. For text files, output is "
            "truncated to "
            f"{DEFAULT_MAX_OUTPUT_LINES} lines or {DEFAULT_MAX_OUTPUT_BYTES // 1024}KB "
            "(whichever is hit first). Use offset/limit for large files. When you need the "
            "full file, continue with offset until complete."
        ),
        prompt_snippet="Read file contents",
        prompt_guidelines=("Use read to examine files instead of cat or sed.",),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to read"},
                "offset": {"type": "integer", "description": "Line number to start reading from"},
                "limit": {"type": "integer", "description": "Maximum number of lines to read"},
            },
            "required": ["path"],
        },
        executor=execute,
    )


def _omitted_image_result(
    *,
    path: Path,
    source_mime_type: str,
    source_bytes: int,
    reason: str,
) -> AgentToolResult:
    """Return safe metadata explaining why an image attachment was omitted.

    返回说明图像附件为何省略的安全元数据。
    """
    return AgentToolResult(
        content=[
            TextContent(text=f"Read image file [{source_mime_type}]\n[Image omitted: {reason}.]")
        ],
        details={
            "path": str(path),
            "source_mime_type": source_mime_type,
            "bytes": source_bytes,
        },
    )


def create_read_tool(
    *,
    cwd: str | Path | None = None,
    operations: ReadOperations | None = None,
    image_support: ImageSupportState | None = None,
) -> AgentTool:
    """Create an `AgentTool` for reading UTF-8 text files and supported images.

    创建用于读取 UTF-8 文本文件和受支持图像的 `AgentTool`。
    """
    return create_read_tool_definition(
        cwd=cwd,
        operations=operations,
        image_support=image_support,
    ).to_agent_tool()


def create_write_tool_definition(*, cwd: str | Path | None = None) -> ToolDefinition:
    """Create a definition for the `write` tool.

    为 `write` 工具创建定义。

    The tool writes the supplied string `content` to `path`, resolving relative
    paths against `cwd`. Parent directories are created automatically and any
    existing file is overwritten. Writes use UTF-8 text encoding and are guarded
    by a per-path async lock so multiple writes/edits to the same resolved file
    are serialized within this process.

    The executor raises `ToolInputError` when `path` or `content` has the wrong
    type. Successful results include the resolved path and number of characters
    written in `data`.

    工具将字符串内容写入相对于 cwd 解析的路径，自动创建父目录并覆盖现有文件。按路径的
    异步锁会串行化同一文件的写入和编辑。执行器校验参数，成功结果包含路径和字符数。
    """
    root = Path.cwd() if cwd is None else Path(cwd)

    async def execute(
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
    ) -> AgentToolResult:
        """Serialize one locked UTF-8 file creation or overwrite.

        串行执行一次加锁的 UTF-8 文件创建或覆盖。
        """
        del signal
        path = _path_arg(arguments, "path", cwd=root)
        content = _str_arg(arguments, "content")

        async with _file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        return AgentToolResult(
            content=[TextContent(text=f"Successfully wrote to {path}.")],
            details={"path": str(path), "characters": len(content)},
        )

    return ToolDefinition(
        name="write",
        description=(
            "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. "
            "Automatically creates parent directories."
        ),
        prompt_snippet="Create or overwrite files",
        prompt_guidelines=("Use write only for new files or complete rewrites.",),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to write"},
                "content": {"type": "string", "description": "Content to write to the file"},
            },
            "required": ["path", "content"],
        },
        executor=execute,
    )


def create_write_tool(*, cwd: str | Path | None = None) -> AgentTool:
    """Create an `AgentTool` for creating or overwriting UTF-8 text files.

    创建用于新建或覆盖 UTF-8 文本文件的 `AgentTool`。
    """
    return create_write_tool_definition(cwd=cwd).to_agent_tool()


def create_edit_tool_definition(*, cwd: str | Path | None = None) -> ToolDefinition:
    """Create a definition for the `edit` tool.

    为 `edit` 工具创建定义。

    The tool applies one or more exact text replacements to a single UTF-8 file
    resolved relative to `cwd`. Each edit item contains `oldText` and `newText`.
    Every `oldText` must be non-empty, must occur exactly once in the original
    file, and must not overlap another edit span. All replacements are validated
    before writing, so the file is left unchanged if any edit fails.

    File content and edit text are normalized to LF for matching, then the
    original file's dominant line ending is restored after replacement. UTF-8
    byte-order marks are preserved. The executor also accepts legacy top-level
    `oldText`/`newText` arguments and JSON-string `edits` values by normalizing
    them into the canonical edits list.

    Successful results include the resolved path, edit count, an ndiff-style
    diff, a unified patch, and the first changed line in `data`.

    工具对单个 UTF-8 文件应用一个或多个精确文本替换。所有替换会在写入前校验唯一性和
    不重叠性；匹配时规范化换行符，写回时恢复原换行符并保留 BOM。成功结果包含差异、
    统一补丁和首个变更行。
    """
    root = Path.cwd() if cwd is None else Path(cwd)

    async def execute(
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
    ) -> AgentToolResult:
        del signal
        prepared = _prepare_edit_arguments(arguments)
        path = _path_arg(prepared, "path", cwd=root)
        edits = _edits_arg(prepared)

        if not path.exists():
            raise ToolInputError(f"Could not edit file: {path}. File not found.")
        if path.is_dir():
            raise ToolInputError(f"Could not edit file: {path}. Path is a directory.")

        async with _file_lock(path):
            raw_content = path.read_text(encoding="utf-8")
            bom, content = _strip_bom(raw_content)
            original_ending = detect_line_ending(content)
            normalized = normalize_to_lf(content)
            base_content, new_content = apply_edits_to_normalized_content(
                normalized, edits, str(path)
            )
            final_content = bom + restore_line_endings(new_content, original_ending)
            path.write_text(final_content, encoding="utf-8")

        diff_text, first_changed_line = generate_diff_string(base_content, new_content)
        patch = generate_unified_patch(str(path), base_content, new_content)
        return AgentToolResult(
            content=[TextContent(text=f"Successfully replaced {len(edits)} block(s) in {path}.")],
            details={
                "path": str(path),
                "edits": len(edits),
                "diff": diff_text,
                "patch": patch,
                "first_changed_line": first_changed_line,
            },
        )

    return ToolDefinition(
        name="edit",
        description=(
            "Edit a single file using exact text replacement. Every edits[].oldText must match "
            "a unique, non-overlapping region of the original file. If two changes affect the "
            "same block or nearby lines, merge them into one edit instead of emitting overlapping "
            "edits. Do not include large unchanged regions just to connect distant changes."
        ),
        prompt_snippet=(
            "Make precise file edits with exact text replacement, including multiple disjoint "
            "edits in one call"
        ),
        prompt_guidelines=(
            "Use edit for precise changes (edits[].oldText must match exactly)",
            "When changing multiple separate locations in one file, use one edit call with "
            "multiple entries in edits[] instead of multiple edit calls",
            "Each edits[].oldText is matched against the original file, not after earlier "
            "edits are applied. Do not emit overlapping or nested edits. Merge nearby "
            "changes into one edit.",
            "Keep edits[].oldText as small as possible while still being unique in the file. "
            "Do not pad with large unchanged regions.",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "edits": {
                    "type": "array",
                    "description": "One or more targeted replacements.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oldText": {"type": "string"},
                            "newText": {"type": "string"},
                        },
                        "required": ["oldText", "newText"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["path", "edits"],
            "additionalProperties": False,
        },
        executor=execute,
    )


def create_edit_tool(*, cwd: str | Path | None = None) -> AgentTool:
    """Create an `AgentTool` for exact, validated text replacement in one file.

    创建用于单文件精确且已校验文本替换的 `AgentTool`。
    """
    return create_edit_tool_definition(cwd=cwd).to_agent_tool()


def create_bash_tool_definition(
    *,
    cwd: str | Path | None = None,
    shell_command_prefix: str | None = None,
) -> ToolDefinition:
    """Create a definition for the `bash` tool.

    为 `bash` 工具创建定义。

    The tool runs a shell command with `cwd` as the subprocess working
    directory, disconnects stdin, and combines stdout and stderr into one UTF-8
    decoded output stream. Disconnecting stdin keeps interactive programs from
    competing with Tau for terminal input. The optional `timeout` argument must
    be positive when supplied. On timeout, POSIX commands are started in a new
    session and the entire process group is killed so shell children from
    pipelines or compound commands do not continue running; non-POSIX platforms
    fall back to killing the direct subprocess.

    Output is tail-truncated to `DEFAULT_MAX_OUTPUT_LINES` lines or
    `DEFAULT_MAX_OUTPUT_BYTES` bytes. When truncation occurs, the full output is
    written to a temporary log file and that path is reported in `data`.
    Successful and failed command results both include exit code, timeout state,
    duration, truncation metadata, and full-output path metadata.

    工具在 cwd 中运行 Shell 命令，断开 stdin，并合并 stdout 与 stderr。超时或取消时会
    终止进程树；输出按尾部截断，完整输出可写入临时日志。结果包含退出码、超时状态、
    持续时间、截断元数据和完整输出路径。
    """
    root = Path.cwd() if cwd is None else Path(cwd)
    prefix = shell_command_prefix.strip() if shell_command_prefix else None

    async def execute(
        arguments: Mapping[str, JSONValue],
        signal: ToolCancellationToken | None = None,
    ) -> AgentToolResult:
        command = _str_arg(arguments, "command")
        shell_command = _prefixed_shell_command(command, prefix)
        timeout = _optional_float_arg(arguments, "timeout")
        if timeout is not None and timeout <= 0:
            raise ToolInputError("timeout must be greater than 0")
        if signal is not None and signal.is_cancelled():
            raise ToolInputError("Command cancelled")

        start = monotonic()
        if os.name == "posix":
            process = await asyncio.create_subprocess_shell(
                shell_command,
                cwd=root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
                executable="bash" if prefix else None,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                shell_command,
                cwd=root,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        output_bytes, _stderr, timed_out, cancelled = await _communicate_with_cancellation(
            process,
            timeout=timeout,
            signal=signal,
        )

        output = output_bytes.decode(errors="replace")
        truncation = truncate_tail(output)
        full_output_path: str | None = None
        output_text = truncation.content or "(no output)"
        if truncation.truncated:
            full_output_path = _write_temp_output(output)
            start_line = truncation.total_lines - truncation.output_lines + 1
            end_line = truncation.total_lines
            if truncation.last_line_partial:
                output_text += (
                    f"\n\n[Showing last {format_size(truncation.output_bytes)} of line {end_line}. "
                    f"Full output: {full_output_path}]"
                )
            elif truncation.truncated_by == "lines":
                output_text += (
                    f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines}. "
                    f"Full output: {full_output_path}]"
                )
            else:
                output_text += (
                    f"\n\n[Showing lines {start_line}-{end_line} of {truncation.total_lines} "
                    f"({format_size(DEFAULT_MAX_OUTPUT_BYTES)} limit). "
                    f"Full output: {full_output_path}]"
                )

        exit_code = process.returncode
        status: str | None = None
        if timed_out:
            status = (
                f"Command timed out after {timeout:g} seconds" if timeout else "Command timed out"
            )
        elif cancelled:
            status = "Command cancelled"
        elif exit_code not in (0, None):
            status = f"Command exited with code {exit_code}"
        if status:
            output_text = append_status_block(output_text, status)

        return AgentToolResult(
            content=[TextContent(text=output_text)],
            details={
                "command": command,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "cancelled": cancelled,
                "duration_seconds": round(monotonic() - start, 3),
                "truncation": truncation.to_json(),
                "full_output_path": full_output_path,
                "shell_command_prefix_applied": prefix is not None,
            },
        )

    return ToolDefinition(
        name="bash",
        description=(
            "Execute a bash command in the current working directory. Returns stdout and stderr. "
            f"Output is truncated to last {DEFAULT_MAX_OUTPUT_LINES} lines or "
            f"{DEFAULT_MAX_OUTPUT_BYTES // 1024}KB (whichever is hit first). If truncated, "
            "full output is saved to a temp file. Optionally provide a timeout in seconds."
        ),
        prompt_snippet="Execute bash commands (ls, grep, find, etc.)",
        prompt_guidelines=(
            "When using bash, include a brief present-participle description of the "
            "command's purpose (for example, 'Running tests').",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Bash command to execute"},
                "description": {
                    "type": "string",
                    "description": (
                        "Brief present-participle summary of the command's purpose, such as "
                        "'Running tests' or 'Validating and committing changes'"
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in seconds (optional, no default timeout)",
                },
            },
            "required": ["command", "description"],
        },
        executor=execute,
    )


def create_bash_tool(
    *,
    cwd: str | Path | None = None,
    shell_command_prefix: str | None = None,
) -> AgentTool:
    """Create an `AgentTool` for executing shell commands with captured output.

    创建用于执行 Shell 命令并捕获输出的 `AgentTool`。
    """
    return create_bash_tool_definition(
        cwd=cwd,
        shell_command_prefix=shell_command_prefix,
    ).to_agent_tool()


def _prefixed_shell_command(command: str, prefix: str | None) -> str:
    """Return a shell command with an opt-in setup prefix applied.

    返回应用了可选设置前缀的 Shell 命令。
    """
    if prefix is None:
        return command
    return f"{prefix}\n{command}"


def format_size(bytes_count: int) -> str:
    """Format a byte count as a concise human-readable size.

    将字节数格式化为简洁易读的大小。
    """
    if bytes_count < 1024:
        return f"{bytes_count}B"
    if bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    return f"{bytes_count / (1024 * 1024):.1f}MB"


def append_status_block(text: str, status: str) -> str:
    """Append command status text after a blank line when output already exists.

    已有输出时，在空行后追加命令状态文本。
    """
    return f"{text}\n\n{status}" if text else status


async def _communicate_with_cancellation(
    process: asyncio.subprocess.Process,
    *,
    timeout: float | None,
    signal: ToolCancellationToken | None,
) -> tuple[bytes, bytes | None, bool, bool]:
    communicate = asyncio.create_task(process.communicate())
    cancel_watch: asyncio.Task[None] | None = None
    try:
        wait_for: set[asyncio.Task[Any]] = {communicate}
        if signal is not None:
            cancel_watch = asyncio.create_task(_wait_for_cancel(signal))
            wait_for.add(cancel_watch)

        done, _pending = await asyncio.wait(
            wait_for,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if communicate in done:
            output_bytes, stderr = communicate.result()
            return output_bytes, stderr, False, False

        cancelled = cancel_watch is not None and cancel_watch in done
        _kill_process_tree(process)
        try:
            output_bytes, stderr = await communicate
        except asyncio.CancelledError:
            output_bytes = b""
            stderr_result: bytes | None = None
        else:
            stderr_result = stderr
        return output_bytes, stderr_result, not cancelled, cancelled
    except asyncio.CancelledError:
        _kill_process_tree(process)
        if not communicate.done():
            communicate.cancel()
        raise
    finally:
        if cancel_watch is not None:
            cancel_watch.cancel()


async def _wait_for_cancel(signal: ToolCancellationToken) -> None:
    """Wait until a tool cancellation token is signalled.

    等待工具取消令牌发出信号。
    """
    while not signal.is_cancelled():
        await asyncio.sleep(0.05)


def truncate_head(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_OUTPUT_LINES,
    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> TruncationResult:
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    total_bytes = len(content.encode())
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _truncation_result(
            content, False, None, total_lines, total_bytes, total_lines, total_bytes
        )

    first_line_bytes = len(lines[0].encode()) if lines else 0
    if first_line_bytes > max_bytes:
        return _truncation_result(
            "", True, "bytes", total_lines, total_bytes, 0, 0, first_line=True
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    for index, line in enumerate(lines[:max_lines]):
        line_bytes = len(line.encode()) + (1 if index > 0 else 0)
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes += line_bytes

    output = "\n".join(output_lines)
    return _truncation_result(
        output,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output_lines),
        len(output.encode()),
    )


def truncate_tail(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_OUTPUT_LINES,
    max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> TruncationResult:
    lines = _split_lines_for_counting(content)
    total_lines = len(lines)
    total_bytes = len(content.encode())
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return _truncation_result(
            content, False, None, total_lines, total_bytes, total_lines, total_bytes
        )

    output_lines: list[str] = []
    output_bytes = 0
    truncated_by = "lines"
    last_line_partial = False
    for line in reversed(lines):
        line_bytes = len(line.encode()) + (1 if output_lines else 0)
        if len(output_lines) >= max_lines:
            truncated_by = "lines"
            break
        if output_bytes + line_bytes > max_bytes:
            truncated_by = "bytes"
            if not output_lines:
                clipped = _truncate_string_to_bytes_from_end(line, max_bytes)
                output_lines.insert(0, clipped)
                output_bytes = len(clipped.encode())
                last_line_partial = True
            break
        output_lines.insert(0, line)
        output_bytes += line_bytes

    output = "\n".join(output_lines)
    return _truncation_result(
        output,
        True,
        truncated_by,
        total_lines,
        total_bytes,
        len(output_lines),
        len(output.encode()),
        last_line_partial=last_line_partial,
    )


def detect_line_ending(content: str) -> str:
    """Detect the dominant line ending used by text content.

    检测文本内容使用的主要换行符。
    """
    crlf_index = content.find("\r\n")
    lf_index = content.find("\n")
    if lf_index == -1 or crlf_index == -1:
        return "\n"
    return "\r\n" if crlf_index < lf_index else "\n"


def normalize_to_lf(text: str) -> str:
    """Normalize supported line endings to LF.

    将受支持的换行符规范化为 LF。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def restore_line_endings(text: str, ending: str) -> str:
    """Restore normalized text to a chosen line ending.

    将规范化文本恢复为指定换行符。
    """
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def apply_edits_to_normalized_content(
    normalized_content: str,
    edits: list[dict[str, str]],
    path: str,
) -> tuple[str, str]:
    normalized_edits = [
        {"oldText": normalize_to_lf(edit["oldText"]), "newText": normalize_to_lf(edit["newText"])}
        for edit in edits
    ]
    for index, edit in enumerate(normalized_edits):
        if not edit["oldText"]:
            raise ToolInputError(_empty_old_text_error(path, index, len(normalized_edits)))

    matches: list[tuple[int, int, str]] = []
    for index, edit in enumerate(normalized_edits):
        old_text = edit["oldText"]
        occurrences = _count_occurrences(normalized_content, old_text)
        if occurrences == 0:
            raise ToolInputError(_not_found_error(path, index, len(normalized_edits)))
        if occurrences > 1:
            raise ToolInputError(_duplicate_error(path, index, len(normalized_edits), occurrences))
        start = normalized_content.index(old_text)
        matches.append((start, start + len(old_text), edit["newText"]))

    _validate_non_overlapping(matches)
    new_content = normalized_content
    for start, end, new_text in sorted(matches, reverse=True):
        new_content = f"{new_content[:start]}{new_text}{new_content[end:]}"
    if new_content == normalized_content:
        raise ToolInputError(_no_change_error(path, len(normalized_edits)))
    return normalized_content, new_content


def generate_diff_string(old: str, new: str) -> tuple[str, int | None]:
    """Generate a compact diff and first changed line.

    生成紧凑差异及首个变更行。
    """
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = "\n".join(difflib.ndiff(old_lines, new_lines))
    first_changed_line: int | None = None
    new_line_number = 0
    for line in difflib.ndiff(old_lines, new_lines):
        if line.startswith("  "):
            new_line_number += 1
        elif line.startswith("+"):
            new_line_number += 1
            if first_changed_line is None:
                first_changed_line = new_line_number
        elif line.startswith("-") and first_changed_line is None:
            first_changed_line = max(new_line_number + 1, 1)
    return diff, first_changed_line


def generate_unified_patch(path: str, old: str, new: str) -> str:
    """Generate a unified patch for one file path.

    为一个文件路径生成统一补丁。
    """
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=path,
            tofile=path,
        )
    )


def _truncation_result(
    content: str,
    truncated: bool,
    truncated_by: str | None,
    total_lines: int,
    total_bytes: int,
    output_lines: int,
    output_bytes: int,
    *,
    last_line_partial: bool = False,
    first_line: bool = False,
) -> TruncationResult:
    return TruncationResult(
        content=content,
        truncated=truncated,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=output_lines,
        output_bytes=output_bytes,
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=first_line,
        max_lines=DEFAULT_MAX_OUTPUT_LINES,
        max_bytes=DEFAULT_MAX_OUTPUT_BYTES,
    )


def _split_lines_for_counting(content: str) -> list[str]:
    """Split text into lines using output-count semantics.

    按输出计数语义将文本拆分为行。
    """
    if not content:
        return []
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    return lines


def _truncate_string_to_bytes_from_end(text: str, max_bytes: int) -> str:
    """Retain a valid UTF-8 suffix within a byte limit.

    在字节限制内保留有效 UTF-8 后缀。
    """
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[-max_bytes:]
    return clipped.decode(errors="ignore")


def _str_arg(arguments: Mapping[str, JSONValue], name: str) -> str:
    """Read one required string tool argument.

    读取一个必需的字符串工具参数。
    """
    value = arguments.get(name)
    if not isinstance(value, str):
        raise ToolInputError(f"{name} must be a string")
    return value


def _path_arg(arguments: Mapping[str, JSONValue], name: str, *, cwd: Path) -> Path:
    """Resolve one required path argument against the tool working directory.

    相对于工具工作目录解析一个必需路径参数。
    """
    value = _str_arg(arguments, name)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path


def _optional_int_arg(arguments: Mapping[str, JSONValue], name: str) -> int | None:
    """Read one optional integer argument.

    读取一个可选整数参数。
    """
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ToolInputError(f"{name} must be an integer")
    return value


def _optional_float_arg(arguments: Mapping[str, JSONValue], name: str) -> float | None:
    """Read one optional numeric argument as a float.

    将一个可选数值参数读取为浮点数。
    """
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, int | float):
        raise ToolInputError(f"{name} must be a number")
    return float(value)


def _prepare_edit_arguments(arguments: Mapping[str, JSONValue]) -> Mapping[str, JSONValue]:
    """Normalize legacy and JSON-string edit arguments into canonical edits.

    将旧式和 JSON 字符串编辑参数规范化为标准 edits。
    """
    prepared = dict(arguments)
    edits_value = prepared.get("edits")
    if isinstance(edits_value, str):
        try:
            parsed = json.loads(edits_value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            prepared["edits"] = parsed

    old_text = prepared.get("oldText")
    new_text = prepared.get("newText")
    if isinstance(old_text, str) and isinstance(new_text, str):
        edits = prepared.get("edits")
        edit_list = edits if isinstance(edits, list) else []
        prepared["edits"] = [*edit_list, {"oldText": old_text, "newText": new_text}]
        prepared.pop("oldText", None)
        prepared.pop("newText", None)
    return prepared


def _edits_arg(arguments: Mapping[str, JSONValue]) -> list[dict[str, str]]:
    """Validate and return the canonical list of exact text replacements.

    校验并返回标准精确文本替换列表。
    """
    value = arguments.get("edits")
    if not isinstance(value, list) or not value:
        raise ToolInputError(
            "Edit tool input is invalid. edits must contain at least one replacement."
        )

    edits: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ToolInputError(f"edits[{index}] must be an object")
        old_text = item.get("oldText")
        new_text = item.get("newText")
        if not isinstance(old_text, str) or not isinstance(new_text, str):
            raise ToolInputError(
                f"edits[{index}].oldText and edits[{index}].newText must be strings"
            )
        edits.append({"oldText": old_text, "newText": new_text})
    return edits


def _validate_non_overlapping(spans: list[tuple[int, int, str]]) -> None:
    """Reject edit spans that overlap in the original file.

    拒绝在原始文件中重叠的编辑范围。
    """
    previous_end = -1
    for start, end, _new_text in sorted(spans):
        if start < previous_end:
            raise ToolInputError("Edits must not overlap")
        previous_end = end


def _count_occurrences(content: str, text: str) -> int:
    """Count non-overlapping exact occurrences in text.

    统计文本中不重叠的精确出现次数。
    """
    count = 0
    start = 0
    while True:
        index = content.find(text, start)
        if index == -1:
            return count
        count += 1
        start = index + len(text)


def _strip_bom(content: str) -> tuple[str, str]:
    """Remove and return an optional UTF-8 BOM prefix.

    移除并返回可选的 UTF-8 BOM 前缀。
    """
    return (UTF8_BOM, content[1:]) if content.startswith(UTF8_BOM) else ("", content)


def _not_found_error(path: str, edit_index: int, total_edits: int) -> str:
    """Build a precise error for an exact edit target that was not found.

    为未找到的精确编辑目标构建明确错误。
    """
    if total_edits == 1:
        return (
            f"Could not find the exact text in {path}. The old text must match exactly "
            "including all whitespace and newlines."
        )
    return (
        f"Could not find edits[{edit_index}] in {path}. The oldText must match exactly "
        "including all whitespace and newlines."
    )


def _duplicate_error(path: str, edit_index: int, total_edits: int, occurrences: int) -> str:
    """Build a precise error for a non-unique edit target.

    为不唯一的编辑目标构建明确错误。
    """
    if total_edits == 1:
        return (
            f"Found {occurrences} occurrences of the text in {path}. The text must be unique. "
            "Please provide more context to make it unique."
        )
    return (
        f"Found {occurrences} occurrences of edits[{edit_index}] in {path}. "
        "Each oldText must be unique. Please provide more context to make it unique."
    )


def _empty_old_text_error(path: str, edit_index: int, total_edits: int) -> str:
    """Build an error for an empty exact-match edit target.

    为精确匹配编辑目标为空构建错误。
    """
    if total_edits == 1:
        return f"oldText must not be empty in {path}."
    return f"edits[{edit_index}].oldText must not be empty in {path}."


def _no_change_error(path: str, total_edits: int) -> str:
    """Build an error when validated replacements produce identical content.

    当已校验替换产生相同内容时构建错误。
    """
    if total_edits == 1:
        return (
            f"No changes made to {path}. The replacement produced identical content. "
            "This might indicate an issue with special characters or the text not existing "
            "as expected."
        )
    return f"No changes made to {path}. The replacements produced identical content."


def _base64_text(data: bytes) -> str:
    """Encode binary data as ASCII base64 text.

    将二进制数据编码为 ASCII Base64 文本。
    """
    import base64

    return base64.b64encode(data).decode("ascii")


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Terminate a subprocess and its process group when supported.

    在平台支持时终止子进程及其进程组。
    """
    if os.name == "posix":
        # `getattr` keeps mypy happy on the Windows stubs (see issue #513).
        # `getattr` 用于满足 Windows 类型桩上的 mypy 检查（见 issue #513）。
        killpg = getattr(os, "killpg")  # noqa: B009
        sigkill = getattr(signal, "SIGKILL")  # noqa: B009
        try:
            killpg(process.pid, sigkill)
        except ProcessLookupError:
            return
    else:
        try:
            process.kill()
        except ProcessLookupError:
            return


def _write_temp_output(output: str) -> str:
    """Persist full truncated command output to a temporary file.

    将完整的截断命令输出持久化到临时文件。
    """
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="tau-bash-",
        suffix=".log",
        delete=False,
    ) as handle:
        handle.write(output)
        return handle.name


class _FileLockContext:
    """Async context manager serializing mutations of one file path.

    串行化单个文件路径变更的异步上下文管理器。
    """
    def __init__(self, path: Path) -> None:
        """Bind the context manager to one normalized file path.

        将上下文管理器绑定到一个规范化文件路径。
        """
        self._path = path.resolve()
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> None:
        """Acquire the process-local lock for the file path.

        获取文件路径的进程本地锁。
        """
        lock = _file_locks.setdefault(self._path, asyncio.Lock())
        self._lock = lock
        await lock.acquire()

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Release the process-local file lock.

        释放进程本地文件锁。
        """
        if self._lock is not None:
            self._lock.release()


def _file_lock(path: Path) -> _FileLockContext:
    """Return an async lock context for one file path.

    返回一个文件路径的异步锁上下文。
    """
    return _FileLockContext(path)
