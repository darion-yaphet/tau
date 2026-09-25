"""Shared retry helpers for provider adapters.

提供商适配器共享的重试辅助函数。
"""

from __future__ import annotations

from asyncio import sleep

from tau_agent.types import JSONValue
from tau_ai._provider_events import ProviderRetryEvent
from tau_ai.provider import CancellationToken

RETRY_POLL_SECONDS = 0.05
RETRY_BASE_DELAY_SECONDS = 0.25


def retry_delay_seconds(attempt: int, *, max_delay_seconds: float) -> float:
    """Return an exponential retry delay capped by provider config.

    返回受提供商配置上限约束的指数重试延迟。
    """
    if max_delay_seconds <= 0:
        return 0.0
    base_delay = min(RETRY_BASE_DELAY_SECONDS, max_delay_seconds)
    return float(min(max_delay_seconds, base_delay * (2**attempt)))


def provider_retry_event(
    *,
    attempt: int,
    max_retries: int,
    delay_seconds: float,
    reason: str,
    data: dict[str, JSONValue] | None = None,
) -> ProviderRetryEvent:
    """Build a provider-neutral retry progress event.

    构建与具体提供商无关的重试进度事件。
    """
    next_attempt = attempt + 2
    max_attempts = max_retries + 1
    delay_suffix = f" in {delay_seconds:g}s" if delay_seconds else ""
    return ProviderRetryEvent(
        attempt=next_attempt,
        max_attempts=max_attempts,
        delay_seconds=delay_seconds,
        message=(
            f"Retrying provider request {next_attempt}/{max_attempts} after {reason}{delay_suffix}."
        ),
        data=data,
    )


async def wait_for_retry(
    delay_seconds: float,
    *,
    signal: CancellationToken | None,
) -> bool:
    """Sleep before a retry while allowing cancellation to interrupt backoff.

    在重试前等待，同时允许取消操作中断退避过程。
    """
    if delay_seconds <= 0:
        return signal is None or not signal.is_cancelled()

    remaining = delay_seconds
    while remaining > 0:
        if signal is not None and signal.is_cancelled():
            return False
        step = min(RETRY_POLL_SECONDS, remaining)
        await sleep(step)
        remaining -= step
    return signal is None or not signal.is_cancelled()
