"""Build-time models.dev catalog generation for Tau's bundled providers.

为 Tau 内置提供商生成构建时 models.dev 目录。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from importlib.resources import files
from typing import Any

from tau_coding.provider_catalog import ProviderCatalogEntry
from tau_coding.thinking import THINKING_LEVELS, ThinkingLevel

MODELS_DEV_URL = "https://models.dev/api.json"
NVIDIA_MODELS_URL = "https://integrate.api.nvidia.com/v1/models"
MODELS_DEV_CATALOG_RESOURCE = "data/models-dev-catalog.json"
NVIDIA_UNSUPPORTED_MODELS = {
    "abacusai/dracarys-llama-3.1-70b-instruct",
    "bytedance/seed-oss-36b-instruct",
    "deepseek-ai/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro",
    "google/gemma-2-2b-it",
    "google/gemma-3n-e2b-it",
    "google/gemma-3n-e4b-it",
    "google/gemma-4-31b-it",
    "meta/llama-3.2-1b-instruct",
    "meta/llama-4-maverick-17b-128e-instruct",
    "microsoft/phi-4-mini-instruct",
    "minimaxai/minimax-m2.7",
    "mistralai/mistral-nemotron",
    "nvidia/nemotron-mini-4b-instruct",
    "qwen/qwen3-next-80b-a3b-instruct",
    "qwen/qwen3.5-397b-a17b",
    "sarvamai/sarvam-m",
    "upstage/solar-10.7b-instruct",
}

# Tau names providers for users; models.dev names them for its own catalog.
# Tau 面向用户命名提供商；models.dev 则为自身目录命名它们。
_MODELS_DEV_PROVIDER_KEYS = {
    "kimi-code": "kimi-for-coding",
    "together": "togetherai",
}


def nvidia_model_filter(source: object, live_source: object) -> set[str]:
    """Return models.dev NVIDIA IDs accepted by Pi's live NIM filter.

    返回 Pi 实时 NIM 过滤器接受的 models.dev NVIDIA ID。
    """
    if not isinstance(source, dict) or not isinstance(live_source, dict):
        raise ValueError("NVIDIA filtering sources must be JSON objects")
    provider = source.get("nvidia")
    source_models = provider.get("models") if isinstance(provider, dict) else None
    live_models = live_source.get("data")
    if not isinstance(source_models, dict) or not isinstance(live_models, list):
        raise ValueError("NVIDIA filtering sources have invalid model data")
    live_ids = {
        model["id"].lower().replace("_", ".")
        for model in live_models
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    }
    return {
        model_id
        for model_id in source_models
        if model_id.lower().replace("_", ".") in live_ids
        and model_id not in NVIDIA_UNSUPPORTED_MODELS
    }


def thinking_level_map_from_reasoning_options(
    reasoning_options: object,
) -> dict[ThinkingLevel, str | None] | None:
    """Convert verified models.dev effort values using Pi's exact semantics.

    使用 Pi 的精确语义转换已校验的 models.dev effort 值。
    """
    if not isinstance(reasoning_options, list):
        return None

    values: set[str | None] = set()
    for option in reasoning_options:
        if not isinstance(option, Mapping) or option.get("type") != "effort":
            continue
        raw_values = option.get("values")
        if isinstance(raw_values, list):
            values.update(value for value in raw_values if isinstance(value, str) or value is None)

    # Pi emits no map when models.dev has no usable effort values. Toggle-only
    # and empty option arrays therefore retain provider/manual behavior.
    # 当 models.dev 没有可用的 effort 值时，Pi 不会生成映射。因此，仅切换和空选项
    # 数组会保留提供商或手动行为。
    selectable = set(THINKING_LEVELS) - {"off"}
    if not values or not (selectable.intersection(values) or "none" in values):
        return None

    return {
        "off": "none" if "none" in values else None,
        **{
            level: level if level in values else None for level in THINKING_LEVELS if level != "off"
        },
    }


def models_dev_catalog_document(
    source: object,
    catalog: Iterable[ProviderCatalogEntry],
    *,
    provider_model_filters: Mapping[str, set[str]] | None = None,
    generated_at: int = 0,
) -> dict[str, Any]:
    """Generate complete model inventories and metadata for Tau providers.

    为 Tau 提供商生成完整的模型清单和元数据。

    Like Pi, models.dev supplies the model rows while provider transport/auth
    configuration remains application-owned. Existing catalog-only rows are
    retained as explicit Tau corrections; source rows that are deprecated or do
    not advertise tool calling are removed unless they are the provider default.

    与 Pi 一样，models.dev 提供模型行，而提供商传输和认证配置仍由应用拥有。
    仅在目录中的现有行会作为 Tau 的显式修正保留；已弃用或未声明工具调用的源行会被
    移除，除非它们是提供商默认模型。
    """
    if not isinstance(source, Mapping):
        raise ValueError("models.dev data must be a JSON object")

    providers: dict[str, Any] = {}
    for provider in sorted(catalog, key=lambda item: item.name):
        source_key = _MODELS_DEV_PROVIDER_KEYS.get(provider.name, provider.name)
        source_provider = source.get(source_key)
        if not isinstance(source_provider, Mapping):
            continue
        source_models = source_provider.get("models")
        if not isinstance(source_models, Mapping):
            continue

        allowed_models = (provider_model_filters or {}).get(provider.name)
        eligible = {
            model_id: model
            for model_id, model in source_models.items()
            if isinstance(model_id, str)
            and isinstance(model, Mapping)
            and (allowed_models is None or model_id in allowed_models)
            and _is_eligible_model(model)
        }
        source_ids = {model_id for model_id in source_models if isinstance(model_id, str)}
        retained_manual = [
            model
            for model in provider.models
            if model not in source_ids or model == provider.default_model
        ]
        existing_eligible = [model for model in provider.models if model in eligible]
        new_models = sorted(set(eligible) - set(provider.models))
        models = list(dict.fromkeys([*existing_eligible, *retained_manual, *new_models]))
        if not models:
            continue

        metadata = {
            model_id: _model_metadata(model_id, model, provider)
            for model_id, model in sorted(eligible.items())
        }
        providers[provider.name] = {
            "models": models,
            "model_metadata": metadata,
        }

    return {
        "schema_version": 1,
        "source": MODELS_DEV_URL,
        "generated_at": generated_at,
        "providers": providers,
    }


def bundled_models_dev_catalog_document() -> dict[str, Any] | None:
    """Load and validate generated model data, or fall back silently.

    加载并校验生成的模型数据，失败时静默回退。
    """
    try:
        text = files("tau_coding").joinpath(MODELS_DEV_CATALOG_RESOURCE).read_text(encoding="utf-8")
        document = json.loads(text)
        models_dev_catalog_overlay(document)
        return document if isinstance(document, dict) else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def bundled_models_dev_catalog_overlay() -> dict[str, Any] | None:
    """Return bundled generated model data as a validated catalog overlay.

    将内置生成模型数据作为已校验的目录覆盖返回。
    """
    document = bundled_models_dev_catalog_document()
    return models_dev_catalog_overlay(document) if document is not None else None


def models_dev_catalog_overlay(document: object) -> dict[str, Any]:
    """Validate a generated document and return raw partial provider tables.

    校验生成的文档并返回原始的部分提供商表。
    """
    if not isinstance(document, Mapping) or document.get("schema_version") != 1:
        raise ValueError("models.dev catalog has an unsupported schema")
    providers = document.get("providers")
    if not isinstance(providers, Mapping):
        raise ValueError("models.dev catalog providers must be an object")

    raw_providers: list[dict[str, Any]] = []
    for provider_name, provider in providers.items():
        if not isinstance(provider_name, str) or not provider_name:
            raise ValueError("models.dev provider names must be non-empty strings")
        if not isinstance(provider, Mapping):
            raise ValueError("models.dev provider entries must be objects")
        models = provider.get("models")
        metadata = provider.get("model_metadata")
        if (
            not isinstance(models, list)
            or not models
            or not all(isinstance(model, str) and model for model in models)
            or len(models) != len(set(models))
            or not isinstance(metadata, Mapping)
        ):
            raise ValueError("models.dev provider model data is invalid")
        if any(model not in models for model in metadata):
            raise ValueError("models.dev metadata references an unknown model")
        raw_providers.append(
            {
                "name": provider_name,
                "models": list(models),
                "model_metadata": dict(metadata),
            }
        )
    return {"schema_version": 1, "providers": raw_providers}


def _is_eligible_model(model: Mapping[object, object]) -> bool:
    """Return whether a source model supports Tau's required capabilities.

    返回源模型是否支持 Tau 所需能力。
    """
    if model.get("tool_call") is not True or model.get("status") == "deprecated":
        return False
    modalities = model.get("modalities")
    if not isinstance(modalities, Mapping):
        return True
    inputs = modalities.get("input")
    outputs = modalities.get("output")
    return not (
        isinstance(inputs, list)
        and "text" not in inputs
        or isinstance(outputs, list)
        and "text" not in outputs
    )


def _model_metadata(
    model_id: str,
    model: Mapping[object, object],
    provider: ProviderCatalogEntry,
) -> dict[str, Any]:
    """Convert one models.dev row into Tau catalog metadata.

    将一条 models.dev 记录转换为 Tau 目录元数据。
    """
    modalities = model.get("modalities")
    inputs = modalities.get("input") if isinstance(modalities, Mapping) else None
    cost = model.get("cost")
    limits = model.get("limit")
    metadata: dict[str, Any] = {
        "name": _string_or(model.get("name"), model_id),
        "reasoning": model.get("reasoning") is True,
        "input": ["text", "image"] if isinstance(inputs, list) and "image" in inputs else ["text"],
        "cost": _cost_rates(cost),
        "context_window": _positive_int_or(
            limits.get("context") if isinstance(limits, Mapping) else None, 4096
        ),
        "max_tokens": _positive_int_or(
            limits.get("output") if isinstance(limits, Mapping) else None, 4096
        ),
    }
    cost_tiers = _cost_tiers(cost)
    if cost_tiers:
        metadata["cost_tiers"] = cost_tiers

    existing = provider.model_metadata.get(model_id)
    supports_direct_effort = (
        provider.kind == "openai-compatible"
        and provider.thinking_parameter in {"reasoning_effort", "reasoning.effort"}
        or provider.kind == "anthropic"
        and existing is not None
        and existing.compat.get("forceAdaptiveThinking") is True
    )
    if supports_direct_effort:
        level_map = thinking_level_map_from_reasoning_options(model.get("reasoning_options"))
        if level_map is not None:
            mapped = {level: value for level, value in level_map.items() if value is not None}
            unsupported = [level for level, value in level_map.items() if value is None]
            if mapped:
                metadata["thinking_level_map"] = mapped
            if unsupported:
                metadata["unsupported_thinking_levels"] = unsupported
    return metadata


def _cost_rates(value: object) -> dict[str, float]:
    """Normalize a models.dev cost object into Tau rate fields.

    将 models.dev 成本对象规范化为 Tau 费率字段。
    """
    cost = value if isinstance(value, Mapping) else {}
    return {
        "input": _number_or_zero(cost.get("input")),
        "output": _number_or_zero(cost.get("output")),
        "cacheRead": _number_or_zero(cost.get("cache_read")),
        "cacheWrite": _number_or_zero(cost.get("cache_write")),
    }


def _cost_tiers(value: object) -> list[dict[str, Any]]:
    """Convert context-dependent source pricing into ordered cost tiers.

    将依赖上下文的源定价转换为有序成本层级。
    """
    if not isinstance(value, Mapping):
        return []
    raw_tiers = value.get("tiers")
    if not isinstance(raw_tiers, list):
        return []
    tiers: list[tuple[int, dict[str, float]]] = []
    for raw_tier in raw_tiers:
        if not isinstance(raw_tier, Mapping):
            continue
        tier = raw_tier.get("tier")
        if not isinstance(tier, Mapping) or tier.get("type") != "context":
            continue
        size = tier.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            continue
        tiers.append((size, _cost_rates(raw_tier)))
    tiers.sort(key=lambda item: item[0])
    if not tiers:
        return []

    output: list[dict[str, Any]] = [{"max_input_tokens": tiers[0][0], **_cost_rates(value)}]
    for index, (_, rates) in enumerate(tiers):
        next_limit = tiers[index + 1][0] if index + 1 < len(tiers) else None
        output.append(
            {
                **({"max_input_tokens": next_limit} if next_limit is not None else {}),
                **rates,
            }
        )
    return output


def _string_or(value: object, fallback: str) -> str:
    """Return a non-empty string or its fallback.

    返回非空字符串，否则返回回退值。
    """
    return value if isinstance(value, str) and value else fallback


def _number_or_zero(value: object) -> float:
    """Return a numeric value as float, or zero for invalid input.

    将数值作为浮点数返回，无效输入则返回零。
    """
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def _positive_int_or(value: object, fallback: int) -> int:
    """Return a positive integer or its fallback.

    返回正整数，否则返回回退值。
    """
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else fallback
    )
