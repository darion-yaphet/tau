"""Shared OpenAI prompt-cache affinity helpers.

OpenAI 提示词缓存亲和性共享辅助函数。
"""

from __future__ import annotations

from urllib.parse import urlsplit

OPENAI_API_HOST = "api.openai.com"
OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH = 64


def openai_prompt_cache_key(session_id: str | None) -> str | None:
    """Return a non-empty session-derived key within OpenAI's 64-char limit.

    返回由会话派生且不超过 OpenAI 64 字符限制的非空键。
    """
    if not session_id:
        return None
    return session_id[:OPENAI_PROMPT_CACHE_KEY_MAX_LENGTH]


def is_direct_openai_url(base_url: str) -> bool:
    """Return whether a URL targets OpenAI's first-party API host.

    返回 URL 是否指向 OpenAI 的第一方 API 主机。
    """
    return urlsplit(base_url).hostname == OPENAI_API_HOST
