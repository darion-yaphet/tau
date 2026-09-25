"""Thinking-mode primitives for Tau coding sessions.

Tau 编码会话的思考模式基础组件。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
ThinkingParameter = Literal["reasoning_effort", "reasoning.effort", "anthropic.thinking"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]

THINKING_LEVELS: tuple[ThinkingLevel, ...] = (
    "off",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
DEFAULT_THINKING_LEVEL: ThinkingLevel = "medium"

THINKING_LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning",
    "low": "Light reasoning",
    "medium": "Moderate reasoning",
    "high": "Deep reasoning",
    "xhigh": "Very deep reasoning",
    "max": "Maximum reasoning",
}


def normalize_thinking_level(value: str | None) -> ThinkingLevel:
    """Return a valid Tau thinking level or raise a user-facing error.

    返回有效的 Tau 思考等级，否则抛出面向用户的错误。
    """
    if value is None:
        return DEFAULT_THINKING_LEVEL
    normalized = value.strip().lower()
    if normalized in THINKING_LEVELS:
        return normalized
    allowed = ", ".join(THINKING_LEVELS)
    raise ValueError(f"Unknown thinking mode: {value}. Available modes: {allowed}")


def normalize_thinking_levels(values: Sequence[str]) -> tuple[ThinkingLevel, ...]:
    """Return a validated, duplicate-free thinking level tuple.

    返回经过验证且不含重复项的思考等级元组。
    """
    if isinstance(values, str) or not values:
        allowed = ", ".join(THINKING_LEVELS)
        raise ValueError(f"Thinking modes must be a non-empty list. Available modes: {allowed}")

    normalized = tuple(normalize_thinking_level(value) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("Thinking modes must be unique")
    return normalized


def reasoning_effort_for_level(level: str | None) -> ReasoningEffort:
    """Map Tau's UI thinking level to an OpenAI-compatible reasoning effort.

    将 Tau 界面的思考等级映射为兼容 OpenAI 的推理强度。
    """
    normalized = normalize_thinking_level(level)
    if normalized == "off":
        return "none"
    return normalized


def anthropic_thinking_budget_for_level(level: str | None) -> int | None:
    """Map Tau's UI thinking level to Anthropic extended-thinking tokens.

    将 Tau 界面的思考等级映射为 Anthropic 扩展思考令牌数。
    """
    normalized = normalize_thinking_level(level)
    if normalized == "off":
        return None
    return {
        "minimal": 1024,
        "low": 2048,
        "medium": 4096,
        "high": 8192,
        "xhigh": 16384,
        "max": 32768,
    }[normalized]


def next_thinking_level(
    current: str | None,
    *,
    available: tuple[ThinkingLevel, ...] = THINKING_LEVELS,
) -> ThinkingLevel:
    """Return the next thinking level in a stable cycle.

    按稳定循环顺序返回下一个思考等级。
    """
    if not available:
        return DEFAULT_THINKING_LEVEL
    try:
        normalized_current = normalize_thinking_level(current)
        index = available.index(normalized_current)
    except ValueError:
        return available[0]
    return available[(index + 1) % len(available)]
