"""Optional runtime model-catalog discovery contracts for provider adapters.

供模型适配器选择性实现的运行时模型目录发现协议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from tau_ai.model_limits import RuntimeModelLimits

RuntimeInputModality = Literal["text", "image"]
RuntimeThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    """One model advertised by an authenticated provider catalog.

    已认证的模型提供者目录中公布的一个模型。
    """

    id: str
    name: str | None = None
    limits: RuntimeModelLimits | None = None
    input_modalities: tuple[RuntimeInputModality, ...] = ("text",)
    thinking_levels: tuple[RuntimeThinkingLevel, ...] = ()
    default_thinking_level: RuntimeThinkingLevel | None = None

    def __post_init__(self) -> None:
        """Validate the model identifier and advertised capabilities.

        验证模型标识符及其公布的能力。
        """
        if not self.id:
            raise ValueError("model id must be non-empty")
        if not self.input_modalities:
            raise ValueError("input_modalities must be non-empty")
        if self.default_thinking_level is not None and (
            self.default_thinking_level not in self.thinking_levels
        ):
            raise ValueError("default_thinking_level must be advertised in thinking_levels")


@dataclass(frozen=True, slots=True)
class RuntimeModelCatalog:
    """Complete account-specific model snapshot from a provider.

    模型提供者返回的完整账户专属模型快照。
    """

    models: tuple[RuntimeModel, ...]

    def model(self, model_id: str) -> RuntimeModel | None:
        """Return one advertised model by exact ID.

        按精确 ID 返回一个已公布的模型。
        """
        return next((model for model in self.models if model.id == model_id), None)


@runtime_checkable
class ModelCatalogProvider(Protocol):
    """Optional provider capability for authenticated model discovery.

    用于已认证模型发现的可选提供者能力。
    """

    async def discover_models(self) -> RuntimeModelCatalog:
        """Return the complete model inventory available to the active account.

        返回当前账户可用的完整模型清单。
        """
        ...
