"""Provider-neutral local-backend contracts and generation-local registry.

与提供者无关的本地后端协议和代际内注册表。

Backends describe protocol work and structured data.  They never construct UI
widgets or receive a frontend object.  The registry owns source/generation
identity, cancellation, and stale-result containment; the host owns rendering
and session/model selection.

后端描述协议工作和结构化数据，不会构造 UI 控件，也不会接收前端对象。注册表负责
来源/代际身份、取消和过期结果收容；宿主负责渲染以及会话和模型选择。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

from tau_agent.harness import SimpleCancellationToken
from tau_coding.extensions.provider_registry import (
    DynamicProviderRegistry,
    ProviderLayerToken,
)

LOCAL_OPERATION_CANCELLATION_TIMEOUT_SECONDS = 0.25
_SUPERVISED_LOCAL_OPERATION_TASKS: set[asyncio.Task[object]] = set()

LocalFieldKind = Literal["text", "secret", "choice"]
LocalConnectionState = Literal[
    "unconfigured",
    "connecting",
    "loading",
    "ready",
    "unavailable",
    "stale",
    "error",
    "cancelled",
]
LocalAction = Literal[
    "configure",
    "refresh",
    "use",
    "doctor",
    "reset",
    "load_model",
    "unload_model",
    "download_model",
    "search_models",
]
LocalDiagnosticSeverity = Literal["info", "warning", "error"]


class LocalBackendError(ValueError):
    """Raised when a backend contract or registration is invalid.

    后端协议或注册项无效时抛出。
    """


class LocalOperationError(RuntimeError):
    """Raised for host-owned local operation failures.

    宿主管理的本地操作失败时抛出。
    """


@dataclass(frozen=True, slots=True)
class LocalConfigField:
    """One host-rendered configuration field.

    一个由宿主渲染的配置字段。

    ``secret`` values are accepted only through :class:`LocalConfigValues` and
    are deliberately excluded from reprs and diagnostics by the host.

    ``secret`` 值只能通过 :class:`LocalConfigValues` 接收，且宿主会有意将其从 repr 和
    诊断信息中排除。
    """

    key: str
    label: str
    kind: LocalFieldKind
    required: bool = False
    placeholder: str | None = None
    choices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate field names, types, and choices before exposing the schema.

        在公开配置模式前验证字段名称、类型和选项。
        """
        _non_empty(self.key, "Configuration field key")
        _non_empty(self.label, "Configuration field label")
        if self.kind not in {"text", "secret", "choice"}:
            raise LocalBackendError("Configuration field kind is unsupported")
        if not isinstance(self.required, bool):
            raise LocalBackendError("Configuration field required flag must be boolean")
        if self.placeholder is not None and not isinstance(self.placeholder, str):
            raise LocalBackendError("Configuration field placeholder must be a string or None")
        if not isinstance(self.choices, tuple) or any(
            not isinstance(choice, str) or not choice for choice in self.choices
        ):
            raise LocalBackendError("Configuration choices must be non-empty strings")
        if self.kind == "choice" and not self.choices:
            raise LocalBackendError("Choice configuration fields must define choices")
        if self.key != self.key.strip():
            raise LocalBackendError("Configuration field key must not have surrounding whitespace")
        if self.kind != "choice" and self.choices:
            raise LocalBackendError("Only choice configuration fields may define choices")
        if len(set(self.choices)) != len(self.choices):
            raise LocalBackendError("Configuration choices must be unique")


@dataclass(frozen=True, slots=True)
class LocalConfigureSpec:
    """Complete configuration schema rendered by the host.

    由宿主渲染的完整配置模式。
    """

    fields: tuple[LocalConfigField, ...] = ()

    def __post_init__(self) -> None:
        """Validate field values and ensure keys are unique.

        验证字段值并确保键名唯一。
        """
        if not isinstance(self.fields, tuple) or any(
            not isinstance(field, LocalConfigField) for field in self.fields
        ):
            raise LocalBackendError("Configuration fields must be LocalConfigField values")
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise LocalBackendError("Configuration field keys must be unique")


ReadConfigureSpec = LocalConfigureSpec | Callable[[], LocalConfigureSpec]


@dataclass(frozen=True, slots=True)
class LocalConfigValues(Mapping[str, str]):
    """Ephemeral submitted configuration values.

    临时提交的配置值。

    The mapping remains usable by backend code, but its values never appear in
    reprs.  Backends should not retain it after their transactional configure
    callback returns.

    后端代码仍可使用此映射，但其中的值不会出现在 repr 中。事务式 configure 回调返回后，
    后端不应继续保留此映射。
    """

    _values: Mapping[str, str] = field(repr=False)
    secret_keys: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        """Copy submitted strings into an immutable, secret-aware mapping.

        将提交的字符串复制到不可变且可标记机密字段的映射中。
        """
        copied: dict[str, str] = {}
        for key, value in self._values.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise LocalBackendError("Configuration values must map strings to strings")
            copied[key] = value
        if not self.secret_keys.issubset(copied):
            raise LocalBackendError("Secret configuration keys must be submitted values")
        object.__setattr__(self, "_values", MappingProxyType(copied))
        object.__setattr__(self, "secret_keys", frozenset(self.secret_keys))

    def __getitem__(self, key: str) -> str:
        """Return the submitted value for one field key.

        返回指定字段键对应的提交值。
        """
        return self._values[key]

    def __iter__(self):  # type: ignore[no-untyped-def]
        """Iterate over submitted configuration field keys.

        遍历已提交配置字段的键。
        """
        return iter(self._values)

    def __len__(self) -> int:
        """Return the number of submitted configuration fields.

        返回已提交配置字段的数量。
        """
        return len(self._values)

    def __repr__(self) -> str:
        """Represent only field names so secret values remain hidden.

        仅表示字段名称，以隐藏机密值。
        """
        return f"LocalConfigValues(keys={tuple(self._values)!r})"


