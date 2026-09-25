"""Structured diagnostic logging for coding-session failures.

记录编码会话失败的结构化诊断日志。
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from tau_agent.messages import AssistantMessage
from tau_coding.paths import TauPaths


@dataclass(frozen=True, slots=True)
class AgentCallDiagnosticContext:
    """Non-secret context attached to an agent-call diagnostic entry.

    附加到代理调用诊断条目的非敏感上下文。
    """

    provider_name: str
    model: str
    cwd: Path
    session_id: str | None
    run_id: str


class AgentCallDiagnosticLogger:
    """Append structured JSONL diagnostics for agent-call failures.

    为代理调用失败追加结构化 JSONL 诊断信息。
    """

    def __init__(self, path: Path) -> None:
        """Initialize a diagnostic logger for the target JSONL path.

        为目标 JSONL 路径初始化诊断日志记录器。
        """
        self.path = path

    @classmethod
    def from_paths(cls, paths: TauPaths | None = None) -> AgentCallDiagnosticLogger:
        """Create a logger using Tau's default path layout.

        使用 Tau 的默认路径布局创建日志记录器。
        """
        return cls((paths or TauPaths()).agent_calls_log_path)

    def log_exception(
        self,
        *,
        context: AgentCallDiagnosticContext,
        phase: str,
        exc: BaseException,
    ) -> Path:
        """Log an unexpected exception with traceback and return the log path.

        记录带堆栈跟踪的意外异常，并返回日志路径。
        """
        entry = _base_entry(context, phase=phase, kind="exception")
        entry["exception"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }
        self._append(entry)
        return self.path

    def log_assistant_error(
        self,
        *,
        context: AgentCallDiagnosticContext,
        phase: str,
        message: AssistantMessage,
    ) -> Path:
        """Log a terminal assistant error message with safe diagnostic details.

        记录终止性的助手错误消息及安全的诊断详情。
        """
        entry = _base_entry(context, phase=phase, kind="assistant_error")
        error: dict[str, Any] = {
            "message": message.error_message or "Error",
            "stop_reason": message.stop_reason,
        }
        provider = _provider_error_details(message)
        if provider:
            error["provider"] = provider
        entry["error"] = error
        self._append(entry)
        return self.path

    def log_huggingface_route_failover(
        self,
        *,
        context: AgentCallDiagnosticContext,
        failed_route: str,
        replacement_route: str | None,
        success: bool,
        error_message: str | None,
    ) -> Path:
        """Log one completed automatic Hugging Face route failover.

        记录一次已完成的 Hugging Face 自动路由故障转移。
        """
        entry = _base_entry(context, phase="agent_loop_route_failover", kind="route_failover")
        entry["route_failover"] = {
            "from": failed_route,
            "to": replacement_route,
            "success": success,
            **({"error": error_message} if error_message is not None else {}),
        }
        self._append(entry)
        return self.path

    def _append(self, entry: dict[str, Any]) -> None:
        """Append one structured diagnostic entry to the JSONL log.

        向 JSONL 日志追加一条结构化诊断记录。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, sort_keys=True) + "\n")


def new_agent_call_run_id() -> str:
    """Return a stable id for one coding-session agent call.

    为一次编码会话代理调用返回稳定标识符。
    """
    return uuid4().hex


_SAFE_ERROR_OBJECT_KEYS = ("type", "code", "message", "param")


def _provider_error_details(message: AssistantMessage) -> dict[str, Any]:
    """Extract non-secret provider failure details from message diagnostics.

    从消息诊断信息中提取非敏感的提供者失败详情。

    Provider adapters attach the raw stream event to assistant diagnostics, but
    that payload can be large. Only scalar classification fields (status codes,
    attempt counts, and error type/code/message) are copied into the log so the
    entry stays small and free of request or credential material.

    提供者适配器会把原始流事件附加到助手诊断信息中，但该载荷可能很大。
    日志只复制标量分类字段，包括状态码、尝试次数以及错误类型、代码和消息，
    从而保持条目简短，且不包含请求或凭据材料。
    """
    for diagnostic in message.diagnostics or []:
        if diagnostic.type != "provider_error" or not diagnostic.details:
            continue
        details: dict[str, Any] = {}
        status_code = diagnostic.details.get("status_code")
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            details["status_code"] = status_code
        attempts = diagnostic.details.get("attempts")
        if isinstance(attempts, int) and not isinstance(attempts, bool):
            details["attempts"] = attempts
        event = diagnostic.details.get("event")
        if isinstance(event, dict):
            event_details = _safe_stream_event_details(event)
            if event_details:
                details["event"] = event_details
        return details
    return {}


def _safe_stream_event_details(event: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-secret scalar fields from a provider stream error event.

    仅保留提供者流错误事件中的非敏感标量字段。
    """
    details: dict[str, Any] = {}
    event_type = event.get("type")
    if isinstance(event_type, str) and event_type:
        details["type"] = event_type
    sequence_number = event.get("sequence_number")
    if isinstance(sequence_number, int) and not isinstance(sequence_number, bool):
        details["sequence_number"] = sequence_number
    nested = _safe_error_object(event.get("error"))
    if nested:
        details["error"] = nested
    response = event.get("response")
    if isinstance(response, dict):
        response_error = _safe_error_object(response.get("error"))
        if response_error:
            details["response_error"] = response_error
    return details


def _safe_error_object(value: object) -> dict[str, str]:
    """Copy scalar classification fields from a provider error object.

    从提供者错误对象中复制标量分类字段。
    """
    if not isinstance(value, dict):
        return {}
    return {
        key: field
        for key in _SAFE_ERROR_OBJECT_KEYS
        if isinstance((field := value.get(key)), str) and field
    }


def _base_entry(
    context: AgentCallDiagnosticContext,
    *,
    phase: str,
    kind: str,
) -> dict[str, Any]:
    """Build the shared metadata for one diagnostic log entry.

    构建一条诊断日志记录的共享元数据。
    """
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "kind": kind,
        "phase": phase,
        "run_id": context.run_id,
        "session_id": context.session_id,
        "provider_name": context.provider_name,
        "model": context.model,
        "cwd": str(context.cwd),
    }
