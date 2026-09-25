"""Pinned llama.cpp router protocol adapter.

固定版本的 llama.cpp 路由器协议适配器。

The mutating API is used only after ``/props`` proves both router identity and
a tested build. Unknown builds intentionally fall back to OpenAI-compatible
model discovery in the owning service.

只有在 ``/props`` 同时证明路由器身份和经过测试的构建后，才会使用变更 API。未知构建
会有意回退到所属服务中的 OpenAI 兼容模型发现。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, TypeGuard, cast

import httpx

LLAMA_CPP_ROUTER_MIN_BUILD = 9688
LLAMA_CPP_ROUTER_MAX_BUILD = 10595

RouterState = Literal[
    "loaded", "sleeping", "unloaded", "loading", "downloading", "failed", "unknown"
]


class LlamaCppRouterError(RuntimeError):
    """A malformed or failed request to a confirmed compatible router.

    向已确认兼容路由器发出的格式错误或失败请求。
    """


@dataclass(frozen=True, slots=True)
class RouterCapability:
    """Detected router identity and compatibility information.

    检测到的路由器身份与兼容性信息。
    """
    role: Literal["standard", "compatible", "incompatible"]
    build: int | None = None
    diagnostic: str | None = None

    @property
    def compatible(self) -> bool:
        """Return whether the detected router build is supported.

        返回检测到的路由器构建是否受支持。
        """
        return self.role == "compatible"


@dataclass(frozen=True, slots=True)
class RouterModel:
    """One model reported by the llama.cpp router.

    llama.cpp 路由器报告的一个模型。
    """
    id: str
    state: RouterState
    display_name: str | None = None
    input_modalities: tuple[Literal["text", "image"], ...] | None = None
    failed: bool = False
    downloaded_bytes: int | None = None
    download_total_bytes: int | None = None


async def detect_router(
    client: httpx.AsyncClient,
    server_root: str,
    headers: Mapping[str, str],
) -> RouterCapability:
    """Identify only the documented router and gate it to the tested builds.

    仅识别有文档说明的路由器，并限制到经过测试的构建。
    """
    response = await client.get(server_root + "/props", headers=dict(headers))
    if response.status_code == 404:
        return RouterCapability("standard")
    _raise_http(response, "detecting router capabilities")
    payload = _object(response, "/props")
    if payload.get("role") != "router":
        return RouterCapability("standard")
    build_info = payload.get("build_info")
    match = re.match(r"^b(\d+)(?:-|$)", build_info) if isinstance(build_info, str) else None
    if match is None:
        return RouterCapability(
            "incompatible",
            diagnostic=(
                "Router build is unknown; management is disabled and Tau is using "
                "standard OpenAI-compatible discovery."
            ),
        )
    build = int(match.group(1))
    if not LLAMA_CPP_ROUTER_MIN_BUILD <= build <= LLAMA_CPP_ROUTER_MAX_BUILD:
        return RouterCapability(
            "incompatible",
            build,
            (
                f"Router build b{build} is outside Tau's tested range "
                f"b{LLAMA_CPP_ROUTER_MIN_BUILD}-b{LLAMA_CPP_ROUTER_MAX_BUILD}; "
                "management is disabled."
            ),
        )
    return RouterCapability("compatible", build)


async def list_router_models(
    client: httpx.AsyncClient,
    server_root: str,
    headers: Mapping[str, str],
    *,
    reload: bool = False,
) -> tuple[RouterModel, ...]:
    response = await client.get(
        server_root + "/models",
        params={"reload": "1"} if reload else None,
        headers=dict(headers),
    )
    _raise_http(response, "listing router models")
    payload = _object(response, "/models")
    data = payload.get("data")
    if not isinstance(data, list):
        raise LlamaCppRouterError("llama.cpp /models returned a malformed model list.")
    result: list[RouterModel] = []
    ids: set[str] = set()
    for raw in data:
        if not isinstance(raw, Mapping):
            raise LlamaCppRouterError("llama.cpp /models returned a malformed model.")
        model_id = raw.get("id")
        if not isinstance(model_id, str) or not model_id or model_id != model_id.strip():
            raise LlamaCppRouterError("llama.cpp /models returned a model without an exact id.")
        if model_id in ids:
            raise LlamaCppRouterError("llama.cpp /models returned duplicate model ids.")
        ids.add(model_id)
        status = raw.get("status")
        value = status.get("value") if isinstance(status, Mapping) else None
        failed = bool(status.get("failed")) if isinstance(status, Mapping) else False
        state: RouterState
        if failed:
            state = "failed"
        elif value in {"loaded", "sleeping", "unloaded", "loading", "downloading"}:
            state = cast(RouterState, value)
        else:
            state = "unknown"
        architecture = raw.get("architecture")
        modalities_raw = (
            architecture.get("input_modalities") if isinstance(architecture, Mapping) else None
        )
        modalities = None
        if (
            isinstance(modalities_raw, list)
            and modalities_raw
            and all(item in {"text", "image"} for item in modalities_raw)
        ):
            modalities = cast(
                tuple[Literal["text", "image"], ...], tuple(dict.fromkeys(modalities_raw))
            )
        display = raw.get("name", raw.get("display_name"))
        downloaded_bytes, download_total_bytes = _download_progress(status)
        result.append(
            RouterModel(
                id=model_id,
                state=state,
                display_name=(
                    display if isinstance(display, str) and display.strip() else model_id
                ),
                input_modalities=modalities,
                failed=failed,
                downloaded_bytes=downloaded_bytes,
                download_total_bytes=download_total_bytes,
            )
        )
    return tuple(result)


def _download_progress(status: object) -> tuple[int | None, int | None]:
    if not isinstance(status, Mapping):
        return None, None
    progress = status.get("progress")
    if not isinstance(progress, Mapping):
        return None, None
    downloaded = 0.0
    total = 0.0
    found = False
    for value in progress.values():
        if not isinstance(value, Mapping):
            continue
        done = value.get("done")
        size = value.get("total")
        if not _is_non_negative_number(done) or not _is_non_negative_number(size):
            continue
        downloaded += float(done)
        total += float(size)
        found = True
    if not found or total <= 0:
        return None, None
    return int(downloaded), int(total)


def _is_non_negative_number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


async def watch_router_download_progress(
    client: httpx.AsyncClient,
    server_root: str,
    headers: Mapping[str, str],
    model_id: str,
    callback: Callable[[int, int], None],
) -> None:
    """Forward bounded aggregate download progress from the router SSE stream.

    从路由器 SSE 流转发有界的汇总下载进度。
    """
    async with client.stream(
        "GET",
        server_root + "/models/sse",
        headers=dict(headers),
        timeout=None,
    ) as response:
        _raise_http(response, "watching model progress")
        data_lines: list[str] = []
        data_size = 0
        async for line in response.aiter_lines():
            if line:
                if line.startswith("data:"):
                    value = line[5:].lstrip()
                    data_size += len(value)
                    if data_size <= 1_000_000:
                        data_lines.append(value)
                continue
            if data_lines and data_size <= 1_000_000:
                _forward_download_progress("\n".join(data_lines), model_id, callback)
            data_lines = []
            data_size = 0
        if data_lines and data_size <= 1_000_000:
            _forward_download_progress("\n".join(data_lines), model_id, callback)


def _forward_download_progress(
    data: str,
    model_id: str,
    callback: Callable[[int, int], None],
) -> None:
    try:
        event = json.loads(data)
    except (json.JSONDecodeError, ValueError):
        return
    if not isinstance(event, Mapping):
        return
    if event.get("model") != model_id or event.get("event") != "download_progress":
        return
    downloaded, total = _download_progress(event.get("data"))
    if downloaded is not None and total is not None:
        callback(downloaded, total)


async def mutate_router_model(
    client: httpx.AsyncClient,
    server_root: str,
    headers: Mapping[str, str],
    *,
    action: Literal["load", "unload", "download"],
    model_id: str,
) -> None:
    endpoint = "/models" if action == "download" else f"/models/{action}"
    response = await client.post(
        server_root + endpoint,
        headers=dict(headers),
        json={"model": model_id},
    )
    _raise_http(response, f"requesting model {action}")
    payload = _object(response, endpoint)
    if payload.get("success") is not True:
        raise LlamaCppRouterError(f"llama.cpp did not accept the model {action} request.")


def _object(response: httpx.Response, endpoint: str) -> Mapping[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise LlamaCppRouterError(f"llama.cpp {endpoint} returned malformed JSON.") from exc
    if not isinstance(payload, Mapping):
        raise LlamaCppRouterError(f"llama.cpp {endpoint} returned malformed JSON.")
    return payload


def _raise_http(response: httpx.Response, operation: str) -> None:
    if response.status_code in {401, 403}:
        raise LlamaCppRouterError(
            "llama.cpp rejected the router request. Check the optional API key or LLAMA_API_KEY."
        )
    if response.status_code >= 400:
        detail = _server_error_detail(response)
        suffix = f": {detail}" if detail else "."
        raise LlamaCppRouterError(
            f"llama.cpp returned HTTP {response.status_code} while {operation}{suffix}"
        )


def _server_error_detail(response: httpx.Response) -> str | None:
    """Extract one bounded, user-actionable message from a router error.

    从路由器错误中提取一条有界且用户可操作的消息。
    """
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    message = error.get("message") if isinstance(error, Mapping) else None
    if not isinstance(message, str):
        return None
    normalized = " ".join(message.split())
    if not normalized:
        return None
    return normalized[:300]


__all__ = [
    "LLAMA_CPP_ROUTER_MAX_BUILD",
    "LLAMA_CPP_ROUTER_MIN_BUILD",
    "LlamaCppRouterError",
    "RouterCapability",
    "RouterModel",
    "RouterState",
    "detect_router",
    "list_router_models",
    "mutate_router_model",
    "watch_router_download_progress",
]