@dataclass(frozen=True, slots=True)
class LocalModel:
    """Safe model information returned by a local backend.

    本地后端返回的安全模型信息。
    """

    id: str
    display_name: str | None = None
    state: str | None = None

    def __post_init__(self) -> None:
        """Validate model identifiers and optional display fields.

        验证模型标识符和可选的显示字段。
        """
        _identifier(self.id, "Local model id")
        if self.display_name is not None:
            _non_empty(self.display_name, "Local model display name")
        if self.state is not None:
            _non_empty(self.state, "Local model state")


@dataclass(frozen=True, slots=True)
class LocalDiagnostic:
    """Secret-free diagnostic suitable for host rendering.

    适合由宿主渲染且不包含机密的诊断信息。
    """

    message: str
    severity: LocalDiagnosticSeverity = "info"
    stage: str | None = None

    def __post_init__(self) -> None:
        """Validate diagnostic content and severity.

        验证诊断内容和严重级别。
        """
        _non_empty(self.message, "Diagnostic message")
        if self.severity not in {"info", "warning", "error"}:
            raise LocalBackendError("Diagnostic severity is unsupported")
        if self.stage is not None:
            _non_empty(self.stage, "Diagnostic stage")


@dataclass(frozen=True, slots=True)
class LocalProgress:
    """Bounded progress update emitted by a backend operation.

    后端操作发出的有界进度更新。
    """

    message: str
    fraction: float | None = None
    done: bool = False

    def __post_init__(self) -> None:
        """Validate the progress message, fraction, and completion flag.

        验证进度消息、比例和完成标记。
        """
        _non_empty(self.message, "Progress message")
        if self.fraction is not None and not 0 <= self.fraction <= 1:
            raise LocalBackendError("Progress fraction must be between 0 and 1")
        if not isinstance(self.done, bool):
            raise LocalBackendError("Progress done flag must be boolean")


@dataclass(frozen=True, slots=True)
class LocalArtifactOption:
    """One backend-neutral variant offered for a discovered artifact.

    为发现的资源提供的一个通用变体选项。
    """

    id: str
    label: str
    size_bytes: int | None = None
    recommended: bool = False

    def __post_init__(self) -> None:
        """Validate an artifact variant and its optional size metadata.

        验证资源变体及其可选的大小元数据。
        """
        _identifier(self.id, "Artifact option id")
        _non_empty(self.label, "Artifact option label")
        if self.size_bytes is not None and (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise LocalBackendError("Artifact option size must be a non-negative integer")
        if not isinstance(self.recommended, bool):
            raise LocalBackendError("Artifact option recommended flag must be boolean")


@dataclass(frozen=True, slots=True)
class LocalSearchResult:
    """One host-renderable backend search result and its selectable variants.

    一条可由宿主渲染的后端搜索结果及其可选变体。
    """

    id: str
    label: str
    restricted: bool = False
    options: tuple[LocalArtifactOption, ...] = ()
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        """Validate result fields and ensure option identifiers are unique.

        验证结果字段并确保选项标识符唯一。
        """
        _identifier(self.id, "Search result id")
        _non_empty(self.label, "Search result label")
        if not isinstance(self.restricted, bool):
            raise LocalBackendError("Search result restricted flag must be boolean")
        if not isinstance(self.options, tuple) or any(
            not isinstance(option, LocalArtifactOption) for option in self.options
        ):
            raise LocalBackendError("Search result options are malformed")
        if len({option.id for option in self.options}) != len(self.options):
            raise LocalBackendError("Search result option ids must be unique")
        if self.diagnostic is not None:
            _non_empty(self.diagnostic, "Search result diagnostic")


@dataclass(frozen=True, slots=True)
class LocalConfirmationChoice:
    """One host-rendered option answering a backend confirmation request.

    用于回答后端确认请求的一个宿主渲染选项。
    """

    value: str
    label: str
    recommended: bool = False

    def __post_init__(self) -> None:
        """Validate a confirmation choice and its recommended flag.

        验证确认选项及其推荐标记。
        """
        _identifier(self.value, "Confirmation choice value")
        _non_empty(self.label, "Confirmation choice label")
        if not isinstance(self.recommended, bool):
            raise LocalBackendError("Confirmation choice recommended flag must be boolean")


@dataclass(frozen=True, slots=True)
class LocalConfirmationRequest:
    """An explicit decision a backend needs before an operation may proceed.

    后端在继续操作前需要用户明确作出的决定。

    Backends declare structured questions; the host renders, confirms, and
    resubmits the chosen value through the operation context.  A request is
    never an instruction for the host to guess: an unanswered request leaves
    the operation uncommitted.

    后端声明结构化问题，由宿主进行渲染、确认，并通过操作上下文重新提交所选值。
    它绝不是让宿主自行猜测的指令：如果没有得到回答，操作就不会提交。
    """

    message: str
    choices: tuple[LocalConfirmationChoice, ...]

    def __post_init__(self) -> None:
        """Validate the request and its unique choices.

        验证确认请求及其唯一选项。
        """
        _non_empty(self.message, "Confirmation request message")
        if not isinstance(self.choices, tuple) or not self.choices:
            raise LocalBackendError("Confirmation request choices must be a non-empty tuple")
        if any(not isinstance(choice, LocalConfirmationChoice) for choice in self.choices):
            raise LocalBackendError("Confirmation request choices are malformed")
        values = [choice.value for choice in self.choices]
        if len(set(values)) != len(values):
            raise LocalBackendError("Confirmation request choice values must be unique")
        if sum(1 for choice in self.choices if choice.recommended) > 1:
            raise LocalBackendError("At most one confirmation choice may be recommended")


@dataclass(frozen=True, slots=True)
class LocalBackendStatus:
    """Structured status snapshot; no backend-specific protocol vocabulary.

    结构化状态快照，不包含特定后端协议术语。
    """

    state: LocalConnectionState
    endpoint_display: str | None = None
    authentication_source: Literal["none", "environment", "stored credential"] = "none"
    models: tuple[LocalModel, ...] = ()
    selected_model: str | None = None
    actions: tuple[LocalAction, ...] = ()
    diagnostics: tuple[LocalDiagnostic, ...] = ()
    cached: bool = False
    stale: bool = False

    def __post_init__(self) -> None:
        """Validate the connection state, models, actions, and diagnostics.

        验证连接状态、模型、操作和诊断信息。
        """
        if self.state not in {
            "unconfigured",
            "connecting",
            "loading",
            "ready",
            "unavailable",
            "stale",
            "error",
            "cancelled",
        }:
            raise LocalBackendError("Local connection state is unsupported")
        if self.endpoint_display is not None:
            _non_empty(self.endpoint_display, "Endpoint display value")
        if self.authentication_source not in {"none", "environment", "stored credential"}:
            raise LocalBackendError("Authentication source is unsupported")
        if not isinstance(self.models, tuple) or any(
            not isinstance(model, LocalModel) for model in self.models
        ):
            raise LocalBackendError("Local models must be LocalModel values")
        model_ids = [model.id for model in self.models]
        if len(model_ids) != len(set(model_ids)):
            raise LocalBackendError("Local model ids must be unique")
        if self.selected_model is not None and self.selected_model not in model_ids:
            raise LocalBackendError("Selected model must belong to the status snapshot")
        if not isinstance(self.actions, tuple) or any(
            action
            not in {
                "configure",
                "refresh",
                "use",
                "doctor",
                "reset",
                "load_model",
                "unload_model",
                "download_model",
                "search_models",
            }
            for action in self.actions
        ):
            raise LocalBackendError("Local actions are unsupported")
        if len(set(self.actions)) != len(self.actions):
            raise LocalBackendError("Local actions must be unique")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, LocalDiagnostic) for item in self.diagnostics
        ):
            raise LocalBackendError("Local diagnostics must be structured values")


