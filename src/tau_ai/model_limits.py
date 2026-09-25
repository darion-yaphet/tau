"""Optional runtime model-limit discovery contracts for provider adapters.

供模型适配器选择性实现的运行时模型限制发现协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class RuntimeModelLimits:
    """Provider-reported limits for one model on the active serving surface.

    模型提供者针对当前服务端面公布的单个模型限制。
    """

    context_window: int
    max_output_tokens: int | None = None
    effective_context_window_percent: int = 100
    auto_compact_token_limit: int | None = None

    def __post_init__(self) -> None:
        """Validate the provider-reported token limits and percentages.

        验证模型提供者公布的令牌限制和百分比。
        """
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if self.max_output_tokens is not None and self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if not 1 <= self.effective_context_window_percent <= 100:
            raise ValueError("effective_context_window_percent must be between 1 and 100")
        if self.auto_compact_token_limit is not None and self.auto_compact_token_limit <= 0:
            raise ValueError("auto_compact_token_limit must be positive")

    @property
    def effective_context_window(self) -> int:
        """Return the provider's usable window after its requested headroom.

        扣除配置的预留空间后，返回模型提供者可用的上下文窗口。
        """
        return max(1, self.context_window * self.effective_context_window_percent // 100)

    @property
    def effective_auto_compact_token_limit(self) -> int:
        """Return an explicit limit or the Codex-compatible 90% default.

        返回显式配置的限制；未配置时使用兼容 Codex 的 90% 默认值。
        """
        default_limit = max(1, self.context_window * 9 // 10)
        if self.auto_compact_token_limit is None:
            return min(default_limit, self.effective_context_window)
        return min(self.auto_compact_token_limit, self.effective_context_window)


@runtime_checkable
class ModelLimitsProvider(Protocol):
    """Optional provider capability for serving-surface-specific model limits.

    用于发现特定服务端面模型限制的可选提供者能力。
    """

    async def discover_model_limits(self, model: str) -> RuntimeModelLimits | None:
        """Return live limits for ``model``, or ``None`` when it is not advertised.

        返回 ``model`` 的实时限制；若未公布该模型，则返回 ``None``。
        """
        ...
