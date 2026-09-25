"""HTTP client helpers shared by Tau network integrations.

Tau 网络集成共享的 HTTP 客户端辅助函数。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def normalize_proxy_url(proxy_url: str) -> str:
    """Return an httpx-compatible proxy URL.

    返回与 httpx 兼容的代理 URL。

    Some environments use ``socks://`` as a generic SOCKS proxy scheme. httpx
    accepts explicit SOCKS versions (for example ``socks5://`` and
    ``socks5h://``), but rejects the generic scheme before it can make a
    request. Treat the generic form as SOCKS5 so Tau can honor these proxy
    environment variables.

    某些环境使用 ``socks://`` 作为通用 SOCKS 代理协议。httpx 接受明确的
    SOCKS 版本（例如 ``socks5://`` 和 ``socks5h://``），但会在发出请求前
    拒绝通用协议。这里将通用形式视为 SOCKS5，使 Tau 能够遵循这些代理
    环境变量。
    """

    if proxy_url.lower().startswith("socks://"):
        return f"socks5://{proxy_url[len('socks://') :]}"
    return proxy_url


@contextmanager
def normalized_proxy_environment() -> Iterator[None]:
    """Temporarily normalize proxy environment variables for httpx construction.

    在构造 httpx 客户端期间临时规范化代理环境变量。
    """

    original: dict[str, str | None] = {}
    changed = False
    for name in _PROXY_ENV_VARS:
        value = os.environ.get(name)
        if value is None:
            continue
        normalized = normalize_proxy_url(value)
        if normalized == value:
            continue
        original[name] = value
        os.environ[name] = normalized
        changed = True

    try:
        yield
    finally:
        if changed:
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def create_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Create an ``httpx.AsyncClient`` with Tau's proxy normalization applied.

    创建应用了 Tau 代理规范化逻辑的 ``httpx.AsyncClient``。
    """

    with normalized_proxy_environment():
        return httpx.AsyncClient(**kwargs)


def get_json(url: str, *, timeout: float, follow_redirects: bool = False) -> dict[str, object]:
    """Fetch a JSON object with Tau's proxy normalization applied.

    使用 Tau 的代理规范化逻辑获取 JSON 对象。
    """

    with normalized_proxy_environment():
        response = httpx.get(url, timeout=timeout, follow_redirects=follow_redirects)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("HTTP response must be a JSON object")
    return data