@dataclass(frozen=True, slots=True)
class LocalOperationContext:
    """Inputs owned by one backend operation.

    由一次后端操作拥有的输入和回调。
    """

    signal: SimpleCancellationToken
    action: LocalAction
    generation_id: str
    backend_id: str
    source_id: str
    _is_current: Callable[[], bool] = field(repr=False)
    _progress: Callable[[LocalProgress], None] = field(repr=False)
    confirmation: str | None = None

    @property
    def cancelled(self) -> bool:
        """Return whether cancellation was requested for this operation.

        返回此操作是否已收到取消请求。
        """
        return self.signal.is_cancelled()

    @property
    def stale(self) -> bool:
        """Return whether the operation's backend layer is no longer current.

        返回此操作所属的后端层是否已不再有效。
        """
        return not self._is_current()

    def report_progress(self, progress: LocalProgress) -> None:
        """Publish one structured progress event to the host.

        向宿主发布一条结构化进度事件。
        """
        if not self.cancelled and not self.stale:
            self._progress(progress)


@dataclass(frozen=True, slots=True)
class LocalOperationResult:
    """Common host-renderable operation result.

    通用的宿主可渲染操作结果。
    """

    backend_status: LocalBackendStatus | None = None
    message: str | None = None
    diagnostics: tuple[LocalDiagnostic, ...] = ()
    progress: tuple[LocalProgress, ...] = ()
    cancelled: bool = False
    stale: bool = False
    committed: bool = False
    field_errors: Mapping[str, str] = field(default_factory=dict)
    credential_orphaned: bool = False
    confirmation: LocalConfirmationRequest | None = None
    search_results: tuple[LocalSearchResult, ...] = ()

    def __post_init__(self) -> None:
        """Validate all structured fields in the host-facing result.

        验证宿主可见结果中的所有结构化字段。
        """
        if self.message is not None and not isinstance(self.message, str):
            raise LocalBackendError("Operation message must be a string or None")
        if not isinstance(self.diagnostics, tuple) or any(
            not isinstance(item, LocalDiagnostic) for item in self.diagnostics
        ):
            raise LocalBackendError("Operation diagnostics must be structured values")
        if not isinstance(self.progress, tuple) or any(
            not isinstance(item, LocalProgress) for item in self.progress
        ):
            raise LocalBackendError("Operation progress must be structured values")
        if not isinstance(self.field_errors, Mapping):
            raise LocalBackendError("Configuration field errors must be a mapping")
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.field_errors.items()
        ):
            raise LocalBackendError("Configuration field errors must contain strings")
        if self.confirmation is not None and not isinstance(
            self.confirmation, LocalConfirmationRequest
        ):
            raise LocalBackendError("Operation confirmation must be a structured request")
        if self.confirmation is not None and self.committed:
            raise LocalBackendError("A committed operation cannot request confirmation")
        if not isinstance(self.search_results, tuple) or any(
            not isinstance(item, LocalSearchResult) for item in self.search_results
        ):
            raise LocalBackendError("Operation search results are malformed")


@dataclass(frozen=True, slots=True)
class LocalConfigureResult:
    """Backend result for a transactional configuration attempt.

    后端对一次事务式配置尝试返回的结果。
    """

    committed: bool = False
    backend_status: LocalBackendStatus | None = None
    message: str | None = None
    field_errors: Mapping[str, str] = field(default_factory=dict)
    diagnostics: tuple[LocalDiagnostic, ...] = ()
    credential_orphaned: bool = False


@dataclass(frozen=True, slots=True)
class LocalBackendLayerToken:
    backend_id: str
    source_id: str
    generation_id: str
    layer_id: str
    provider_layer: ProviderLayerToken


@dataclass(frozen=True, slots=True)
class LocalBackendView:
    """A backend plus effective-source information for host rendering.

    后端及其生效来源信息，供宿主渲染使用。
    """

    backend: LocalBackend
    token: LocalBackendLayerToken
    recommended: bool = False
    provider_layer_effective: bool = True

    @property
    def use_available(self) -> bool:
        """Return whether the provider layer permits using this backend.

        返回对应提供者层是否允许使用此后端。
        """
        return self.provider_layer_effective


class ProgressCallback(Protocol):
    """Callable contract for receiving structured backend progress.

    接收结构化后端进度的可调用协议。
    """

    def __call__(self, progress: LocalProgress) -> None:
        """Receive one structured backend progress event.

        接收一个结构化后端进度事件。
        """
        ...


