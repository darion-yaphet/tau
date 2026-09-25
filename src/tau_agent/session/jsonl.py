"""JSONL serialization and Tau-v1 persisted-session migration."""

# JSONL 序列化与 Tau v1 持久化会话迁移。

from __future__ import annotations

import json
from typing import Any

from pydantic import TypeAdapter, ValidationError

from tau_agent.session.entries import SessionEntry

_SESSION_ENTRY_ADAPTER: TypeAdapter[SessionEntry] = TypeAdapter(SessionEntry)


class SessionJsonlError(ValueError):
    """Raised when a session JSONL line cannot be decoded."""

    # 当某行会话 JSONL 无法解码时抛出。


def entry_to_json_line(entry: SessionEntry) -> str:
    """Serialize one entry in Tau's canonical persisted shape."""

    # 按 Tau 的标准持久化结构序列化单个条目。
    return _SESSION_ENTRY_ADAPTER.dump_json(entry, exclude_none=True).decode() + "\n"


def entry_from_json_line(line: str, *, line_number: int | None = None) -> SessionEntry:
    """Deserialize one entry, migrating persisted Tau-v1 messages first."""

    # 反序列化单个条目，并先迁移已持久化的 Tau v1 消息。
    location = f" on line {line_number}" if line_number is not None else ""
    try:
        payload = json.loads(line)
        migrated = _migrate_session_entry(payload, legacy_label_target_id=None)
        return _SESSION_ENTRY_ADAPTER.validate_python(migrated)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise SessionJsonlError(f"Invalid session entry{location}: {exc}") from exc


