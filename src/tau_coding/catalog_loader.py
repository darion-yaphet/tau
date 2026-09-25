"""Load Tau's provider catalog from packaged and user TOML files.

从内置和用户 TOML 文件加载 Tau 的提供商目录。
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping
from contextlib import suppress
from functools import cache
from importlib.resources import files
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    ValidationError,
)

from tau_agent.types import JSONValue
from tau_coding.paths import TauPaths
from tau_coding.provider_catalog import (
    AuthMethod,
    ModelCatalogMetadata,
    ModelCostTier,
    ModelInput,
    ProviderApi,
    ProviderCatalogEntry,
    ProviderKind,
)
from tau_coding.thinking import ThinkingLevel, ThinkingParameter

CATALOG_SCHEMA_VERSION = 1
USER_CATALOG_FILENAME = "catalog.toml"

# Thinking fields are merged as a group: an overlay that sets thinking_levels
# replaces all four, mirroring _merge_provider_config in provider_config.

# 思考相关字段会作为一个整体合并：设置 thinking_levels 的覆盖层会替换全部四个
# 字段，其行为与 provider_config 中的 _merge_provider_config 一致。
_THINKING_FIELDS = ("thinking_levels", "thinking_models", "thinking_default", "thinking_parameter")

_NonEmptyString = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
_NonEmptyStringTuple = Annotated[tuple[_NonEmptyString, ...], Field(min_length=1)]
_PositiveInt = Annotated[StrictInt, Field(gt=0)]
_NonNegativeFloat = Annotated[float, Field(ge=0)]


class CatalogError(ValueError):
    """Raised when a Tau catalog file is invalid.

    当 Tau 目录文件无效时抛出。
    """


class _CatalogCostTier(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_input_tokens: _PositiveInt | None = None
    input: _NonNegativeFloat
    output: _NonNegativeFloat
    cacheRead: _NonNegativeFloat
    cacheWrite: _NonNegativeFloat
    # Optional 1-hour TTL cache-write rate (Anthropic bills those above the
    # 5-minute cacheWrite rate). Omitted from the cost dict when unset so
    # consumers can fall back to cacheWrite.

    # 可选的 1 小时 TTL 缓存写入费率（Anthropic 的该费率高于 5 分钟
    # cacheWrite 费率）。未设置时不写入费用字典，以便使用方回退到 cacheWrite。
    cacheWrite1h: _NonNegativeFloat | None = None


class _CatalogModelMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _NonEmptyString | None = None
    api: ProviderApi | None = None
    base_url: _NonEmptyString | None = None
    reasoning: StrictBool | None = None
    input: tuple[ModelInput, ...] = ()
    cost: dict[_NonEmptyString, _NonNegativeFloat] | None = None
    cost_tiers: tuple[_CatalogCostTier, ...] = ()
    context_window: _PositiveInt | None = None
    max_tokens: _PositiveInt | None = None
    headers: dict[_NonEmptyString, _NonEmptyString] = {}
    compat: dict[_NonEmptyString, Any] = {}
    thinking_level_map: dict[ThinkingLevel, _NonEmptyString] = {}
    unsupported_thinking_levels: tuple[ThinkingLevel, ...] = ()


class _CatalogProvider(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: _NonEmptyString
    display_name: _NonEmptyString
    kind: ProviderKind
    base_url: _NonEmptyString
    api_key_env: _NonEmptyString
    credential_name: _NonEmptyString | None = None
    models: _NonEmptyStringTuple
    default_model: _NonEmptyString
    docs_url: _NonEmptyString
    api: ProviderApi | None = None
    context_windows: dict[_NonEmptyString, _PositiveInt] | None = None
    headers: dict[_NonEmptyString, _NonEmptyString] = {}
    compat: dict[_NonEmptyString, Any] = {}
    model_metadata: dict[_NonEmptyString, _CatalogModelMetadata] = {}
    thinking_levels: tuple[ThinkingLevel, ...] | None = None
    thinking_models: tuple[_NonEmptyString, ...] = ()
    thinking_default: ThinkingLevel | None = None
    thinking_parameter: ThinkingParameter | None = None
    removed_models: tuple[_NonEmptyString, ...] = ()
    auth_methods: tuple[AuthMethod, ...] = ("api_key",)


class _CatalogFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    providers: tuple[_CatalogProvider, ...] = ()


def builtin_catalog_resource_text() -> str:
    """Return the packaged builtin catalog TOML text.

    返回随包分发的内置目录 TOML 文本。
    """
    return files("tau_coding").joinpath("data/catalog.toml").read_text(encoding="utf-8")


@cache
def builtin_catalog() -> tuple[ProviderCatalogEntry, ...]:
    """Return Tau's built-in catalog with generated models.dev model data.

    返回包含 models.dev 生成模型数据的 Tau 内置目录。
    """
    raw = _builtin_raw_with_generated_models()
    filtered = _apply_model_tombstones(raw, base=_builtin_raw())
    return _entries_from_raw(filtered, source="built-in catalog.toml")


@cache
def builtin_source_catalog() -> tuple[ProviderCatalogEntry, ...]:
    """Return the application-owned catalog used as generator input/fallback.

    返回由应用维护、供生成器输入或回退使用的目录。
    """
    raw = _builtin_raw()
    filtered = _apply_model_tombstones(raw, base=raw)
    return _entries_from_raw(filtered, source="built-in catalog.toml")


def user_catalog_path(paths: TauPaths | None = None) -> Path:
    """Return the user-level catalog overlay path.

    返回用户级目录覆盖文件的路径。
    """
    return (paths or TauPaths()).home / USER_CATALOG_FILENAME


def effective_catalog(paths: TauPaths | None = None) -> tuple[ProviderCatalogEntry, ...]:
    """Return bundled, refreshed, then user-overlaid provider model data.

    按内置、刷新缓存、用户覆盖的顺序合并并返回提供商模型数据。
    """
    # Core flow: start from generated/cached data, apply a user overlay when present,
    # remove tombstoned models, then validate and materialize public entries.

    # 核心流程：先读取生成数据和缓存数据，再按需应用用户覆盖层，移除已撤回模型，
    # 最后完成校验并生成公开条目。
    builtin_raw = _builtin_raw_with_cached_models(paths)
    path = user_catalog_path(paths)
    if not path.exists():
        filtered = _apply_model_tombstones(builtin_raw, base=_builtin_raw())
        return _entries_from_raw(filtered, source="effective built-in catalog")
    overlay_raw = _parse_catalog_text(path.read_text(encoding="utf-8"), source=str(path))
    _validate_catalog_root(overlay_raw, source=str(path))
    merged = _merge_raw_catalogs(builtin_raw, overlay_raw)
    filtered = _apply_model_tombstones(merged, base=_builtin_raw())
    return _entries_from_raw(filtered, source=str(path))


def save_user_catalog_entries(
    entries: Iterable[ProviderCatalogEntry],
    paths: TauPaths | None = None,
) -> Path:
    """Upsert full provider definitions into the user-level catalog file.

    将完整的提供商定义插入或更新到用户级目录文件中。
    """
    # Core flow: preserve unrelated raw providers, replace matching definitions,
    # serialize the resulting catalog, and atomically replace the user file.

    # 核心流程：保留无关的原始提供商，替换同名定义，序列化结果目录，
    # 再以原子方式替换用户文件。
    path = user_catalog_path(paths)
    if path.exists():
        raw = _parse_catalog_text(path.read_text(encoding="utf-8"), source=str(path))
        _validate_catalog_root(raw, source=str(path))
    else:
        raw = {"schema_version": CATALOG_SCHEMA_VERSION, "providers": []}

    providers = list(_raw_providers(raw))
    provider_indexes = {
        _raw_provider_name(provider): index for index, provider in enumerate(providers)
    }
    for entry in entries:
        raw_provider = _raw_provider_from_entry(entry)
        if entry.name in provider_indexes:
            providers[provider_indexes[entry.name]] = raw_provider
        else:
            provider_indexes[entry.name] = len(providers)
            providers.append(raw_provider)

    updated = {
        "schema_version": raw.get("schema_version", CATALOG_SCHEMA_VERSION),
        "providers": providers,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, _catalog_to_toml(updated))
    return path


@cache
def _builtin_raw() -> dict[str, Any]:
    """Parse and cache the packaged catalog as a raw mapping.

    将随包分发的目录解析为原始映射并缓存结果。
    """
    return _parse_catalog_text(builtin_catalog_resource_text(), source="built-in catalog.toml")


def _builtin_raw_with_cached_models(paths: TauPaths | None) -> dict[str, Any]:
    """Merge a valid cached models.dev overlay into the generated built-in catalog.

    将有效的 models.dev 缓存覆盖层合并到生成后的内置目录中。
    """
    from tau_coding.models_dev_store import cached_models_dev_catalog_overlay

    raw = _builtin_raw_with_generated_models()
    overlay = cached_models_dev_catalog_overlay(paths)
    if overlay is None:
        return raw
    merged = _merge_generated_catalog(raw, overlay)
    try:
        _entries_from_raw(
            _apply_model_tombstones(merged, base=_builtin_raw()),
            source="cached models.dev catalog",
        )
    except CatalogError:
        return raw
    return merged


@cache
def _builtin_raw_with_generated_models() -> dict[str, Any]:
    """Merge valid bundled models.dev data into the application-owned catalog.

    将有效的内置 models.dev 数据合并到由应用维护的目录中。
    """
    # Imported lazily because models_dev uses the catalog dataclasses imported by
    # this module. Missing or invalid generated data is deliberately non-fatal.

    # 这里采用延迟导入，因为 models_dev 会使用本模块导入的目录数据类。
    # 生成数据缺失或无效时会被有意视为非致命情况。
    from tau_coding.models_dev import bundled_models_dev_catalog_overlay

    raw = _builtin_raw()
    overlay = bundled_models_dev_catalog_overlay()
    if overlay is None:
        return raw
    merged = _merge_generated_catalog(raw, overlay)
    try:
        _entries_from_raw(
            _apply_model_tombstones(merged, base=raw),
            source="generated models.dev catalog",
        )
    except CatalogError:
        return raw
    return merged


def _parse_catalog_text(text: str, *, source: str) -> dict[str, Any]:
    """Parse catalog TOML text and report decoding failures with source context.

    解析目录 TOML 文本，并在解码失败时附带来源上下文。
    """
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise CatalogError(f"{source}: invalid TOML: {error}") from error


def _validate_catalog_root(raw: dict[str, Any], *, source: str) -> None:
    """Validate top-level catalog keys, schema version, and provider table shape.

    校验目录顶层键、模式版本以及提供商表的结构。
    """
    allowed = {"schema_version", "providers"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise CatalogError(f"{source}: unknown catalog keys: {', '.join(unknown)}")
    if "schema_version" not in raw:
        raise CatalogError(f"{source}: schema_version is required")
    if raw["schema_version"] != CATALOG_SCHEMA_VERSION:
        raise CatalogError(f"{source}: unsupported schema_version: {raw['schema_version']!r}")
    _raw_providers(raw)


def _merge_raw_catalogs(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge overlay provider tables over base ones; overlay values win.

    将覆盖层提供商表合并到基础表之上，并以覆盖层的值为准。
    """
    base_providers = _raw_providers(base)
    overlay_providers = _raw_providers(overlay)
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for provider in base_providers:
        name = _raw_provider_name(provider)
        by_name[name] = provider
        order.append(name)
    for provider in overlay_providers:
        name = _raw_provider_name(provider)
        if name in by_name:
            by_name[name] = _merge_raw_provider(by_name[name], provider)
        else:
            by_name[name] = provider
            order.append(name)
    return {
        "schema_version": overlay.get("schema_version", base.get("schema_version")),
        "providers": [by_name[name] for name in order],
    }