ConfigureLocalBackend = Callable[
    [LocalConfigValues, LocalOperationContext],
    LocalConfigureResult
    | LocalOperationResult
    | Awaitable[LocalConfigureResult | LocalOperationResult],
]
ReadLocalBackendStatus = Callable[
    [LocalOperationContext],
    LocalBackendStatus
    | LocalOperationResult
    | Awaitable[LocalBackendStatus | LocalOperationResult],
]
RefreshLocalBackend = ReadLocalBackendStatus
DoctorLocalBackend = ReadLocalBackendStatus
ResetLocalBackend = ReadLocalBackendStatus
ManageModel = Callable[
    [str, LocalOperationContext],
    LocalOperationResult | Awaitable[LocalOperationResult],
]
DownloadModel = ManageModel
SearchModels = ManageModel


@dataclass(frozen=True, slots=True)
class LocalBackend:
    """Provider-neutral local backend capability declaration.

    与提供者无关的本地后端能力声明。
    """

    id: str
    provider_id: str
    display_name: str
    configure_spec: ReadConfigureSpec
    configure: ConfigureLocalBackend
    status: ReadLocalBackendStatus
    refresh: RefreshLocalBackend
    doctor: DoctorLocalBackend | None = None
    reset: ResetLocalBackend | None = None
    load_model: ManageModel | None = None
    unload_model: ManageModel | None = None
    download_model: DownloadModel | None = None
    search_models: SearchModels | None = None
    recommended: bool = False

    def __post_init__(self) -> None:
        """Validate backend identifiers and required/optional callbacks.

        验证后端标识符以及必需和可选回调。
        """
        _identifier(self.id, "Local backend id")
        _identifier(self.provider_id, "Local backend provider id")
        _non_empty(self.display_name, "Local backend display name")
        if not callable(self.configure) or not callable(self.status) or not callable(self.refresh):
            raise LocalBackendError("Local backend configure, status, and refresh must be callable")
        if not callable(self.configure_spec) and not isinstance(
            self.configure_spec, LocalConfigureSpec
        ):
            raise LocalBackendError("Local backend configure_spec is unsupported")
        if not isinstance(self.recommended, bool):
            raise LocalBackendError("Local backend recommended flag must be boolean")
        for callback in (
            self.doctor,
            self.reset,
            self.load_model,
            self.unload_model,
            self.download_model,
            self.search_models,
        ):
            if callback is not None and not callable(callback):
                raise LocalBackendError("Local backend capability must be callable")

    def read_configure_spec(self) -> LocalConfigureSpec:
        """Resolve and validate the backend's configuration form schema.

        解析并验证后端的配置表单模式。
        """
        value = self.configure_spec() if callable(self.configure_spec) else self.configure_spec
        if not isinstance(value, LocalConfigureSpec):
            raise LocalBackendError("Local backend configure_spec returned an unsupported value")
        return value


@dataclass(eq=False, slots=True)
class _Operation:
    """One supervised backend operation with progress and cancellation state.

    一个带进度和取消状态的受监管后端操作。
    """
    signal: SimpleCancellationToken
    task: asyncio.Task[LocalOperationResult]
    progress: list[LocalProgress]
    listeners: list[ProgressCallback]
    latest_progress: LocalProgress | None = None
    latest_fractional_progress: LocalProgress | None = None
    cancellation_requested: bool = False