def entries_from_json_lines(lines: list[str]) -> list[SessionEntry]:
    """Deserialize non-empty JSONL lines in order.

    按顺序反序列化非空的 JSONL 行。

    Legacy session-level labels had no target. They deterministically become a
    bookmark on the earliest branchable entry, preserving the old value without
    conflating bookmarks with ``SessionInfoEntry.title``. If the transcript has
    no branchable entry, the earliest ordinary entry is used instead.

    旧版会话级标签没有目标。迁移时会确定性地将其变为最早可分支条目上的书签，
    从而保留旧值且不会将书签与 ``SessionInfoEntry.title`` 混为一谈。如果对话记录
    中没有可分支条目，则改用最早的普通条目。
    """
    # Phase 1: decode non-empty lines while preserving source line numbers.
    #
    # 阶段 1：解码非空行，同时保留源文件行号。
    decoded: list[tuple[int, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            decoded.append((index, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise SessionJsonlError(f"Invalid session entry on line {index}: {exc}") from exc

    # Phase 2: choose one deterministic target for legacy session labels.
    #
    # 阶段 2：为旧版会话标签选择一个确定性目标。
    legacy_target_id = _legacy_label_target_id([value for _line, value in decoded])

    # Phase 3: migrate and validate each decoded entry in original order.
    #
    # 阶段 3：按原始顺序迁移并校验每个已解码条目。
    entries: list[SessionEntry] = []
    for line_number, value in decoded:
        try:
            migrated = _migrate_session_entry(value, legacy_label_target_id=legacy_target_id)
            entries.append(_SESSION_ENTRY_ADAPTER.validate_python(migrated))
        except (ValidationError, TypeError, ValueError) as exc:
            raise SessionJsonlError(f"Invalid session entry on line {line_number}: {exc}") from exc
    return entries


def _migrate_session_entry(value: Any, *, legacy_label_target_id: str | None) -> Any:
    """Return a canonical copy of one decoded persisted entry.

    返回单个已解码持久化条目的标准副本。

    The extension API may break in lockstep, but user session history must not.
    Migration is intentionally confined to this persistence boundary so runtime
    models and extension-facing constructors retain one strict protocol.

    扩展 API 可以同步发生破坏性变更，但用户会话历史不能因此失效。迁移逻辑被
    有意限制在这一持久化边界内，使运行时模型和面向扩展的构造器维持单一严格协议。
    """
    # Normalize entry-level legacy shapes before handling message payloads.
    #
    # 在处理消息载荷前，先规范化条目级旧版结构。
    if not isinstance(value, dict):
        return value
    if value.get("type") == "label" and "target_id" not in value:
        migrated = dict(value)
        migrated["target_id"] = legacy_label_target_id or value.get("parent_id") or value.get("id")
        return migrated
    if value.get("type") == "custom_message":
        migrated = dict(value)
        if "customType" in migrated:
            migrated.setdefault("custom_type", migrated["customType"])
            migrated.pop("customType")
        return migrated
    if value.get("type") != "message":
        return value

    # Migrate the nested message, then lift legacy custom messages to entries.
    #
    # 迁移嵌套消息，再将旧版自定义消息提升为条目。
    migrated = dict(value)
    message = _migrate_message(value.get("message"))
    if isinstance(message, dict) and message.get("role") == "custom":
        message_timestamp = message.get("timestamp")
        if isinstance(message_timestamp, int | float) and not isinstance(message_timestamp, bool):
            migrated["timestamp"] = message_timestamp / 1000
        migrated["type"] = "custom_message"
        migrated["custom_type"] = message.get("customType", message.get("custom_type"))
        migrated["content"] = message.get("content")
        migrated["display"] = message.get("display", True)
        migrated["details"] = message.get("details")
        migrated.pop("message", None)
        return migrated

    migrated["message"] = message
    return migrated


def _legacy_label_target_id(values: list[Any]) -> str | None:
    """Choose one stable target for target-less pre-bookmark label records."""

    # 为没有目标的早期书签标签记录选择一个稳定目标。
    branchable_types = {"message", "compaction", "branch_summary"}
    for value in values:
        if isinstance(value, dict) and value.get("type") in branchable_types:
            entry_id = value.get("id")
            if isinstance(entry_id, str):
                return entry_id
    for value in values:
        if isinstance(value, dict) and value.get("type") not in {"label", "leaf"}:
            entry_id = value.get("id")
            if isinstance(entry_id, str):
                return entry_id
    return None


# Convert one legacy message payload to the canonical message protocol.
#
# 将单个旧版消息载荷转换为标准消息协议。
def _migrate_message(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    message = dict(value)
    role = message.get("role")

    # Migrate legacy custom messages encoded with the user role.
    #
    # 迁移以用户角色编码的旧版自定义消息。
    if role == "user" and ("custom_type" in message or "customType" in message):
        message["role"] = "custom"
        message["customType"] = message.pop("custom_type", message.get("customType"))
        message.pop("custom_type", None)
        message.setdefault("display", True)
        return message

    # Normalize assistant usage and merge legacy text/tool-call fields into blocks.
    #
    # 规范化助手用量，并将旧版文本与工具调用字段合并为内容块。
    if role == "assistant":
        usage = message.get("usage")
        if isinstance(usage, dict) and usage.get("cost") is None:
            usage = dict(usage)
            usage["cost"] = {}
            message["usage"] = usage

        content = message.get("content", "")
        if isinstance(content, str):
            blocks: list[Any] = []
            if content:
                blocks.append({"type": "text", "text": content})
            blocks.extend(message.pop("tool_calls", message.pop("toolCalls", [])) or [])
            message["content"] = blocks
        elif "tool_calls" in message or "toolCalls" in message:
            blocks = list(content or [])
            blocks.extend(message.pop("tool_calls", message.pop("toolCalls", [])) or [])
            message["content"] = blocks
        return message

    # Convert legacy tool messages and reconcile result details and errors.
    #
    # 转换旧版工具消息，并合并结果详情与错误信息。
    if role == "tool":
        message["role"] = "toolResult"
        message["toolName"] = message.pop("name", message.get("toolName", "unknown"))
        message["toolCallId"] = message.pop("tool_call_id", message.get("toolCallId", ""))
        message["isError"] = not bool(message.pop("ok", True))
        content = message.get("content", "")
        if isinstance(content, str):
            message["content"] = [{"type": "text", "text": content}] if content else []
        data = message.pop("data", None)
        details = message.get("details")
        if isinstance(data, dict) and isinstance(details, dict):
            message["details"] = {**data, **details}
        elif details is None and data is not None:
            message["details"] = data
        error = message.pop("error", None)
        if error and not message["content"]:
            message["content"] = [{"type": "text", "text": str(error)}]
        return message

    return message