def _merge_generated_catalog(base: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    """Replace model inventories while retaining application-owned provider config.

    替换模型清单，同时保留由应用维护的提供商配置。
    """
    generated_by_name = {
        _raw_provider_name(provider): provider for provider in _raw_providers(generated)
    }
    providers: list[dict[str, Any]] = []
    for provider in _raw_providers(base):
        name = _raw_provider_name(provider)
        overlay = generated_by_name.get(name)
        if overlay is None:
            providers.append(provider)
            continue
        merged = _merge_raw_provider(provider, overlay)
        models = overlay.get("models")
        if not isinstance(models, list) or not models:
            providers.append(provider)
            continue
        merged["models"] = list(models)
        allowed = set(models)
        for field in ("context_windows", "model_metadata"):
            values = merged.get(field)
            if isinstance(values, dict):
                merged[field] = {
                    model: value for model, value in values.items() if model in allowed
                }
        thinking_models = merged.get("thinking_models")
        if isinstance(thinking_models, list):
            merged["thinking_models"] = [model for model in thinking_models if model in allowed]
        if merged.get("default_model") not in allowed:
            merged["default_model"] = models[0]
        providers.append(merged)
    return {"schema_version": base.get("schema_version"), "providers": providers}


def _merge_raw_provider(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge one raw provider overlay while preserving grouped field semantics.

    合并单个原始提供商覆盖层，同时保留成组字段的合并语义。
    """
    merged = {**base, **overlay}
    base_models = base.get("models", [])
    overlay_models = overlay.get("models", [])
    if isinstance(base_models, list) and isinstance(overlay_models, list):
        merged["models"] = list(dict.fromkeys([*overlay_models, *base_models]))
    base_removed = base.get("removed_models", [])
    overlay_removed = overlay.get("removed_models", [])
    if isinstance(base_removed, list) and isinstance(overlay_removed, list):
        merged["removed_models"] = list(dict.fromkeys([*base_removed, *overlay_removed]))
    for key in ("context_windows", "headers", "compat"):
        base_mapping = base.get(key)
        overlay_mapping = overlay.get(key)
        if isinstance(base_mapping, dict) and isinstance(overlay_mapping, dict):
            merged[key] = {**base_mapping, **overlay_mapping}
    base_metadata = base.get("model_metadata")
    overlay_metadata = overlay.get("model_metadata")
    if isinstance(base_metadata, dict) and isinstance(overlay_metadata, dict):
        merged["model_metadata"] = _merge_model_metadata(base_metadata, overlay_metadata)
    if "thinking_levels" in overlay:
        for field in _THINKING_FIELDS:
            if field in overlay:
                merged[field] = overlay[field]
            else:
                merged.pop(field, None)
    return merged


def _apply_model_tombstones(
    raw: dict[str, Any],
    *,
    base: dict[str, Any],
) -> dict[str, Any]:
    """Remove provider-scoped models withdrawn by bundled or user catalogs.

    移除内置目录或用户目录中标记为撤回的提供商级模型。
    """
    base_by_name = {_raw_provider_name(provider): provider for provider in _raw_providers(base)}
    providers: list[dict[str, Any]] = []
    for provider in _raw_providers(raw):
        removed = {model for model in provider.get("removed_models", []) if isinstance(model, str)}
        if not removed:
            providers.append(provider)
            continue
        filtered = {**provider}
        for field in ("models", "thinking_models"):
            values = filtered.get(field)
            if isinstance(values, list):
                filtered[field] = [model for model in values if model not in removed]
        for field in ("context_windows", "model_metadata"):
            values = filtered.get(field)
            if isinstance(values, dict):
                filtered[field] = {
                    model: value for model, value in values.items() if model not in removed
                }
        if filtered.get("default_model") in removed:
            name = _raw_provider_name(provider)
            base_default = base_by_name.get(name, {}).get("default_model")
            remaining = filtered.get("models", [])
            filtered["default_model"] = (
                base_default
                if isinstance(base_default, str) and base_default not in removed
                else remaining[0]
                if isinstance(remaining, list) and remaining
                else ""
            )
        providers.append(filtered)
    return {**raw, "providers": providers}


def _merge_model_metadata(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge per-model metadata and its nested mapping fields.

    合并逐模型元数据及其中的嵌套映射字段。
    """
    merged: dict[str, Any] = {**base}
    for model, overlay_metadata in overlay.items():
        base_metadata = merged.get(model)
        if isinstance(base_metadata, dict) and isinstance(overlay_metadata, dict):
            next_metadata = {**base_metadata, **overlay_metadata}
            for key in ("cost", "headers", "compat", "thinking_level_map"):
                base_mapping = base_metadata.get(key)
                overlay_mapping = overlay_metadata.get(key)
                if isinstance(base_mapping, dict) and isinstance(overlay_mapping, dict):
                    next_metadata[key] = {**base_mapping, **overlay_mapping}
            merged[model] = next_metadata
        else:
            merged[model] = overlay_metadata
    return merged


def _raw_providers(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return raw provider tables after validating their container shape.

    校验提供商容器结构后返回原始提供商表。
    """
    providers = raw.get("providers", [])
    if not isinstance(providers, list) or not all(isinstance(item, dict) for item in providers):
        raise CatalogError("catalog providers must be an array of tables ([[providers]])")
    return providers


def _raw_provider_name(provider: dict[str, Any]) -> str:
    """Return a normalized non-empty name from a raw provider table.

    从原始提供商表中返回规范化的非空名称。
    """
    name = provider.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CatalogError("catalog provider entries must have a non-empty string name")
    return name.strip()


def _entries_from_raw(raw: dict[str, Any], *, source: str) -> tuple[ProviderCatalogEntry, ...]:
    """Validate raw catalog data and convert it into unique provider entries.

    校验原始目录数据，并将其转换为名称唯一的提供商条目。
    """
    try:
        catalog = _CatalogFile.model_validate(raw)
    except ValidationError as error:
        raise CatalogError(f"{source}: {_format_validation_error(raw, error)}") from error
    entries = tuple(_entry_from_provider(provider, source=source) for provider in catalog.providers)
    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise CatalogError(f"{source}: duplicate provider names: {', '.join(duplicates)}")
    return entries


def _entry_from_provider(provider: _CatalogProvider, *, source: str) -> ProviderCatalogEntry:
    """Validate cross-field provider rules and build a catalog entry.

    校验提供商各字段之间的约束，并构建目录条目。
    """
    prefix = f"{source}: providers.{provider.name}"
    if provider.default_model not in provider.models:
        raise CatalogError(f"{prefix}.default_model: {provider.default_model!r} is not in models")
    for model in provider.thinking_models:
        if model not in provider.models:
            raise CatalogError(f"{prefix}.thinking_models: {model!r} is not in models")
    for model in provider.context_windows or {}:
        if model not in provider.models:
            raise CatalogError(f"{prefix}.context_windows: {model!r} is not in models")
    for model in provider.model_metadata:
        if model not in provider.models:
            raise CatalogError(f"{prefix}.model_metadata: {model!r} is not in models")
    if provider.thinking_default is not None and (
        provider.thinking_levels is None
        or provider.thinking_default not in provider.thinking_levels
    ):
        raise CatalogError(
            f"{prefix}.thinking_default: {provider.thinking_default!r} is not in thinking_levels"
        )

    for model, catalog_metadata in provider.model_metadata.items():
        _validate_cost_tiers(
            catalog_metadata.cost_tiers,
            field_name=f"{prefix}.model_metadata.{model}",
        )

    model_metadata = {
        model: _model_metadata_from_provider(metadata)
        for model, metadata in provider.model_metadata.items()
    }
    context_windows = dict(provider.context_windows or {})
    for model, metadata in model_metadata.items():
        if metadata.context_window is not None and model not in context_windows:
            context_windows[model] = metadata.context_window

    return ProviderCatalogEntry(
        name=provider.name,
        display_name=provider.display_name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_env=provider.api_key_env,
        credential_name=provider.credential_name,
        models=provider.models,
        default_model=provider.default_model,
        docs_url=provider.docs_url,
        api=provider.api,
        context_windows=context_windows or None,
        headers=dict(provider.headers),
        compat=_json_object(provider.compat, f"{prefix}.compat"),
        model_metadata=model_metadata,
        thinking_levels=provider.thinking_levels,
        thinking_models=provider.thinking_models,
        thinking_default=provider.thinking_default,
        thinking_parameter=provider.thinking_parameter,
        removed_models=provider.removed_models,
        auth_methods=provider.auth_methods,
    )


def _validate_cost_tiers(
    tiers: tuple[_CatalogCostTier, ...],
    *,
    field_name: str,
) -> None:
    """Ensure cost tier limits increase strictly and the final tier is unbounded.

    确保费用分层上限严格递增，且最后一层不设置上限。
    """
    if not tiers:
        return
    if tiers[-1].max_input_tokens is not None:
        raise CatalogError(f"{field_name}.cost_tiers: final tier must omit max_input_tokens")
    previous_limit = 0
    for index, tier in enumerate(tiers[:-1]):
        limit = tier.max_input_tokens
        if limit is None or limit <= previous_limit:
            raise CatalogError(
                f"{field_name}.cost_tiers.{index}.max_input_tokens: "
                "limits must be strictly increasing"
            )
        previous_limit = limit


def _model_metadata_from_provider(metadata: _CatalogModelMetadata) -> ModelCatalogMetadata:
    """Convert validated model metadata into the public catalog representation.

    将校验后的模型元数据转换为公开的目录表示。
    """
    thinking_level_map: dict[ThinkingLevel, str | None] = dict(metadata.thinking_level_map)
    for level in metadata.unsupported_thinking_levels:
        thinking_level_map[level] = None
    return ModelCatalogMetadata(
        name=metadata.name,
        api=metadata.api,
        base_url=metadata.base_url,
        reasoning=metadata.reasoning,
        input=metadata.input,
        cost=dict(metadata.cost) if metadata.cost else None,
        cost_tiers=tuple(
            ModelCostTier(
                max_input_tokens=tier.max_input_tokens,
                cost=_cost_tier_rates(tier),
            )
            for tier in metadata.cost_tiers
        ),
        context_window=metadata.context_window,
        max_tokens=metadata.max_tokens,
        headers=dict(metadata.headers),
        compat=_json_object(metadata.compat, "model_metadata.compat"),
        thinking_level_map=thinking_level_map,
    )


def _cost_tier_rates(tier: _CatalogCostTier) -> dict[str, float]:
    """Build the cost-rate mapping for one validated pricing tier.

    为一个已校验的计费层级构建费率映射。
    """
    rates = {
        "input": tier.input,
        "output": tier.output,
        "cacheRead": tier.cacheRead,
        "cacheWrite": tier.cacheWrite,
    }
    if tier.cacheWrite1h is not None:
        rates["cacheWrite1h"] = tier.cacheWrite1h
    return rates


def _json_object(value: Mapping[str, Any], field_name: str) -> dict[str, JSONValue]:
    """Convert a mapping into a JSON-compatible object with field-aware errors.

    将映射转换为 JSON 兼容对象，并在报错时标明字段位置。
    """
    return {key: _json_value(item, f"{field_name}.{key}") for key, item in value.items()}


def _json_value(value: Any, field_name: str) -> JSONValue:
    """Recursively validate and convert a catalog value to a JSON value.

    递归校验目录值并将其转换为 JSON 值。
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item, field_name) for item in value]
    if isinstance(value, dict):
        output: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CatalogError(f"{field_name}: object keys must be strings")
            output[key] = _json_value(item, f"{field_name}.{key}")
        return output
    raise CatalogError(f"{field_name}: unsupported value {value!r}")


def _format_validation_error(raw: dict[str, Any], error: ValidationError) -> str:
    """Format schema validation issues with readable dotted catalog locations.

    使用易读的目录点路径格式化模式校验问题。
    """
    messages = []
    for issue in error.errors():
        location = ".".join(_dotted_location(raw, issue["loc"]))
        messages.append(f"{location}: {issue['msg']}")
    return "; ".join(messages)


def _dotted_location(raw: dict[str, Any], location: tuple[int | str, ...]) -> list[str]:
    """Translate a validation location into dotted parts using provider names.

    使用提供商名称将校验位置转换为点路径片段。
    """
    parts: list[str] = []
    for part in location:
        if parts and parts[-1] == "providers" and isinstance(part, int):
            providers = raw.get("providers")
            name = None
            if isinstance(providers, list) and part < len(providers):
                item = providers[part]
                if isinstance(item, dict):
                    name = item.get("name")
            parts.append(str(name) if isinstance(name, str) else str(part))
        else:
            parts.append(str(part))
    return parts


def _raw_provider_from_entry(entry: ProviderCatalogEntry) -> dict[str, Any]:
    """Serialize a provider catalog entry into its raw TOML-ready mapping.

    将提供商目录条目序列化为可写入 TOML 的原始映射。
    """
    raw: dict[str, Any] = {
        "name": entry.name,
        "display_name": entry.display_name,
        "kind": entry.kind,
        "base_url": entry.base_url,
        "api_key_env": entry.api_key_env,
        "models": list(entry.models),
        "default_model": entry.default_model,
        "docs_url": entry.docs_url,
    }
    if entry.api is not None:
        raw["api"] = entry.api
    if entry.credential_name is not None:
        raw["credential_name"] = entry.credential_name
    if entry.context_windows:
        raw["context_windows"] = dict(entry.context_windows)
    if entry.headers:
        raw["headers"] = dict(entry.headers)
    if entry.compat:
        raw["compat"] = dict(entry.compat)
    if entry.model_metadata:
        raw["model_metadata"] = {
            model: _raw_model_metadata_from_entry(metadata)
            for model, metadata in entry.model_metadata.items()
        }
    if entry.thinking_levels is not None:
        raw["thinking_levels"] = list(entry.thinking_levels)
    if entry.thinking_models:
        raw["thinking_models"] = list(entry.thinking_models)
    if entry.thinking_default is not None:
        raw["thinking_default"] = entry.thinking_default
    if entry.thinking_parameter is not None:
        raw["thinking_parameter"] = entry.thinking_parameter
    if entry.removed_models:
        raw["removed_models"] = list(entry.removed_models)
    if entry.auth_methods != ("api_key",):
        raw["auth_methods"] = list(entry.auth_methods)
    return raw


def _raw_model_metadata_from_entry(metadata: ModelCatalogMetadata) -> dict[str, Any]:
    """Serialize model metadata while omitting unset optional fields.

    序列化模型元数据，并省略未设置的可选字段。
    """
    raw: dict[str, Any] = {}
    if metadata.name is not None:
        raw["name"] = metadata.name
    if metadata.api is not None:
        raw["api"] = metadata.api
    if metadata.base_url is not None:
        raw["base_url"] = metadata.base_url
    if metadata.reasoning is not None:
        raw["reasoning"] = metadata.reasoning
    if metadata.input:
        raw["input"] = list(metadata.input)
    if metadata.cost:
        raw["cost"] = dict(metadata.cost)
    if metadata.cost_tiers:
        raw["cost_tiers"] = [
            {
                **(
                    {"max_input_tokens": tier.max_input_tokens}
                    if tier.max_input_tokens is not None
                    else {}
                ),
                **tier.cost,
            }
            for tier in metadata.cost_tiers
        ]
    if metadata.context_window is not None:
        raw["context_window"] = metadata.context_window
    if metadata.max_tokens is not None:
        raw["max_tokens"] = metadata.max_tokens
    if metadata.headers:
        raw["headers"] = dict(metadata.headers)
    if metadata.compat:
        raw["compat"] = dict(metadata.compat)
    thinking_level_map = {
        level: value for level, value in metadata.thinking_level_map.items() if value is not None
    }
    unsupported = [level for level, value in metadata.thinking_level_map.items() if value is None]
    if thinking_level_map:
        raw["thinking_level_map"] = thinking_level_map
    if unsupported:
        raw["unsupported_thinking_levels"] = unsupported
    return raw


def _catalog_to_toml(raw: dict[str, Any]) -> str:
    """Render a raw catalog mapping as deterministic TOML text.

    将原始目录映射渲染为确定性的 TOML 文本。
    """
    lines = [f"schema_version = {raw.get('schema_version', CATALOG_SCHEMA_VERSION)}", ""]
    for provider in _raw_providers(raw):
        lines.append("[[providers]]")
        for key in (
            "name",
            "display_name",
            "kind",
            "base_url",
            "api_key_env",
            "credential_name",
            "models",
            "default_model",
            "docs_url",
            "api",
            "headers",
            "compat",
            "thinking_levels",
            "thinking_models",
            "thinking_default",
            "thinking_parameter",
            "removed_models",
            "auth_methods",
        ):
            if key in provider:
                lines.append(f"{key} = {_toml_value(provider[key])}")
        context_windows = provider.get("context_windows")
        if isinstance(context_windows, dict) and context_windows:
            lines.append("")
            lines.append("[providers.context_windows]")
            for model, context_window in context_windows.items():
                lines.append(f"{_toml_key(model)} = {_toml_value(context_window)}")
        model_metadata = provider.get("model_metadata")
        if isinstance(model_metadata, dict) and model_metadata:
            for model, metadata in model_metadata.items():
                if not isinstance(metadata, dict):
                    continue
                lines.append("")
                lines.append(f"[providers.model_metadata.{_toml_key(model)}]")
                for key, value in metadata.items():
                    lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_key(value: str) -> str:
    """Render a TOML key plainly when safe, otherwise as a quoted string.

    在安全时直接渲染 TOML 键，否则将其渲染为带引号的字符串。
    """
    if value.replace("_", "").replace("-", "").isalnum() and not value[0].isdigit():
        return value
    return json.dumps(value)


def _toml_value(value: object) -> str:
    """Render supported Python scalar and container values as TOML literals.

    将支持的 Python 标量和容器值渲染为 TOML 字面量。
    """
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{_toml_key(str(key))} = {_toml_value(item)}" for key, item in value.items()
            )
            + " }"
        )
    raise TypeError(f"Unsupported TOML value: {value!r}")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text through a temporary sibling file and atomically replace the target.

    先将文本写入同目录临时文件，再以原子方式替换目标文件。
    """
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            "w",
            dir=path.parent,
            encoding="utf-8",
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(text)
            temp_file.flush()
        temp_path.replace(path)
    except Exception:
        if temp_path is not None:
            with suppress(OSError):
                temp_path.unlink()
        raise