class LocalBackendRegistry:
    """Generation-local, source-bound local backend registry.

    代际本地且绑定来源的本地后端注册表。
    """

    def __init__(
        self,
        provider_registry: DynamicProviderRegistry,
        *,
        generation_id: str | None = None,
        recommended_backend_id: str | None = None,
    ) -> None:
        """Initialize a backend generation paired with a provider registry.

        初始化与提供商注册表配对的后端代际。
        """
        self._providers = provider_registry
        self._generation_id = generation_id or provider_registry.generation_id
        if self._generation_id != provider_registry.generation_id:
            raise LocalBackendError(
                "Local backend registry and provider registry must share a generation"
            )
        self._recommended_backend_id = recommended_backend_id
        self._layers: dict[str, list[tuple[LocalBackendLayerToken, LocalBackend, int]]] = {}
        self._sequence = 0
        self._operations: dict[tuple[LocalBackendLayerToken, LocalAction], _Operation] = {}
        self._retired = False

    @property
    def generation_id(self) -> str:
        """Return this backend registry's generation identity.

        返回此后端注册表的代际标识。
        """
        return self._generation_id

    def register(self, source_id: str, backend: LocalBackend) -> LocalBackendLayerToken:
        """Register a backend only against its exact source provider layer.

        仅针对精确的同来源提供商层注册后端。
        """
        self._assert_active()
        source = _source_id(source_id)
        provider_layer = self._providers.layer_token(backend.provider_id, source)
        if provider_layer is None:
            raise LocalBackendError(
                f"Local backend {backend.id} must be paired with provider {backend.provider_id} "
                "registered by the same source"
            )
        self._sequence += 1
        token = LocalBackendLayerToken(
            backend_id=backend.id,
            source_id=source,
            generation_id=self._generation_id,
            layer_id=f"{self._generation_id}:{self._sequence}",
            provider_layer=provider_layer,
        )
        current = self._layers.get(backend.id, [])
        replaced = [item for item in current if item[0].source_id == source]
        self._layers[backend.id] = [item for item in current if item[0].source_id != source] + [
            (token, backend, self._sequence)
        ]
        for old_token, _, _ in replaced:
            self._cancel_token(old_token)
        return token

    def unregister(self, backend_id: str, source_id: str) -> bool:
        """Remove one exact source layer and preserve preceding layers.

        移除一个精确来源层并保留先前层。
        """
        source = _source_id(source_id)
        current = self._layers.get(backend_id, [])
        removed = [item for item in current if item[0].source_id == source]
        if not removed:
            return False
        remaining = [item for item in current if item[0].source_id != source]
        if remaining:
            self._layers[backend_id] = remaining
        else:
            self._layers.pop(backend_id, None)
        for token, _, _ in removed:
            self._cancel_token(token)
        return True

    def unregister_source(self, source_id: str) -> None:
        """Remove every backend layer owned by one source.

        移除一个来源拥有的所有后端层。
        """
        source = _source_id(source_id)
        for backend_id in tuple(self._layers):
            self.unregister(backend_id, source)

    def layers(self, backend_id: str) -> tuple[LocalBackendLayerToken, ...]:
        """Return active layer tokens for one backend in precedence order.

        按优先级顺序返回一个后端的活动层令牌。
        """
        return tuple(token for token, _, _ in self._layers.get(backend_id, ()))

    def effective(self, backend_id: str) -> LocalBackendView | None:
        """Return the effective backend and whether its provider layer is usable.

        返回有效后端及其提供商层是否可用。
        """
        current = self._layers.get(backend_id)
        if not current:
            return None
        token, backend, _ = current[-1]
        provider = self._providers.layer_token(backend.provider_id, token.source_id)
        effective_provider = self._providers.effective(backend.provider_id)
        provider_effective = (
            provider == token.provider_layer
            and effective_provider is not None
            and effective_provider.layer_token == provider
        )
        return LocalBackendView(
            backend=backend,
            token=token,
            recommended=backend.recommended or backend.id == self._recommended_backend_id,
            provider_layer_effective=provider_effective,
        )

    def all_views(self, backend_id: str) -> tuple[LocalBackendView, ...]:
        """Return every source layer, including shadowed backend layers.

        返回所有来源层，包括被遮蔽的后端层。
        """
        views: list[LocalBackendView] = []
        for token, backend, _ in self._layers.get(backend_id, ()):
            provider = self._providers.layer_token(backend.provider_id, token.source_id)
            effective_provider = self._providers.effective(backend.provider_id)
            views.append(
                LocalBackendView(
                    backend=backend,
                    token=token,
                    recommended=backend.recommended or backend.id == self._recommended_backend_id,
                    provider_layer_effective=(
                        provider == token.provider_layer
                        and effective_provider is not None
                        and effective_provider.layer_token == provider
                    ),
                )
            )
        return tuple(views)

    def effective_backends(self) -> tuple[LocalBackendView, ...]:
        """Return one view per backend in deterministic registration order.

        按确定性注册顺序为每个后端返回一个视图。
        """
        views = [self.effective(backend_id) for backend_id in self._layers]
        return tuple(view for view in views if view is not None)

    def operation_running(self, backend_id: str, action: LocalAction) -> bool:
        """Return whether one effective backend action is still in flight.

        返回一个有效后端操作是否仍在进行。
        """
        return any(
            token.backend_id == backend_id and operation == action and not state.task.done()
            for (token, operation), state in self._operations.items()
        )

    def observe_progress(
        self,
        backend_id: str,
        action: LocalAction,
        callback: ProgressCallback,
    ) -> Callable[[], None] | None:
        """Observe one running action and replay its best current progress.

        观察一个运行中的操作并重放其最佳当前进度。
        """
        state = next(
            (
                state
                for (token, operation), state in self._operations.items()
                if token.backend_id == backend_id and operation == action and not state.task.done()
            ),
            None,
        )
        if state is None:
            return None
        state.listeners.append(callback)
        current = state.latest_fractional_progress or state.latest_progress
        if current is not None:
            with suppress(Exception):
                callback(current)

        def unsubscribe() -> None:
            """Detach one progress listener from the running operation.

            从运行中的操作分离一个进度监听器。
            """
            with suppress(ValueError):
                state.listeners.remove(callback)

        return unsubscribe

    def cancel(self, backend_id: str, action: LocalAction | None = None) -> bool:
        """Request cancellation for matching backend operations.

        请求取消匹配的后端操作。
        """
        cancelled = False
        for (token, operation), state in tuple(self._operations.items()):
            if token.backend_id == backend_id and (action is None or operation == action):
                cancelled = _request_operation_cancellation(state) or cancelled
        return cancelled

    async def configure(
        self,
        backend_id: str,
        values: Mapping[str, str],
        *,
        progress: ProgressCallback | None = None,
    ) -> LocalOperationResult:
        """Validate values and run one transactional backend configuration.

        校验值并运行一次事务式后端配置。
        """
        view = self._require_view(backend_id)
        spec = view.backend.read_configure_spec()
        config_values, errors = _validate_values(spec, values)
        if errors:
            return LocalOperationResult(field_errors=errors)
        secret_values = tuple(
            config_values[key] for key in config_values.secret_keys if config_values[key]
        )
        return await self._run(
            view,
            "configure",
            lambda context: _invoke(view.backend.configure, config_values, context),
            progress=progress,
            secrets=secret_values,
        )

    async def status(
        self, backend_id: str, *, progress: ProgressCallback | None = None
    ) -> LocalOperationResult:
        """Read current backend status through the supervised operation path.

        通过受监管操作路径读取当前后端状态。
        """
        view = self._require_view(backend_id)
        return await self._run(
            view,
            "refresh",
            lambda context: _invoke(view.backend.status, context),
            progress=progress,
        )

    async def refresh(
        self, backend_id: str, *, progress: ProgressCallback | None = None
    ) -> LocalOperationResult:
        """Refresh backend discovery through the supervised operation path.

        通过受监管操作路径刷新后端发现。
        """
        view = self._require_view(backend_id)
        return await self._run(
            view,
            "refresh",
            lambda context: _invoke(view.backend.refresh, context),
            progress=progress,
        )

    async def doctor(
        self, backend_id: str, *, progress: ProgressCallback | None = None
    ) -> LocalOperationResult:
        """Run optional backend diagnostics or return unavailable guidance.

        运行可选后端诊断，或返回不可用指引。
        """
        view = self._require_view(backend_id)
        doctor = view.backend.doctor
        if doctor is None:
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic("This action is unavailable.", "warning"),)
            )
        return await self._run(
            view,
            "doctor",
            lambda context: _invoke(doctor, context),
            progress=progress,
        )

    async def reset(
        self, backend_id: str, *, progress: ProgressCallback | None = None
    ) -> LocalOperationResult:
        """Reset an effective backend when the optional capability exists.

        可选能力存在时重置有效后端。
        """
        view = self._require_view(backend_id)
        reset = view.backend.reset
        if reset is None:
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic("This action is unavailable.", "warning"),)
            )
        if not view.use_available:
            return LocalOperationResult(
                diagnostics=(
                    LocalDiagnostic(
                        "Reset is unavailable while this backend is shadowed by another source.",
                        "warning",
                    ),
                )
            )
        return await self._run(
            view,
            "reset",
            lambda context: _invoke(reset, context),
            progress=progress,
        )

    async def manage_model(
        self,
        backend_id: str,
        action: Literal["load_model", "unload_model", "download_model"],
        model_id: str,
        *,
        progress: ProgressCallback | None = None,
        confirmation: str | None = None,
    ) -> LocalOperationResult:
        """Run a load, unload, or download action for one exact model reference.

        为一个精确模型引用运行加载、卸载或下载操作。
        """
        view = self._require_view(backend_id)
        callback = getattr(view.backend, action)
        if callback is None:
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic("This action is unavailable.", "warning"),)
            )
        if not view.use_available:
            return LocalOperationResult(
                diagnostics=(
                    LocalDiagnostic(
                        "This action is unavailable while this backend is shadowed by "
                        "another source.",
                        "warning",
                    ),
                )
            )
        return await self._run(
            view,
            action,
            lambda context: _invoke(callback, model_id, context),
            progress=progress,
            confirmation=confirmation,
        )

    async def search_models(
        self,
        backend_id: str,
        query: str,
        *,
        progress: ProgressCallback | None = None,
    ) -> LocalOperationResult:
        """Search models through the effective backend with a normalized query.

        使用规范化查询通过有效后端搜索模型。
        """
        view = self._require_view(backend_id)
        callback = view.backend.search_models
        if callback is None:
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic("This action is unavailable.", "warning"),)
            )
        if not view.use_available:
            return LocalOperationResult(
                diagnostics=(
                    LocalDiagnostic(
                        "This action is unavailable while this backend is shadowed by "
                        "another source.",
                        "warning",
                    ),
                )
            )
        if not query.strip():
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic("Enter a search query.", "warning"),)
            )
        return await self._run(
            view,
            "search_models",
            lambda context: _invoke(callback, query.strip(), context),
            progress=progress,
        )

    def retire(self) -> None:
        """Invalidate this generation and cancel all owned backend operations.

        使此代际失效并取消所有拥有的后端操作。
        """
        if self._retired:
            return
        self._retired = True
        self._layers.clear()
        for token, _ in tuple(self._operations):
            self._cancel_token(token)

    async def aclose(self) -> None:
        """Retire the generation and boundedly drain backend operations.

        退役此代际并在有界时间内排空后端操作。

        A backend is extension code and can ignore task cancellation while it
        unwinds a network client or subprocess.  Do not let final session
        cleanup hang forever: request cancellation once, wait for the fixed
        containment window, and keep a still-running task strongly reachable
        until it actually finishes.  Its context is stale, so late results are
        discarded by :meth:`_run`.

        后端属于扩展代码，可能在展开网络客户端或子进程时忽略任务取消。为避免最终会话
        清理永久挂起，只请求取消一次、等待固定容纳窗口，并让仍运行的任务保持强可达直到
        实际完成。其上下文已过期，因此 :meth:`_run` 会丢弃迟到结果。
        """
        self.retire()
        operations = tuple(self._operations.values())
        if operations:
            await asyncio.gather(
                *(_cancel_and_drain_operation(operation) for operation in operations),
                return_exceptions=True,
            )

    async def _run(
        self,
        view: LocalBackendView,
        action: LocalAction,
        callback: Callable[[LocalOperationContext], object],
        *,
        progress: ProgressCallback | None,
        secrets: Sequence[str] = (),
        confirmation: str | None = None,
    ) -> LocalOperationResult:
        """Create, supervise, and publish one source-bound backend operation.

        创建、监管并发布一个绑定来源的后端操作。
        """
        token = view.token
        key = (token, action)
        prior = self._operations.get(key)
        if prior is not None:
            self._cancel_token(token, action)
        signal = SimpleCancellationToken()
        progress_events: list[LocalProgress] = []
        listeners = [progress] if progress is not None else []
        operation: _Operation
        context = LocalOperationContext(
            signal=signal,
            action=action,
            generation_id=self._generation_id,
            backend_id=token.backend_id,
            source_id=token.source_id,
            confirmation=confirmation,
            _is_current=lambda: self._token_is_current(token),
            _progress=lambda item: _record_progress(_redact_progress(item, secrets), operation),
        )
        task = asyncio.create_task(
            self._invoke_operation(context, callback, progress_events, secrets),
            name=f"tau-local-backend:{token.source_id}:{token.backend_id}:{action}",
        )
        operation = _Operation(
            signal=signal,
            task=task,
            progress=progress_events,
            listeners=listeners,
        )
        self._operations[key] = operation
        try:
            result = await task
        finally:
            if self._operations.get(key) is operation:
                self._operations.pop(key, None)
        if not self._token_is_current(token):
            return LocalOperationResult(
                progress=tuple(progress_events),
                cancelled=result.cancelled,
                stale=True,
            )
        return result

    async def _invoke_operation(
        self,
        context: LocalOperationContext,
        callback: Callable[[LocalOperationContext], object],
        progress_events: list[LocalProgress],
        secrets: Sequence[str],
    ) -> LocalOperationResult:
        """Invoke a backend callback and normalize every supported result shape.

        调用后端回调并规范化所有受支持的结果形态。
        """
        try:
            value = callback(context)
            value = await value if inspect.isawaitable(value) else value
        except asyncio.CancelledError:
            context.signal.cancel()
            return LocalOperationResult(progress=tuple(progress_events), cancelled=True)
        except Exception:
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic("The backend operation failed. Try again.", "error"),),
                progress=tuple(progress_events),
            )
        if context.cancelled:
            return LocalOperationResult(progress=tuple(progress_events), cancelled=True)
        if isinstance(value, LocalConfigureResult):
            return _safe_operation_result(
                LocalOperationResult(
                    backend_status=value.backend_status,
                    message=value.message,
                    diagnostics=value.diagnostics,
                    progress=tuple(progress_events),
                    committed=value.committed,
                    field_errors=value.field_errors,
                    credential_orphaned=value.credential_orphaned,
                ),
                secrets,
            )
        if isinstance(value, LocalBackendStatus):
            return _safe_operation_result(
                LocalOperationResult(
                    backend_status=value,
                    progress=tuple(progress_events),
                ),
                secrets,
            )
        if isinstance(value, LocalOperationResult):
            return _safe_operation_result(
                LocalOperationResult(
                    backend_status=value.backend_status,
                    message=value.message,
                    diagnostics=value.diagnostics,
                    progress=tuple((*progress_events, *value.progress)),
                    cancelled=value.cancelled,
                    stale=value.stale,
                    committed=value.committed,
                    field_errors=value.field_errors,
                    credential_orphaned=value.credential_orphaned,
                    confirmation=value.confirmation,
                    search_results=value.search_results,
                ),
                secrets,
            )
        return LocalOperationResult(
            diagnostics=(LocalDiagnostic("The backend returned an invalid result.", "error"),),
            progress=tuple(progress_events),
        )

    def _require_view(self, backend_id: str) -> LocalBackendView:
        """Return an effective backend view or raise a bounded operation error.

        返回有效后端视图，或抛出有界操作错误。
        """
        if self._retired:
            raise LocalOperationError("Local backend registry generation is retired")
        view = self.effective(backend_id)
        if view is None:
            raise LocalOperationError(f"Unknown local backend: {backend_id}")
        return view

    def _token_is_current(self, token: LocalBackendLayerToken) -> bool:
        """Return whether a backend layer token still belongs to this generation.

        返回后端层令牌是否仍属于此代际。
        """
        if self._retired or token.generation_id != self._generation_id:
            return False
        return any(item[0] == token for item in self._layers.get(token.backend_id, ()))

    def _cancel_token(
        self,
        token: LocalBackendLayerToken,
        action: LocalAction | None = None,
    ) -> bool:
        """Cancel operations owned by one exact backend layer token.

        取消一个精确后端层令牌拥有的操作。
        """
        cancelled = False
        for (operation_token, operation_action), operation in tuple(self._operations.items()):
            if operation_token == token and (action is None or operation_action == action):
                cancelled = _request_operation_cancellation(operation) or cancelled
        return cancelled

    def _assert_active(self) -> None:
        """Raise when this backend registry generation has retired.

        此后端注册表代际已退役时抛出异常。
        """
        if self._retired:
            raise LocalOperationError("Local backend registry generation is retired")


def _request_operation_cancellation(operation: _Operation) -> bool:
    """Signal and cancel one operation at most once.

    最多向一个操作发送一次信号和任务取消。
    """
    operation.signal.cancel()
    if operation.cancellation_requested:
        return False
    operation.cancellation_requested = True
    if not operation.task.done():
        operation.task.cancel()
    return True


async def _cancel_and_drain_operation(operation: _Operation) -> bool:
    """Wait through one bounded cancellation window without re-cancelling.

    在一个有界取消窗口内等待，而不重复取消。
    """
    if operation.task.done():
        return True
    _request_operation_cancellation(operation)
    _SUPERVISED_LOCAL_OPERATION_TASKS.add(operation.task)
    operation.task.add_done_callback(_release_supervised_operation)
    try:
        async with asyncio.timeout(LOCAL_OPERATION_CANCELLATION_TIMEOUT_SECONDS):
            await asyncio.shield(operation.task)
    except TimeoutError:
        return operation.task.done()
    except asyncio.CancelledError:
        # The cleanup owner itself was cancelled.  The task remains supervised
        # and receives no second cancellation while its backend unwinds.
        # 清理所有者自身已被取消。任务仍受监管，并且在后端展开期间不会收到第二次取消。
        if operation.task.done():
            return True
        raise
    except Exception:
        return True
    return True


def _release_supervised_operation(task: asyncio.Task[object]) -> None:
    """Release a completed operation from process-owned supervision.

    从进程拥有的监管中释放已完成操作。
    """
    _SUPERVISED_LOCAL_OPERATION_TASKS.discard(task)
    with suppress(asyncio.CancelledError):
        task.exception()


def _validate_values(
    spec: LocalConfigureSpec,
    values: Mapping[str, str],
) -> tuple[LocalConfigValues, Mapping[str, str]]:
    """Validate submitted configuration values against a backend form schema.

    根据后端表单模式校验提交的配置值。
    """
    submitted = dict(values)
    fields = {field.key: field for field in spec.fields}
    errors: dict[str, str] = {}
    for key in submitted:
        if key not in fields:
            errors[key] = "Unknown configuration field."
    for key, config_field in fields.items():
        value = submitted.get(key, "")
        if config_field.required and not value.strip():
            errors[key] = "This field is required."
        if config_field.kind == "choice" and value and value not in config_field.choices:
            errors[key] = "Choose one of the listed values."
    secret_keys = frozenset(
        field.key for field in spec.fields if field.kind == "secret" and field.key in submitted
    )
    return LocalConfigValues(submitted, secret_keys=secret_keys), MappingProxyType(errors)


def _record_progress(item: LocalProgress, operation: _Operation) -> None:
    """Record progress, preserve fractional detail, and notify listeners safely.

    记录进度、保留比例详情并安全通知监听器。
    """
    operation.progress.append(item)
    operation.latest_progress = item
    if item.fraction is not None:
        operation.latest_fractional_progress = item
    for callback in tuple(operation.listeners):
        # A closing or replaced host must not turn backend work into a failed
        # transaction merely because progress rendering disappeared.
        # 关闭或被替换的宿主不应仅因进度渲染消失就使后端工作变成失败事务。
        with suppress(Exception):
            callback(item)


async def _invoke(callback: Callable[..., object], *args: object) -> object:
    """Invoke documented callbacks, accepting a no-context test double too.

    调用有文档说明的回调，同时接受不带上下文的测试替身。
    """
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        value = callback(*args)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        required = [
            parameter for parameter in positional if parameter.default is inspect.Parameter.empty
        ]
        if len(required) == 0:
            value = callback()
        elif len(positional) < len(args):
            value = callback(*args[: len(positional)])
        else:
            value = callback(*args)
    return await value if inspect.isawaitable(value) else value


def _redact(value: str | None, secrets: Sequence[str]) -> str | None:
    """Replace every known secret in optional backend-authored text.

    替换可选后端文本中的所有已知敏感值。
    """
    if value is None:
        return None
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[redacted]")
    return result


def _redact_progress(item: LocalProgress, secrets: Sequence[str]) -> LocalProgress:
    """Return a progress event with secret-bearing text redacted.

    返回敏感文本已脱敏的进度事件。
    """
    return LocalProgress(
        _redact(item.message, secrets) or "[redacted]",
        item.fraction,
        item.done,
    )


def _safe_operation_result(
    result: LocalOperationResult,
    secrets: Sequence[str],
) -> LocalOperationResult:
    """Keep backend-authored values out of host messages and diagnostics.

    对宿主消息和诊断中的后端文本进行脱敏。
    """
    status = result.backend_status
    if status is not None:
        status = LocalBackendStatus(
            state=status.state,
            endpoint_display=_redact(status.endpoint_display, secrets),
            authentication_source=status.authentication_source,
            models=tuple(
                LocalModel(
                    id=_redact(model.id, secrets) or "[redacted]",
                    display_name=_redact(model.display_name, secrets),
                    state=_redact(model.state, secrets),
                )
                for model in status.models
            ),
            selected_model=_redact(status.selected_model, secrets),
            actions=status.actions,
            diagnostics=tuple(
                LocalDiagnostic(
                    _redact(item.message, secrets) or "[redacted]",
                    item.severity,
                    item.stage,
                )
                for item in status.diagnostics
            ),
            cached=status.cached,
            stale=status.stale,
        )
    confirmation = result.confirmation
    if confirmation is not None:
        confirmation = LocalConfirmationRequest(
            message=_redact(confirmation.message, secrets) or "[redacted]",
            choices=tuple(
                LocalConfirmationChoice(
                    choice.value,
                    _redact(choice.label, secrets) or "[redacted]",
                    choice.recommended,
                )
                for choice in confirmation.choices
            ),
        )
    return LocalOperationResult(
        backend_status=status,
        message=_redact(result.message, secrets),
        diagnostics=tuple(
            LocalDiagnostic(
                _redact(item.message, secrets) or "[redacted]",
                item.severity,
                item.stage,
            )
            for item in result.diagnostics
        ),
        progress=tuple(
            LocalProgress(
                _redact(item.message, secrets) or "[redacted]",
                item.fraction,
                item.done,
            )
            for item in result.progress
        ),
        cancelled=result.cancelled,
        stale=result.stale,
        committed=result.committed and not result.field_errors,
        field_errors={
            key: _redact(message, secrets) or "[redacted]"
            for key, message in result.field_errors.items()
        },
        credential_orphaned=result.credential_orphaned,
        confirmation=confirmation,
        search_results=tuple(
            LocalSearchResult(
                id=_redact(item.id, secrets) or "[redacted]",
                label=_redact(item.label, secrets) or "[redacted]",
                restricted=item.restricted,
                options=tuple(
                    LocalArtifactOption(
                        id=_redact(option.id, secrets) or "[redacted]",
                        label=_redact(option.label, secrets) or "[redacted]",
                        size_bytes=option.size_bytes,
                        recommended=option.recommended,
                    )
                    for option in item.options
                ),
                diagnostic=_redact(item.diagnostic, secrets),
            )
            for item in result.search_results
        ),
    )


def _source_id(value: str) -> str:
    """Validate and return one exact backend source identifier.

    校验并返回一个精确后端来源标识符。
    """
    _non_empty(value, "Local backend source id")
    if value != value.strip():
        raise LocalBackendError("Local backend source id must not have surrounding whitespace")
    return value


def _non_empty(value: str, label: str) -> None:
    """Require a value to be a non-empty string.

    要求值为非空字符串。
    """
    if not isinstance(value, str) or not value.strip():
        raise LocalBackendError(f"{label} must be a non-empty string")


def _identifier(value: str, label: str) -> None:
    """Require an identifier to be non-empty without surrounding whitespace.

    要求标识符非空且不含首尾空白。
    """
    _non_empty(value, label)
    if value != value.strip():
        raise LocalBackendError(f"{label} must not have surrounding whitespace")


__all__ = [
    "LOCAL_OPERATION_CANCELLATION_TIMEOUT_SECONDS",
    "ConfigureLocalBackend",
    "DoctorLocalBackend",
    "DownloadModel",
    "LocalAction",
    "LocalArtifactOption",
    "LocalBackend",
    "LocalBackendError",
    "LocalBackendLayerToken",
    "LocalBackendRegistry",
    "LocalBackendStatus",
    "LocalBackendView",
    "LocalConfigField",
    "LocalConfigValues",
    "LocalConfigureResult",
    "LocalConfigureSpec",
    "LocalConfirmationChoice",
    "LocalConfirmationRequest",
    "LocalConnectionState",
    "LocalDiagnostic",
    "LocalFieldKind",
    "LocalModel",
    "LocalOperationContext",
    "LocalOperationError",
    "LocalOperationResult",
    "LocalProgress",
    "LocalSearchResult",
    "ManageModel",
    "ReadConfigureSpec",
    "ReadLocalBackendStatus",
    "RefreshLocalBackend",
    "ResetLocalBackend",
    "SearchModels",
]
