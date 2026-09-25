"""llama.cpp protocol adapter used by the built-in extension.

内置扩展使用的 llama.cpp 协议适配器。
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx

from tau_agent.harness import SimpleCancellationToken
from tau_agent.messages import TextContent, UserMessage
from tau_agent.provider import CancellationToken
from tau_agent.provider_events import (
    AssistantErrorEvent,
    AssistantStartEvent,
    ToolCallEndEvent,
)
from tau_agent.tools import AgentTool, AgentToolResult, ToolCancellationToken
from tau_agent.types import JSONValue
from tau_ai.env import DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS, OpenAICompatibleConfig
from tau_ai.http import create_async_client
from tau_ai.openai_compatible import OpenAICompatibleProvider
from tau_coding.credentials import CredentialStore, FileCredentialStore, credentials_path
from tau_coding.extensions.providers import (
    DynamicProvider,
    OpenAICompatibleTransport,
    ProviderAuthContext,
    ProviderAuthError,
    ProviderModel,
    ProviderModelSnapshot,
    ProviderRefreshContext,
    ResolvedProviderAuth,
)
from tau_coding.local_backends import (
    LocalAction,
    LocalArtifactOption,
    LocalBackend,
    LocalBackendStatus,
    LocalConfigField,
    LocalConfigureResult,
    LocalConfigureSpec,
    LocalConfirmationChoice,
    LocalConfirmationRequest,
    LocalDiagnostic,
    LocalModel,
    LocalOperationContext,
    LocalOperationResult,
    LocalProgress,
    LocalSearchResult,
)
from tau_coding.paths import TauPaths

from .huggingface import (
    HuggingFaceSearchError,
    discover_hf_token,
    search_gguf_repositories,
    validate_repository_reference,
)
from .router import (
    LlamaCppRouterError,
    RouterCapability,
    RouterModel,
    detect_router,
    list_router_models,
    mutate_router_model,
    watch_router_download_progress,
)
from .state import (
    LLAMA_CPP_CREDENTIAL_PREFIX,
    LlamaCppIntegrationState,
    LlamaCppStateError,
    LlamaCppStateStore,
    LlamaCppStoredModel,
)

LLAMA_CPP_PROVIDER_ID = "llama.cpp"
LLAMA_CPP_DISPLAY_NAME = "llama.cpp"
LLAMA_CPP_BACKEND_ID = "llama.cpp"
LLAMA_CPP_DEFAULT_ENDPOINT = "http://127.0.0.1:8080"
LLAMA_CPP_ENDPOINT_ENV = "LLAMA_BASE_URL"
LLAMA_CPP_API_KEY_ENV = "LLAMA_API_KEY"
LLAMA_CPP_SERVER_GUIDE_URL = (
    "https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#using-multiple-models"
)
DEFAULT_LLAMA_CPP_TIMEOUT_SECONDS = 5.0
LLAMA_CPP_ROUTER_POLL_SECONDS = 0.1
LLAMA_CPP_ROUTER_RECONCILE_TIMEOUT_SECONDS = 30.0
LLAMA_CPP_ROUTER_DOWNLOAD_TIMEOUT_SECONDS = 4 * 60 * 60.0


class LlamaCppError(RuntimeError):
    """Raised for a safe, user-actionable llama.cpp integration failure.

    出现安全且用户可操作的 llama.cpp 集成故障时抛出的异常。
    """


class LlamaCppEndpointError(ValueError):
    """Raised when an endpoint is not a safe HTTP base URL.

    端点不是安全 HTTP 基础 URL 时抛出的异常。
    """


@dataclass(frozen=True, slots=True)
class LlamaCppEndpoint:
    """The server root and OpenAI-compatible inference base for one endpoint.

    一个端点的服务器根地址与 OpenAI 兼容推理基础地址。
    """

    server_root: str
    inference_base: str


@dataclass(frozen=True, slots=True)
class LlamaCppDiscovery:
    """Defensive result from standard discovery and optional router state.

    标准发现及可选路由器状态产生的防御性结果。
    """

    endpoint: LlamaCppEndpoint
    models: tuple[ProviderModel, ...]
    health: str = "ok"
    router_capability: RouterCapability = RouterCapability("standard")
    router_models: tuple[RouterModel, ...] = ()


@dataclass(frozen=True, slots=True)
class _LlamaCppAuth:
    """Generation-referenced optional auth; secrets never enter the provider.

    由代际引用的可选认证；敏感信息绝不进入提供商。
    """

    credential_ref: str | None = None
    env_var: str = LLAMA_CPP_API_KEY_ENV

    async def resolve(self, context: ProviderAuthContext) -> ResolvedProviderAuth:
        """Resolve llama.cpp authentication from stored credentials and environment.

        从已存储凭据和环境解析 llama.cpp 认证信息。
        """
        if self.credential_ref:
            stored = context.credentials.get(self.credential_ref)
            if stored:
                return ResolvedProviderAuth(
                    api_key=stored,
                    source="stored credential",
                    omit_authorization_header=False,
                )
        environment_value = context.environment.get(self.env_var)
        if environment_value:
            return ResolvedProviderAuth(
                api_key=environment_value,
                source=f"environment variable {self.env_var}",
                omit_authorization_header=False,
            )
        return ResolvedProviderAuth()


class _HttpFailure(LlamaCppError):
    def __init__(self, status_code: int, message: str) -> None:
        """Capture an HTTP status and safe user-facing failure message.

        捕获 HTTP 状态码和安全的面向用户故障消息。
        """
        super().__init__(message)
        self.status_code = status_code


class LlamaCppService:
    """Own endpoint state, discovery, diagnostics, and provider construction.

    管理端点状态、发现、诊断和提供商构建。
    """

    def __init__(
        self,
        *,
        state_store: LlamaCppStateStore | None = None,
        credential_store: CredentialStore | None = None,
        environment: Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_LLAMA_CPP_TIMEOUT_SECONDS,
        register_provider: Callable[[DynamicProvider], None] | None = None,
        update_provider: Callable[[DynamicProvider], bool] | None = None,
        register_backend: Callable[[LocalBackend], None] | None = None,
    ) -> None:
        """Initialize integration dependencies and recover safe persisted state.

        初始化集成依赖并恢复安全的持久化状态。
        """
        self.state_store = state_store or LlamaCppStateStore()
        self.credential_store: CredentialStore = (
            credential_store
            if credential_store is not None
            else FileCredentialStore(credentials_path(TauPaths()))
        )
        self.environment = dict(environment or {})
        if environment is None:
            import os

            self.environment = dict(os.environ)
        self.client = client
        self.timeout_seconds = timeout_seconds
        self._register_provider = register_provider
        self._update_provider = update_provider
        self._register_backend = register_backend
        self._last_discovery: LlamaCppDiscovery | None = None
        self._router_capability = RouterCapability("standard")
        self._router_models: tuple[RouterModel, ...] = ()
        self._download_sizes: dict[str, int] = {}
        self._last_error: LlamaCppError | None = None
        self._state_error: str | None = None
        self._orphaned_credentials: tuple[str, ...] = ()
        self._stale_selected_model: str | None = None
        try:
            active = self.state_store.active()
        except LlamaCppStateError as exc:
            active = None
            self._state_error = str(exc)
        self._active_state = active
        if active is not None and active.selected_model is not None:
            cached_ids = {model.id for model in active.models}
            if active.selected_model not in cached_ids:
                self._stale_selected_model = active.selected_model
        self._endpoint_error: str | None = None
        endpoint = self._resolve_effective_endpoint(active)
        self._endpoint = endpoint or normalize_llama_cpp_endpoint(LLAMA_CPP_DEFAULT_ENDPOINT)
        self._cleanup_orphans()

    @property
    def configured(self) -> bool:
        """Return whether a valid llama.cpp endpoint is configured.

        返回是否配置了有效的 llama.cpp 端点。
        """
        return self._active_state is not None or bool(self.environment.get(LLAMA_CPP_ENDPOINT_ENV))

    @property
    def endpoint(self) -> LlamaCppEndpoint:
        """Return the effective normalized llama.cpp endpoint.

        返回有效且规范化的 llama.cpp 端点。
        """
        return self._endpoint

    @property
    def endpoint_error(self) -> str | None:
        """Return a safe endpoint configuration error, if any.

        返回安全的端点配置错误（若有）。
        """
        return self._endpoint_error

    @property
    def last_discovery(self) -> LlamaCppDiscovery | None:
        """Return the latest successful discovery snapshot.

        返回最近一次成功的发现快照。
        """
        return self._last_discovery

    @property
    def state_error(self) -> str | None:
        """Return a safe persisted-state error, if any.

        返回安全的持久化状态错误（若有）。
        """
        return self._state_error

    @property
    def orphaned_credentials(self) -> tuple[str, ...]:
        """Return credential references that are no longer reachable.

        返回不再可达的凭据引用。
        """
        return self._orphaned_credentials

    def provider(self) -> DynamicProvider:
        """Build the dynamic provider definition exposed to Tau.

        构建向 Tau 暴露的动态提供商定义。
        """
        active = self._active_state
        if active is not None and active.endpoint == self.endpoint.server_root:
            models = tuple(_provider_model(model) for model in active.models)
        elif self._last_discovery is not None and self._last_discovery.endpoint == self.endpoint:
            # Explicit /local probing may use the offered default endpoint
            # without persisting it. Keep that live discovery available to the
            # current process so /model and model switching work immediately.
            # 显式 /local 探测可使用提供的默认端点而不持久化它。将实时发现保留在当前
            # 进程中，使 /model 和模型切换立即可用。
            models = self._last_discovery.models
        else:
            models = ()
        default = (
            None if self._stale_selected_model is not None else _selected_model(active, models)
        )
        return DynamicProvider(
            id=LLAMA_CPP_PROVIDER_ID,
            display_name=LLAMA_CPP_DISPLAY_NAME,
            models=models,
            default_model=default,
            transport=OpenAICompatibleTransport(
                base_url=self.endpoint.inference_base,
                auth=_LlamaCppAuth(active.credential_ref if active else None),
                # Backend discovery should fail quickly, but first-token latency
                # for large local prompts routinely exceeds that 5s probe bound.
                # 后端发现应快速失败，但大型本地提示词的首令牌延迟通常会超过 5 秒探测上限。
                timeout_seconds=DEFAULT_OPENAI_COMPATIBLE_TIMEOUT_SECONDS,
                max_retries=2,
                max_retry_delay_seconds=1.0,
                client=self.client,
            ),
            refresh_models=self.refresh_provider_models,
            stable_scoped_references=True,
        )

    async def refresh_provider_models(
        self,
        context: ProviderRefreshContext,
    ) -> ProviderModelSnapshot:
        """Discovery callback used by the generic provider registry.

        通用提供商注册表使用的发现回调。
        """
        if not context.allow_network:
            return ProviderModelSnapshot(
                models=tuple(context.cached_models),
                default_model=_cached_default(context.cached_models),
            )
        discovery = await self.discover(context.auth, signal=context.signal)
        self._last_discovery = discovery
        self._last_error = None
        self._save_discovery(discovery)
        return ProviderModelSnapshot(
            models=discovery.models,
            default_model=_selected_model(self._active_state, discovery.models),
        )

    async def discover(
        self,
        auth: ResolvedProviderAuth,
        *,
        signal: CancellationToken | None = None,
    ) -> LlamaCppDiscovery:
        """Probe only the already-selected endpoint and parse standard models.

        仅探测已选择的端点并解析标准模型。
        """
        headers = _auth_headers(auth)
        owned_client = self.client is None
        client = self.client or create_async_client(timeout=self.timeout_seconds)
        try:
            health = await self._get(client, self.endpoint.server_root + "/health", headers, signal)
            health_state = _health_state(health)
            if health_state == "loading":
                raise LlamaCppError(
                    "llama.cpp is still loading a model. Wait for it to become ready and retry."
                )
            if health_state == "unavailable":
                raise LlamaCppError("llama.cpp reported that its server is unavailable.")
            capability = await self._detect_router_safely(client, headers)
            self._router_capability = capability
            if capability.compatible:
                router_models = await list_router_models(client, self.endpoint.server_root, headers)
                self._router_models = router_models
                models = _router_provider_models(router_models)
                return LlamaCppDiscovery(
                    self.endpoint,
                    models,
                    health=health_state,
                    router_capability=capability,
                    router_models=router_models,
                )
            self._router_models = ()
            response = await self._get(
                client, self.endpoint.inference_base + "/models", headers, signal
            )
            payload = _json_object(response, "/v1/models")
            models = _parse_models(payload)
            return LlamaCppDiscovery(
                self.endpoint,
                models,
                health=health_state,
                router_capability=capability,
            )
        except asyncio.CancelledError:
            raise
        except LlamaCppError as exc:
            self._last_error = exc
            raise
        except LlamaCppRouterError as exc:
            error = LlamaCppError(str(exc))
            self._last_error = error
            raise error from exc
        except httpx.TimeoutException as exc:
            error = LlamaCppError(
                f"Timed out connecting to llama.cpp at {self.endpoint.server_root}. "
                f"Check the server and retry. Router setup: {LLAMA_CPP_SERVER_GUIDE_URL}"
            )
            self._last_error = error
            raise error from exc
        except httpx.HTTPError as exc:
            error = LlamaCppError(
                f"Could not connect to llama.cpp at {self.endpoint.server_root}. "
                f"Start llama-server and retry. Router setup: {LLAMA_CPP_SERVER_GUIDE_URL}"
            )
            self._last_error = error
            raise error from exc
        finally:
            if owned_client:
                await client.aclose()

    async def configure(
        self,
        values: Mapping[str, str],
        context: LocalOperationContext,
    ) -> LocalConfigureResult:
        """Commit endpoint and optional credential as one safe-state transaction.

        将端点和可选凭据作为一个安全状态事务提交。
        """
        del context
        try:
            endpoint = normalize_llama_cpp_endpoint(values.get("endpoint", ""))
        except LlamaCppEndpointError as exc:
            return LocalConfigureResult(
                diagnostics=(LocalDiagnostic(str(exc), "error", "configuration"),)
            )
        key = values.get("api_key", "").strip() or None
        old_active = self._active_state
        try:
            prior_for_endpoint = self.state_store.get(endpoint.server_root)
        except Exception:
            return LocalConfigureResult(
                message="Could not save llama.cpp settings.",
                diagnostics=(
                    LocalDiagnostic(
                        "The existing llama.cpp settings could not be read.",
                        "error",
                        "configuration",
                    ),
                ),
            )

        credential_ref = (
            f"{LLAMA_CPP_CREDENTIAL_PREFIX}{secrets.token_urlsafe(18)}" if key else None
        )
        if credential_ref is not None:
            assert key is not None
            try:
                self.credential_store.set(credential_ref, key)
            except Exception:
                return LocalConfigureResult(
                    message="Could not save llama.cpp settings.",
                    diagnostics=(
                        LocalDiagnostic(
                            "The API key could not be stored; no settings were changed.",
                            "error",
                            "credentials",
                        ),
                    ),
                )

        state = LlamaCppIntegrationState(
            endpoint=endpoint.server_root,
            selected_model=(prior_for_endpoint.selected_model if prior_for_endpoint else None),
            credential_ref=credential_ref,
            models=(prior_for_endpoint.models if prior_for_endpoint else ()),
            checked_at=(prior_for_endpoint.checked_at if prior_for_endpoint else None),
        )
        try:
            self.state_store.save(
                state,
                replace_endpoint=(
                    old_active.endpoint
                    if old_active is not None and old_active.endpoint != endpoint.server_root
                    else None
                ),
            )
        except Exception:
            orphaned = False
            if credential_ref is not None:
                try:
                    self.credential_store.delete(credential_ref)
                except Exception:
                    orphaned = True
            return LocalConfigureResult(
                message="llama.cpp settings were not saved.",
                diagnostics=(
                    LocalDiagnostic(
                        "The previous llama.cpp configuration is still active.",
                        "error",
                        "configuration",
                    ),
                ),
                credential_orphaned=orphaned,
            )

        self._active_state = state
        self._endpoint = endpoint
        self._endpoint_error = None
        self._last_error = None
        self._stale_selected_model = None
        cleanup_error = self._delete_unreferenced_credentials(
            old_active.credential_ref if old_active else None
        )
        self._publish_provider()
        discovery = await self._try_discover_current()
        status = (
            self._status_from_discovery(discovery)
            if discovery is not None
            else self._cached_status(
                diagnostics=(
                    LocalDiagnostic(
                        "Start llama-server at the configured endpoint and refresh. "
                        f"Router setup: {LLAMA_CPP_SERVER_GUIDE_URL}",
                        "warning",
                        "connection",
                    ),
                ),
                stale=True,
            )
        )
        diagnostics = list(status.diagnostics)
        if cleanup_error:
            diagnostics.append(LocalDiagnostic(cleanup_error, "warning", "credentials"))
        return LocalConfigureResult(
            committed=True,
            backend_status=status,
            message="llama.cpp settings saved.",
            diagnostics=tuple(diagnostics),
            credential_orphaned=bool(cleanup_error),
        )

    async def status(self, context: LocalOperationContext) -> LocalBackendStatus:
        """Return current llama.cpp backend status and diagnostics.

        返回当前 llama.cpp 后端状态与诊断。
        """
        del context
        if self._last_discovery is not None:
            return self._status_from_discovery(self._last_discovery)
        if not self.configured:
            if self._state_error or self._endpoint_error:
                return self._cached_status()
            return self._status(
                "unconfigured",
                diagnostics=(
                    LocalDiagnostic(
                        "Tau will check the offered default endpoint when this backend opens. "
                        "Use Configure for a different server URL.",
                        "info",
                        "configuration",
                    ),
                ),
            )
        return self._cached_status()

    async def refresh(self, context: LocalOperationContext) -> LocalOperationResult:
        """Refresh model discovery and publish the resulting provider snapshot.

        刷新模型发现并发布所得提供商快照。
        """
        # /local is an explicit opt-in to probe exactly one effective endpoint:
        # saved state, LLAMA_BASE_URL, or the offered localhost default. This
        # is not a process/port/network scan and does not persist the default.
        # /local 是对一个有效端点的显式选择加入探测：已保存状态、LLAMA_BASE_URL 或提供的
        # localhost 默认值。这不是进程、端口或网络扫描，也不会持久化默认值。
        try:
            auth = await _resolve_auth_for_backend(self)
            discovery = await self.discover(auth, signal=context.signal)
            self._last_discovery = discovery
            self._save_discovery(discovery)
            self._publish_provider()
            return LocalOperationResult(backend_status=self._status_from_discovery(discovery))
        except asyncio.CancelledError:
            return LocalOperationResult(cancelled=True)
        except LlamaCppError as exc:
            return LocalOperationResult(
                backend_status=self._cached_status(
                    diagnostics=(LocalDiagnostic(str(exc), "error", "connection"),),
                    stale=True,
                ),
                diagnostics=(LocalDiagnostic(str(exc), "error", "connection"),),
            )

    async def doctor(self, context: LocalOperationContext) -> LocalOperationResult:
        """Probe endpoint health, streaming, and tool-call compatibility.

        探测端点健康、流式传输和工具调用兼容性。
        """
        try:
            auth = await _resolve_auth_for_backend(self)
            discovery = await self.discover(auth, signal=context.signal)
        except asyncio.CancelledError:
            return LocalOperationResult(cancelled=True)
        except LlamaCppError as exc:
            return LocalOperationResult(
                backend_status=self._cached_status(
                    diagnostics=(LocalDiagnostic(str(exc), "error", "connection"),),
                    stale=True,
                ),
                diagnostics=(LocalDiagnostic(str(exc), "error", "connection"),),
            )
        selected = _selected_model(self._active_state, discovery.models)
        if selected is None and self._stale_selected_model is not None:
            # Doctor is an explicit diagnostic action, not a model-selection
            # path. Probe a sole currently reported model so connectivity and
            # tool compatibility remain useful after the active reference goes
            # stale, while status/model selection still refuse to replace it.
            # Doctor 是显式诊断动作，不是模型选择路径。探测当前唯一报告的模型，使活动
            # 引用过期后连接与工具兼容性诊断仍有用，同时状态和模型选择仍拒绝替换它。
            selected = discovery.models[0].id if len(discovery.models) == 1 else None
        if selected is None:
            return LocalOperationResult(
                backend_status=self._status_from_discovery(discovery),
                diagnostics=(
                    LocalDiagnostic(
                        "The server is reachable but has no selectable model for Doctor.",
                        "warning",
                        "models",
                    ),
                ),
            )
        diagnostics = [
            LocalDiagnostic(
                f"Found llama.cpp at {discovery.endpoint.inference_base}", "info", "server"
            ),
            LocalDiagnostic(f"Discovered model: {selected}", "info", "models"),
        ]
        stream_ok, stream_message = await self._probe_stream(auth, selected, context.signal)
        diagnostics.append(
            LocalDiagnostic(stream_message, "info" if stream_ok else "error", "streaming")
        )
        if stream_ok:
            tools_ok, tools_message = await self._probe_tools(auth, selected, context.signal)
            diagnostics.append(
                LocalDiagnostic(tools_message, "info" if tools_ok else "warning", "tools")
            )
        return LocalOperationResult(
            backend_status=self._status_from_discovery(discovery), diagnostics=tuple(diagnostics)
        )

    async def load_model(
        self, model_id: str, context: LocalOperationContext
    ) -> LocalOperationResult:
        """Explicitly load one router model and reconcile before claiming success.

        显式加载一个路由器模型，并在报告成功前完成状态协调。
        """
        prepared = await self._prepare_router_operation(context)
        if isinstance(prepared, LocalOperationResult):
            return prepared
        client, owned, headers = prepared
        cancel_target: str | None = None
        try:
            models = await list_router_models(client, self.endpoint.server_root, headers)
            target = _router_model(models, model_id)
            if target is None:
                return self._router_failure(f"Router model is not available: {model_id}")
            if target.state in {"loaded", "sleeping"}:
                return await self._publish_router_models(models, message="Model is already loaded.")
            existing = tuple(
                model.id
                for model in models
                if model.id != model_id and model.state in {"loaded", "sleeping"}
            )
            if context.confirmation is None:
                choices = (
                    (
                        LocalConfirmationChoice("keep", "Load model and keep existing models"),
                        LocalConfirmationChoice("unload", "Unload existing models, then load"),
                        LocalConfirmationChoice("cancel", "Cancel"),
                    )
                    if existing
                    else (
                        LocalConfirmationChoice("load", "Load model"),
                        LocalConfirmationChoice("cancel", "Cancel"),
                    )
                )
                detail = " Other models are active on this shared router." if existing else ""
                return LocalOperationResult(
                    backend_status=self._status_from_router(models),
                    confirmation=LocalConfirmationRequest(
                        f"Load {model_id!r}? Loading can consume substantial memory and time."
                        + detail,
                        choices,
                    ),
                )
            if context.confirmation == "cancel":
                return LocalOperationResult(
                    cancelled=True, backend_status=self._status_from_router(models)
                )
            allowed = {"keep", "unload"} if existing else {"load"}
            if context.confirmation not in allowed:
                return self._router_failure("Loading requires explicit confirmation.")
            if context.confirmation == "unload":
                for existing_id in existing:
                    await mutate_router_model(
                        client,
                        self.endpoint.server_root,
                        headers,
                        action="unload",
                        model_id=existing_id,
                    )
                    await self._wait_for_router_state(
                        client, headers, existing_id, {"unloaded", "failed"}, context
                    )
            cancel_target = model_id
            await mutate_router_model(
                client,
                self.endpoint.server_root,
                headers,
                action="load",
                model_id=model_id,
            )
            models = await self._wait_for_router_state(
                client, headers, model_id, {"loaded", "sleeping", "failed"}, context
            )
            reconciled = _router_model(models, model_id)
            if reconciled is None or reconciled.state not in {"loaded", "sleeping"}:
                return self._router_failure(
                    "The router did not reach a loaded state. Refresh before retrying.", models
                )
            return await self._publish_router_models(models, message=f"Loaded {model_id}.")
        except asyncio.CancelledError:
            if cancel_target is not None:
                return await self._cancel_router_mutation(client, headers, cancel_target)
            return await self._reconcile_cancelled(client, headers)
        except (httpx.HTTPError, LlamaCppRouterError, TimeoutError) as exc:
            return await self._reconcile_after_router_failure(client, headers, exc)
        finally:
            if owned:
                await client.aclose()

    async def unload_model(
        self, model_id: str, context: LocalOperationContext
    ) -> LocalOperationResult:
        """Unload only after a model-specific explicit confirmation.

        仅在获得针对该模型的明确确认后卸载。
        """
        prepared = await self._prepare_router_operation(context)
        if isinstance(prepared, LocalOperationResult):
            return prepared
        client, owned, headers = prepared
        try:
            models = await list_router_models(client, self.endpoint.server_root, headers)
            if context.confirmation is None:
                return LocalOperationResult(
                    backend_status=self._status_from_router(models),
                    confirmation=LocalConfirmationRequest(
                        f"Unload {model_id!r} from this shared router?",
                        (
                            LocalConfirmationChoice("unload", "Unload model"),
                            LocalConfirmationChoice("cancel", "Keep model loaded"),
                        ),
                    ),
                )
            if context.confirmation == "cancel":
                return LocalOperationResult(
                    cancelled=True, backend_status=self._status_from_router(models)
                )
            if context.confirmation != "unload":
                return self._router_failure("Unloading requires explicit confirmation.")
            await mutate_router_model(
                client,
                self.endpoint.server_root,
                headers,
                action="unload",
                model_id=model_id,
            )
            models = await self._wait_for_router_state(
                client, headers, model_id, {"unloaded", "failed"}, context
            )
            return await self._publish_router_models(models, message=f"Unloaded {model_id}.")
        except asyncio.CancelledError:
            return await self._reconcile_cancelled(client, headers)
        except (httpx.HTTPError, LlamaCppRouterError, TimeoutError) as exc:
            return await self._reconcile_after_router_failure(client, headers, exc)
        finally:
            if owned:
                await client.aclose()

    async def download_model(
        self, model_id: str, context: LocalOperationContext
    ) -> LocalOperationResult:
        """Request one exact server-side repository download after confirmation.

        确认后请求下载一个精确的服务端仓库。
        """
        try:
            model_id = validate_repository_reference(model_id)
        except HuggingFaceSearchError as exc:
            return LocalOperationResult(
                diagnostics=(LocalDiagnostic(str(exc), "error", "download"),)
            )
        prepared = await self._prepare_router_operation(context)
        if isinstance(prepared, LocalOperationResult):
            return prepared
        client, owned, headers = prepared
        mutation_started = False
        progress_task: asyncio.Task[None] | None = None
        try:
            if context.confirmation is None:
                size = self._download_sizes.get(model_id)
                size_detail = (
                    f"Download size: {_format_bytes(size)}."
                    if size is not None
                    else "Download size is unavailable until the server reports transfer progress."
                )
                return LocalOperationResult(
                    confirmation=LocalConfirmationRequest(
                        f"Download {model_id!r} on the shared llama.cpp server? "
                        f"{size_detail} The download continues if this modal is closed.",
                        (
                            LocalConfirmationChoice("download", "Start server-side download"),
                            LocalConfirmationChoice("cancel", "Cancel"),
                        ),
                    )
                )
            if context.confirmation == "cancel":
                return LocalOperationResult(cancelled=True)
            if context.confirmation != "download":
                return self._router_failure("Downloading requires explicit confirmation.")
            progress_task = asyncio.create_task(
                watch_router_download_progress(
                    client,
                    self.endpoint.server_root,
                    headers,
                    model_id,
                    lambda downloaded, total: context.report_progress(
                        _download_progress(model_id, downloaded, total)
                    ),
                )
            )
            mutation_started = True
            await mutate_router_model(
                client,
                self.endpoint.server_root,
                headers,
                action="download",
                model_id=model_id,
            )
            models = await self._wait_for_router_state(
                client,
                headers,
                model_id,
                {"unloaded", "loaded", "sleeping", "failed"},
                context,
                minimum_polls=2,
                timeout_seconds=LLAMA_CPP_ROUTER_DOWNLOAD_TIMEOUT_SECONDS,
            )
            reconciled = _router_model(models, model_id)
            if reconciled is None or reconciled.state == "failed":
                return self._router_failure(
                    "The server-side download did not complete. Refresh before retrying.", models
                )
            return await self._publish_router_models(
                models, message=f"Server-side download completed for {model_id}."
            )
        except asyncio.CancelledError:
            if mutation_started:
                return await self._cancel_router_mutation(client, headers, model_id)
            return await self._reconcile_cancelled(client, headers)
        except (httpx.HTTPError, LlamaCppRouterError, TimeoutError) as exc:
            return await self._reconcile_after_router_failure(client, headers, exc)
        finally:
            if progress_task is not None:
                progress_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await progress_task
            if owned:
                await client.aclose()

    async def search_models(
        self, query: str, context: LocalOperationContext
    ) -> LocalOperationResult:
        """Search Hugging Face with a search-only token and return safe GGUF details.

        使用仅限搜索的令牌搜索 Hugging Face，并返回安全的 GGUF 详情。
        """
        del context
        owned = self.client is None
        client = self.client or create_async_client(timeout=self.timeout_seconds)
        token = discover_hf_token(self.environment)
        try:
            repositories = await search_gguf_repositories(client, query, token=token)
        except (httpx.HTTPError, HuggingFaceSearchError) as exc:
            return LocalOperationResult(diagnostics=(LocalDiagnostic(str(exc), "error", "search"),))
        finally:
            if owned:
                await client.aclose()
        for repository in repositories:
            for variant in repository.variants:
                if variant.size_bytes is not None:
                    self._download_sizes[f"{repository.id}:{variant.quantization}"] = (
                        variant.size_bytes
                    )
        results = tuple(
            LocalSearchResult(
                repository.id,
                repository.id,
                restricted=repository.gated,
                options=tuple(
                    LocalArtifactOption(
                        f"{repository.id}:{variant.quantization}",
                        variant.quantization,
                        variant.size_bytes,
                        recommended=variant.quantization == "Q4_K_M",
                    )
                    for variant in repository.variants
                ),
                diagnostic=(
                    "Gated repository: accept access on huggingface.co and ensure the "
                    "independent llama.cpp server process has its own authorized HF_TOKEN. "
                    "Tau's search token is never forwarded."
                    if repository.gated
                    else None
                ),
            )
            for repository in repositories
        )
        return LocalOperationResult(
            message=(
                f"Found {len(results)} GGUF repositories."
                if results
                else "No GGUF repositories found."
            ),
            search_results=results,
        )

    async def _prepare_router_operation(
        self, context: LocalOperationContext
    ) -> tuple[httpx.AsyncClient, bool, Mapping[str, str]] | LocalOperationResult:
        """Resolve auth, verify router support, and prepare an HTTP client.

        解析认证、验证路由器支持并准备 HTTP 客户端。
        """
        if context.cancelled:
            return LocalOperationResult(cancelled=True)
        if not self._router_capability.compatible:
            return self._router_failure(
                "Router management is unavailable until a compatible router is detected by Refresh."
            )
        auth = await _resolve_auth_for_backend(self)
        owned = self.client is None
        client = self.client or create_async_client(timeout=self.timeout_seconds)
        return client, owned, _auth_headers(auth)

    async def _wait_for_router_state(
        self,
        client: httpx.AsyncClient,
        headers: Mapping[str, str],
        model_id: str,
        terminal: set[str],
        context: LocalOperationContext,
        *,
        minimum_polls: int = 1,
        timeout_seconds: float = LLAMA_CPP_ROUTER_RECONCILE_TIMEOUT_SECONDS,
    ) -> tuple[RouterModel, ...]:
        """Poll until a router model reaches one of the expected states.

        轮询直到路由器模型达到预期状态之一。
        """
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        polls = 0
        while True:
            if context.cancelled:
                raise asyncio.CancelledError
            models = await list_router_models(client, self.endpoint.server_root, headers)
            await self._publish_router_models(models)
            polls += 1
            model = _router_model(models, model_id)
            if model is not None:
                context.report_progress(_router_progress(model))
                if model.state in terminal and polls >= minimum_polls:
                    return models
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("router reconciliation timed out")
            await asyncio.sleep(LLAMA_CPP_ROUTER_POLL_SECONDS)

    async def _publish_router_models(
        self,
        models: tuple[RouterModel, ...],
        *,
        message: str | None = None,
    ) -> LocalOperationResult:
        """Persist router models and publish their provider snapshot.

        持久化路由器模型并发布其提供商快照。
        """
        self._router_models = models
        discovery = LlamaCppDiscovery(
            self.endpoint,
            _router_provider_models(models),
            router_capability=self._router_capability,
            router_models=models,
        )
        self._last_discovery = discovery
        self._save_discovery(discovery)
        self._publish_provider()
        return LocalOperationResult(
            backend_status=self._status_from_router(models),
            message=message,
            committed=message is not None,
        )

    async def _cancel_router_mutation(
        self,
        client: httpx.AsyncClient,
        headers: Mapping[str, str],
        model_id: str,
    ) -> LocalOperationResult:
        """Request cancellation for one in-flight router mutation.

        请求取消一个进行中的路由器变更。
        """
        with suppress(Exception):
            await asyncio.shield(
                mutate_router_model(
                    client,
                    self.endpoint.server_root,
                    headers,
                    action="unload",
                    model_id=model_id,
                )
            )
        return await self._reconcile_cancelled(client, headers)

    async def _reconcile_cancelled(
        self, client: httpx.AsyncClient, headers: Mapping[str, str]
    ) -> LocalOperationResult:
        """Reconcile router state after a user-cancelled mutation.

        用户取消变更后协调路由器状态。
        """
        try:
            models = await asyncio.shield(
                list_router_models(client, self.endpoint.server_root, headers)
            )
            await self._publish_router_models(models)
            return LocalOperationResult(
                backend_status=self._status_from_router(models), cancelled=True
            )
        except Exception:
            return LocalOperationResult(
                backend_status=self._cached_status(stale=True),
                cancelled=True,
                diagnostics=(
                    LocalDiagnostic(
                        "Cancellation was requested, but reconciliation lost the connection. "
                        "Refresh before retrying; Tau will not replay the mutation.",
                        "warning",
                        "reconciliation",
                    ),
                ),
            )

    async def _reconcile_after_router_failure(
        self,
        client: httpx.AsyncClient,
        headers: Mapping[str, str],
        error: BaseException,
    ) -> LocalOperationResult:
        """Reconcile and report safe state after a router mutation failure.

        路由器变更失败后协调并报告安全状态。
        """
        try:
            models = await list_router_models(client, self.endpoint.server_root, headers)
            await self._publish_router_models(models)
            if isinstance(error, LlamaCppRouterError):
                diagnostic = LocalDiagnostic(
                    f"{error} State was refreshed; retry only after reviewing it.",
                    "error",
                    "router",
                )
            else:
                diagnostic = LocalDiagnostic(
                    f"The operation was interrupted ({error}). State was refreshed; retry "
                    "only after reviewing it.",
                    "warning",
                    "reconciliation",
                )
            return LocalOperationResult(
                backend_status=self._status_from_router(models),
                diagnostics=(diagnostic,),
            )
        except Exception:
            return LocalOperationResult(
                backend_status=self._cached_status(stale=True),
                diagnostics=(
                    LocalDiagnostic(
                        "The connection was lost and state could not be reconciled. Refresh before "
                        "retrying; Tau will not replay the interrupted mutation.",
                        "error",
                        "reconciliation",
                    ),
                ),
            )

    def _router_failure(
        self, message: str, models: tuple[RouterModel, ...] | None = None
    ) -> LocalOperationResult:
        """Build a failed local-operation result from a router error.

        根据路由器错误构建失败的本地操作结果。
        """
        return LocalOperationResult(
            backend_status=self._status_from_router(models) if models is not None else None,
            diagnostics=(LocalDiagnostic(message, "error", "router"),),
        )

    async def reset(self, context: LocalOperationContext) -> LocalOperationResult:
        """Remove saved llama.cpp configuration and owned credentials.

        移除已保存的 llama.cpp 配置及其拥有的凭据。
        """
        del context
        try:
            refs = self.state_store.clear()
        except LlamaCppStateError as exc:
            return LocalOperationResult(diagnostics=(LocalDiagnostic(str(exc), "error", "reset"),))
        self._active_state = None
        self._last_discovery = None
        self._router_capability = RouterCapability("standard")
        self._router_models = ()
        self._last_error = None
        self._stale_selected_model = None
        self._endpoint = self._endpoint_from_environment_or_default()
        self._publish_provider()
        diagnostics = (
            (
                LocalDiagnostic(
                    "Saved settings were removed. Stored credentials were retained; "
                    "confirm credential deletion separately.",
                    "warning",
                    "reset",
                ),
            )
            if refs
            else ()
        )
        return LocalOperationResult(
            message="llama.cpp integration settings reset.",
            backend_status=await self.status(_noop_context("reset")),
            diagnostics=diagnostics,
            committed=True,
        )

    def backend(self) -> LocalBackend:
        """Build the provider-neutral local-backend declaration.

        构建提供商无关的本地后端声明。
        """
        return LocalBackend(
            id=LLAMA_CPP_BACKEND_ID,
            provider_id=LLAMA_CPP_PROVIDER_ID,
            display_name=LLAMA_CPP_DISPLAY_NAME,
            configure_spec=LocalConfigureSpec(
                fields=(
                    LocalConfigField(
                        "endpoint",
                        "llama.cpp endpoint",
                        "text",
                        required=True,
                        placeholder=f"{LLAMA_CPP_DEFAULT_ENDPOINT} or .../v1",
                    ),
                    LocalConfigField(
                        "api_key",
                        "API key (optional)",
                        "secret",
                        required=False,
                        placeholder="Leave empty for no authentication",
                    ),
                )
            ),
            configure=self.configure,
            status=self.status,
            refresh=self.refresh,
            doctor=self.doctor,
            reset=self.reset,
            load_model=self.load_model,
            unload_model=self.unload_model,
            download_model=self.download_model,
            search_models=self.search_models,
            recommended=True,
        )

    def _resolve_effective_endpoint(
        self,
        active: LlamaCppIntegrationState | None,
    ) -> LlamaCppEndpoint | None:
        """Resolve the endpoint from saved state or the environment.

        从已保存状态或环境解析端点。
        """
        if active is not None:
            try:
                return normalize_llama_cpp_endpoint(active.endpoint)
            except LlamaCppEndpointError as exc:
                self._endpoint_error = str(exc)
                return None
        environment_endpoint = self.environment.get(LLAMA_CPP_ENDPOINT_ENV, "").strip()
        if not environment_endpoint:
            return None
        try:
            return normalize_llama_cpp_endpoint(environment_endpoint)
        except LlamaCppEndpointError as exc:
            self._endpoint_error = str(exc)
            return None

    def _endpoint_from_environment_or_default(self) -> LlamaCppEndpoint:
        """Return the environment endpoint or offered localhost default.

        返回环境端点或提供的 localhost 默认值。
        """
        endpoint = self._resolve_effective_endpoint(None)
        return endpoint or normalize_llama_cpp_endpoint(LLAMA_CPP_DEFAULT_ENDPOINT)

    def _cleanup_orphans(self) -> None:
        """Delete integration credentials no longer referenced by safe state.

        删除安全状态不再引用的集成凭据。
        """
        try:
            referenced = self.state_store.referenced_credentials()
            names = self.credential_store.names(prefix=LLAMA_CPP_CREDENTIAL_PREFIX)
            orphaned: list[str] = []
            for name in names:
                if name in referenced:
                    continue
                try:
                    self.credential_store.delete(name)
                except Exception:
                    orphaned.append(name)
            self._orphaned_credentials = tuple(orphaned)
        except Exception:
            self._orphaned_credentials = ()

    def _delete_unreferenced_credentials(self, old_ref: str | None) -> str | None:
        """Delete an old credential when no saved endpoint references it.

        当已保存端点不再引用旧凭据时删除它。
        """
        if not old_ref:
            return None
        try:
            if old_ref not in self.state_store.referenced_credentials():
                self.credential_store.delete(old_ref)
        except Exception:
            return "The new configuration is active, but the previous credential needs cleanup."
        return None

    async def _try_discover_current(self) -> LlamaCppDiscovery | None:
        """Best-effort discover the current endpoint without raising.

        尽力发现当前端点而不抛出异常。
        """
        try:
            auth = await _resolve_auth_for_backend(self)
            discovery = await self.discover(auth)
        except (LlamaCppError, ProviderAuthError):
            return None
        self._last_discovery = discovery
        self._save_discovery(discovery)
        self._publish_provider()
        return discovery

    def _save_discovery(self, discovery: LlamaCppDiscovery) -> None:
        """Persist safe discovery metadata while preserving explicit selection.

        持久化安全发现元数据，同时保留显式选择。
        """
        if (
            self._active_state is None
            or self._active_state.endpoint != discovery.endpoint.server_root
        ):
            # Environment endpoints are intentionally not copied to safe state.
            # 环境端点有意不复制到安全状态。
            return
        previous_selected = self._active_state.selected_model
        selected = _selected_model(self._active_state, discovery.models)
        self._stale_selected_model = (
            previous_selected
            if previous_selected is not None
            and selected is None
            and previous_selected not in {model.id for model in discovery.models}
            else None
        )
        # Preserve the user's exact selection as a stable reference even
        # when the server temporarily omits it. ``selected`` is intentionally
        # only the currently usable choice; it must not erase the reference
        # needed for a later refresh or cached resume.
        # 将用户的精确选择保留为稳定引用，即使服务器暂时省略它。``selected`` 仅表示当前
        # 可用选择，不能擦除稍后刷新或缓存恢复所需的引用。
        state = LlamaCppIntegrationState(
            endpoint=discovery.endpoint.server_root,
            selected_model=previous_selected if previous_selected is not None else selected,
            credential_ref=self._active_state.credential_ref,
            models=tuple(_stored_model(model) for model in discovery.models),
            checked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            self.state_store.save(state)
            self._active_state = state
        except LlamaCppStateError as exc:
            self._state_error = str(exc)

    def _publish_provider(self) -> None:
        """Register or update the dynamic provider and paired backend.

        注册或更新动态提供商及配对后端。
        """
        provider = self.provider()
        updated = self._update_provider(provider) if self._update_provider is not None else False
        if not updated and self._register_provider is not None:
            self._register_provider(provider)
        if not updated and self._register_backend is not None:
            # Initial setup and a source replacement need a paired backend
            # registration. Snapshot updates use ``update_provider`` above so
            # an in-flight local operation keeps its source-layer token.
            # 初始设置和来源替换需要配对后端注册。快照更新使用上方的 ``update_provider``，
            # 使进行中的本地操作保留其来源层令牌。
            self._register_backend(self.backend())

    def _status_from_discovery(self, discovery: LlamaCppDiscovery) -> LocalBackendStatus:
        """Convert a discovery snapshot into host-renderable backend status.

        将发现快照转换为宿主可渲染的后端状态。
        """
        self._last_error = None
        if discovery.router_capability.compatible:
            return self._status_from_router(discovery.router_models)
        stale_model = self._stale_selected_model
        diagnostics: list[LocalDiagnostic] = []
        if discovery.router_capability.diagnostic:
            diagnostics.append(
                LocalDiagnostic(discovery.router_capability.diagnostic, "warning", "router")
            )
        if not discovery.models:
            diagnostics.append(
                LocalDiagnostic(
                    "Server is ready but exposes no selectable models.", "warning", "models"
                )
            )
        if stale_model is not None:
            diagnostics.append(
                LocalDiagnostic(
                    f"The active model {stale_model!r} is no longer reported by the server; "
                    "the current runtime remains usable, but select it again after it returns.",
                    "warning",
                    "models",
                )
            )
        return self._status(
            "stale"
            if stale_model is not None
            else ("ready" if discovery.models else "unavailable"),
            models=discovery.models,
            selected=(
                None
                if stale_model is not None
                else _selected_model(self._active_state, discovery.models)
            ),
            diagnostics=tuple(diagnostics),
            stale=stale_model is not None,
        )

    def _status_from_router(self, models: tuple[RouterModel, ...]) -> LocalBackendStatus:
        """Convert router model states into host-renderable backend status.

        将路由器模型状态转换为宿主可渲染的后端状态。
        """
        selectable = _router_provider_models(models)
        selected = _selected_model(self._active_state, selectable)
        diagnostics: list[LocalDiagnostic] = []
        failed = tuple(model.id for model in models if model.state == "failed")
        unknown = tuple(model.id for model in models if model.state == "unknown")
        if failed:
            diagnostics.append(
                LocalDiagnostic("Failed router models: " + ", ".join(failed), "warning", "models")
            )
        if unknown:
            diagnostics.append(
                LocalDiagnostic(
                    "Models with unknown router state are management-only: " + ", ".join(unknown),
                    "warning",
                    "models",
                )
            )
        if models and not selectable:
            diagnostics.append(
                LocalDiagnostic(
                    "The router is ready, but no model is loaded. Select an unloaded model to "
                    "load it.",
                    "info",
                    "models",
                )
            )
        actions: list[LocalAction] = [
            "configure",
            "refresh",
            "doctor",
            "reset",
            "load_model",
            "unload_model",
            "download_model",
            "search_models",
        ]
        if selected is not None:
            actions.append("use")
        return LocalBackendStatus(
            state="ready",
            endpoint_display=self.endpoint.inference_base,
            authentication_source=_authentication_source(
                self._active_state, self.environment, self.credential_store
            ),
            models=tuple(
                LocalModel(model.id, model.display_name or model.id, model.state)
                for model in models
            ),
            selected_model=selected,
            actions=tuple(actions),
            diagnostics=tuple(diagnostics),
        )

    def _cached_status(
        self,
        *,
        diagnostics: tuple[LocalDiagnostic, ...] = (),
        stale: bool = False,
    ) -> LocalBackendStatus:
        """Build status from the latest safe cached integration snapshot.

        根据最新的安全缓存集成快照构建状态。
        """
        active = self._active_state
        models = tuple(_provider_model(model) for model in active.models) if active else ()
        selected = _selected_model(active, models)
        if self._state_error:
            diagnostics = (*diagnostics, LocalDiagnostic(self._state_error, "warning", "state"))
        if self._endpoint_error:
            diagnostics = (*diagnostics, LocalDiagnostic(self._endpoint_error, "error", "endpoint"))
        if self._stale_selected_model is not None:
            diagnostics = (
                *diagnostics,
                LocalDiagnostic(
                    f"The active model {self._stale_selected_model!r} is not in the cached "
                    "snapshot; the current runtime remains usable.",
                    "warning",
                    "models",
                ),
            )
        return self._status(
            "stale"
            if (stale or self._stale_selected_model is not None) and models
            else "unavailable",
            models=models,
            selected=(None if self._stale_selected_model is not None else selected),
            diagnostics=diagnostics,
            cached=bool(models),
            stale=stale or self._stale_selected_model is not None,
        )

    def _status(
        self,
        state: str,
        *,
        models: tuple[ProviderModel, ...] = (),
        selected: str | None = None,
        diagnostics: tuple[LocalDiagnostic, ...] = (),
        cached: bool = False,
        stale: bool = False,
    ) -> LocalBackendStatus:
        """Assemble a provider-neutral backend status response.

        组装提供商无关的后端状态响应。
        """
        actions: list[LocalAction] = ["configure", "refresh", "doctor", "reset"]
        if selected is not None:
            actions.append("use")
        return LocalBackendStatus(
            state=state,  # type: ignore[arg-type]
            endpoint_display=self.endpoint.inference_base,
            authentication_source=_authentication_source(
                self._active_state,
                self.environment,
                self.credential_store,
            ),
            models=tuple(_local_model(model) for model in models),
            selected_model=selected,
            actions=tuple(actions),
            diagnostics=diagnostics,
            cached=cached,
            stale=stale,
        )

    async def _probe_stream(
        self,
        auth: ResolvedProviderAuth,
        model: str,
        signal: SimpleCancellationToken,
    ) -> tuple[bool, str]:
        """Probe whether the endpoint accepts streaming chat completions.

        探测端点是否接受流式聊天补全。
        """
        owned_client = self.client is None
        client = self.client or create_async_client(timeout=self.timeout_seconds)
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key=auth.api_key or "",
                base_url=self.endpoint.inference_base,
                provider_name=LLAMA_CPP_PROVIDER_ID,
                timeout_seconds=self.timeout_seconds,
                max_retries=0,
                omit_authorization_header=auth.omit_authorization_header,
                infer_api_from_model=False,
            ),
            client=client,
        )
        try:
            async for event in provider.stream_response(
                model=model,
                system="Reply briefly.",
                messages=[UserMessage(content="Reply with exactly: OK")],
                tools=[],
                signal=signal,
            ):
                if isinstance(event, AssistantStartEvent):
                    return True, "Streaming chat completions accepted."
                if isinstance(event, AssistantErrorEvent):
                    return False, f"Streaming chat completions failed: {event.error.text}"
        except httpx.HTTPError:
            return False, "Streaming chat completions could not be reached."
        finally:
            if owned_client:
                await provider.aclose()
        return False, "The server did not accept a streaming completion."

    async def _probe_tools(
        self,
        auth: ResolvedProviderAuth,
        model: str,
        signal: SimpleCancellationToken,
    ) -> tuple[bool, str]:
        """Probe whether the endpoint can emit a valid tool call.

        探测端点是否能发出有效工具调用。
        """
        async def executor(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: Callable[[AgentToolResult], None] | None = None,
        ) -> AgentToolResult:
            """Return a deterministic result for the diagnostic probe tool.

            为诊断探测工具返回确定性结果。
            """
            del tool_call_id, arguments, signal, on_update
            return AgentToolResult(content=[TextContent(text="ok")])

        tool = AgentTool(
            name="tau_probe",
            label="tau_probe",
            description="Call this tool to verify tool calling.",
            parameters={"type": "object", "properties": {}},
            execute_fn=executor,
        )
        owned_client = self.client is None
        client = self.client or create_async_client(timeout=self.timeout_seconds)
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key=auth.api_key or "",
                base_url=self.endpoint.inference_base,
                provider_name=LLAMA_CPP_PROVIDER_ID,
                timeout_seconds=self.timeout_seconds,
                max_retries=0,
                omit_authorization_header=auth.omit_authorization_header,
                infer_api_from_model=False,
            ),
            client=client,
        )
        try:
            async for event in provider.stream_response(
                model=model,
                system="Call the tau_probe tool.",
                messages=[UserMessage(content="Call tau_probe now.")],
                tools=[tool],
                signal=signal,
            ):
                if isinstance(event, ToolCallEndEvent):
                    return True, "Tool calls supported."
                if isinstance(event, AssistantErrorEvent):
                    return False, f"Tool-call probe failed: {event.error.text}"
        except httpx.HTTPError:
            return False, "Tool-call compatibility could not be checked."
        finally:
            if owned_client:
                await provider.aclose()
        return False, (
            "Streaming works, but the model did not emit a tool call. Use a tool-capable "
            "instruct GGUF and a compatible llama.cpp chat template."
        )

    async def _detect_router_safely(
        self,
        client: httpx.AsyncClient,
        headers: Mapping[str, str],
    ) -> RouterCapability:
        """Detect router capability while converting failures to diagnostics.

        检测路由器能力，并将故障转换为诊断。
        """
        try:
            return await detect_router(client, self.endpoint.server_root, headers)
        except LlamaCppRouterError as exc:
            return RouterCapability(
                "incompatible",
                diagnostic=(
                    f"Router capabilities could not be verified ({exc}); management is "
                    "disabled and standard discovery remains available."
                ),
            )

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: Mapping[str, str],
        signal: CancellationToken | None,
    ) -> httpx.Response:
        """Issue one cancellable authenticated GET with safe HTTP errors.

        发出一次可取消的认证 GET 请求，并生成安全 HTTP 错误。
        """
        if signal is not None and signal.is_cancelled():
            raise asyncio.CancelledError
        try:
            response = await client.get(url, headers=dict(headers))
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise
        if response.status_code in {401, 403}:
            raise _HttpFailure(
                response.status_code,
                "llama.cpp rejected the request. Check the optional API key or LLAMA_API_KEY.",
            )
        if response.status_code == 503 and url.endswith("/health"):
            health = _safe_json(response)
            if _health_state(health) == "loading":
                raise LlamaCppError(
                    "llama.cpp is still loading a model. Wait for it to become ready and retry."
                )
        if response.status_code == 404 and url.endswith("/health"):
            return response
        if response.status_code >= 400:
            raise _HttpFailure(
                response.status_code,
                f"llama.cpp returned HTTP {response.status_code} while checking its server.",
            )
        return response


def normalize_llama_cpp_endpoint(value: str) -> LlamaCppEndpoint:
    """Normalize a server root or OpenAI-compatible ``/v1`` URL safely.

    安全规范化服务器根地址或 OpenAI 兼容的 ``/v1`` URL。
    """
    if not isinstance(value, str) or not value.strip():
        raise LlamaCppEndpointError("llama.cpp endpoint cannot be empty")
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise LlamaCppEndpointError("llama.cpp endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise LlamaCppEndpointError("llama.cpp endpoint must not contain credentials")
    if parsed.query or parsed.fragment:
        raise LlamaCppEndpointError("llama.cpp endpoint must not contain a query or fragment")
    if not parsed.netloc or parsed.hostname is None:
        raise LlamaCppEndpointError("llama.cpp endpoint must include a host")
    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    elif path.endswith("/v1"):
        path = path[: -len("/v1")].rstrip("/")
    root = urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))
    root = root.rstrip("/")
    if not root:
        raise LlamaCppEndpointError("llama.cpp endpoint must include a host")
    return LlamaCppEndpoint(server_root=root, inference_base=f"{root}/v1")


def _auth_headers(auth: ResolvedProviderAuth) -> dict[str, str]:
    """Build request headers from resolved runtime authentication.

    从已解析的运行时认证信息构建请求头。
    """
    headers = dict(auth.headers)
    if auth.api_key is not None and not auth.omit_authorization_header:
        headers.setdefault("Authorization", f"Bearer {auth.api_key}")
    return headers


async def _resolve_auth_for_backend(service: LlamaCppService) -> ResolvedProviderAuth:
    """Resolve authentication for one local-backend operation.

    为一次本地后端操作解析认证信息。
    """
    active = service._active_state
    return await _LlamaCppAuth(active.credential_ref if active else None).resolve(
        ProviderAuthContext(
            credentials=service.credential_store,
            environment=service.environment,
        )
    )


def _safe_json(response: httpx.Response) -> object:
    """Decode response JSON, returning a neutral value on malformed input.

    解码响应 JSON，格式错误时返回中性值。
    """
    try:
        return response.json()
    except ValueError:
        return None


def _json_object(response: httpx.Response, endpoint: str) -> Mapping[str, object]:
    """Decode an endpoint response as a required JSON object.

    将端点响应解码为必需的 JSON 对象。
    """
    payload = _safe_json(response)
    if not isinstance(payload, Mapping):
        raise LlamaCppError(f"llama.cpp {endpoint} returned malformed JSON.")
    return payload


def _health_state(payload: object) -> str:
    """Extract a bounded server health state from a payload.

    从载荷提取有界服务器健康状态。
    """
    if not isinstance(payload, Mapping):
        return "ok"
    status = payload.get("status")
    if isinstance(status, str):
        normalized = status.casefold()
        if any(word in normalized for word in ("loading", "starting", "initializing")):
            return "loading"
        if any(word in normalized for word in ("unavailable", "error", "failed")):
            return "unavailable"
    return "ok"


def _parse_models(payload: Mapping[str, object]) -> tuple[ProviderModel, ...]:
    """Parse provider models from a llama.cpp discovery response.

    从 llama.cpp 发现响应解析提供商模型。
    """
    data = payload.get("data")
    if not isinstance(data, list):
        raise LlamaCppError("llama.cpp /v1/models returned a malformed model list.")
    models: list[ProviderModel] = []
    ids: set[str] = set()
    for item in data:
        if not isinstance(item, Mapping):
            raise LlamaCppError("llama.cpp /v1/models returned a malformed model.")
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id or model_id != model_id.strip():
            raise LlamaCppError("llama.cpp /v1/models returned a model without an exact id.")
        if model_id in ids:
            raise LlamaCppError("llama.cpp /v1/models returned duplicate model ids.")
        ids.add(model_id)
        display = item.get("name", item.get("display_name"))
        display_name = display if isinstance(display, str) and display.strip() else model_id
        modalities = _modalities(item)
        context_window = _reported_context_window(item)
        models.append(
            ProviderModel(
                id=model_id,
                display_name=display_name,
                api="openai-completions",
                context_window=context_window,
                input_modalities=modalities,
                # No output-limit, reasoning, cost, or tool-support guesses.
                # 不猜测输出上限、推理、成本或工具支持能力。
                compat=_safe_model_compat(item),
            )
        )
    return tuple(models)


def _modalities(item: Mapping[str, object]) -> tuple[Literal["text", "image"], ...] | None:
    """Extract supported input modalities from model metadata.

    从模型元数据提取受支持的输入模态。
    """
    value = item.get("input_modalities", item.get("modalities"))
    if not isinstance(value, list) or not value:
        return None
    if not all(isinstance(entry, str) and entry in {"text", "image"} for entry in value):
        return None
    result = tuple(dict.fromkeys(value))
    return cast(tuple[Literal["text", "image"], ...], result) or None


def _reported_context_window(item: Mapping[str, object]) -> int | None:
    """Extract a positive reported context-window size.

    提取报告的正上下文窗口大小。
    """
    # These names are accepted only when directly reported by a server. Tau
    # never derives one from a model family, max tokens, or a 128K convention.
    # 这些名称仅在服务器直接报告时接受。Tau 绝不会根据模型系列、最大令牌数或 128K
    # 约定推导上下文窗口。
    for key in ("context_window", "context_length"):
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _safe_model_compat(item: Mapping[str, object]) -> Mapping[str, JSONValue]:
    """Extract allowlisted compatibility metadata for a model.

    提取模型的允许列表兼容性元数据。
    """
    # Keep a tiny, non-secret diagnostic subset in memory.  It is not copied to
    # the disk snapshot and is never interpreted as capability metadata.
    # 在内存中仅保留少量不含敏感信息的诊断子集。它不会复制到磁盘快照，也绝不会被解释
    # 为能力元数据。
    result: dict[str, JSONValue] = {}
    for key in ("object", "owned_by"):
        value = item.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return MappingProxyType(result)


def _provider_model(model: LlamaCppStoredModel) -> ProviderModel:
    """Convert persisted model metadata into a provider model.

    将持久化模型元数据转换为提供商模型。
    """
    return ProviderModel(
        id=model.id,
        display_name=model.display_name or model.id,
        api="openai-completions",
        context_window=model.context_window,
        input_modalities=(
            cast(tuple[Literal["text", "image"], ...], model.input_modalities)
            if model.input_modalities is not None
            else None
        ),
    )


def _stored_model(model: ProviderModel) -> LlamaCppStoredModel:
    """Convert a provider model into safe persisted metadata.

    将提供商模型转换为安全的持久化元数据。
    """
    return LlamaCppStoredModel(
        id=model.id,
        display_name=model.display_name,
        context_window=model.context_window,
        input_modalities=model.input_modalities,
    )


def _local_model(model: ProviderModel) -> LocalModel:
    """Convert a provider model into a local-backend model view.

    将提供商模型转换为本地后端模型视图。
    """
    return LocalModel(model.id, model.display_name or model.id)


def _router_provider_models(models: tuple[RouterModel, ...]) -> tuple[ProviderModel, ...]:
    """Convert available router models into provider models.

    将可用路由器模型转换为提供商模型。
    """
    return tuple(
        ProviderModel(
            id=model.id,
            display_name=model.display_name or model.id,
            api="openai-completions",
            input_modalities=model.input_modalities,
        )
        for model in models
        if model.state in {"loaded", "sleeping"}
    )


def _router_model(models: tuple[RouterModel, ...], model_id: str) -> RouterModel | None:
    """Find one router model by exact identifier.

    按精确标识符查找一个路由器模型。
    """
    return next((model for model in models if model.id == model_id), None)


def _router_progress(model: RouterModel) -> LocalProgress:
    """Build host progress from one router model state.

    根据一个路由器模型状态构建宿主进度。
    """
    if (
        model.state == "downloading"
        and model.downloaded_bytes is not None
        and model.download_total_bytes is not None
    ):
        return _download_progress(
            model.id,
            model.downloaded_bytes,
            model.download_total_bytes,
        )
    messages = {
        "loading": f"Loading {model.id}…",
        "downloading": f"Downloading {model.id} on the llama.cpp server…",
        "loaded": f"Loaded {model.id}.",
        "sleeping": f"{model.id} is sleeping and available.",
        "unloaded": f"Unloaded {model.id}.",
        "failed": f"Router operation failed for {model.id}.",
        "unknown": f"Waiting for a reconciled state for {model.id}…",
    }
    return LocalProgress(
        messages[model.state], done=model.state in {"loaded", "sleeping", "unloaded", "failed"}
    )


def _download_progress(model_id: str, downloaded_bytes: int, total_bytes: int) -> LocalProgress:
    """Build a bounded download progress update for one model.

    为一个模型构建有界下载进度更新。
    """
    downloaded = min(downloaded_bytes, total_bytes)
    remaining = total_bytes - downloaded
    return LocalProgress(
        f"Downloading {model_id} on the llama.cpp server… "
        f"{_format_bytes(downloaded)} / {_format_bytes(total_bytes)} "
        f"({downloaded / total_bytes:.1%}; {_format_bytes(remaining)} remaining)",
        fraction=downloaded / total_bytes,
    )


def _format_bytes(value: int) -> str:
    """Format a byte count for user-facing progress text.

    为面向用户的进度文本格式化字节数。
    """
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


def _selected_model(
    state: LlamaCppIntegrationState | None,
    models: tuple[ProviderModel, ...],
) -> str | None:
    """Return an explicitly selected model only while it remains available.

    仅在显式选择的模型仍可用时返回它。
    """
    ids = {model.id for model in models}
    if state is not None and state.selected_model is not None:
        # Never silently replace an explicitly selected model when the server
        # no longer reports it. The active runtime may continue using it, but
        # a new selection must wait for the model to return.
        # 当服务器不再报告显式选择的模型时，绝不静默替换它。活动运行时可以继续使用它，
        # 但新的选择必须等待该模型重新出现。
        return state.selected_model if state.selected_model in ids else None
    if len(models) == 1:
        return models[0].id
    return None


def _cached_default(models: tuple[ProviderModel, ...]) -> str | None:
    """Choose a deterministic default from cached models.

    从缓存模型中选择确定性默认值。
    """
    return models[0].id if len(models) == 1 else None


def _authentication_source(
    state: LlamaCppIntegrationState | None,
    environment: Mapping[str, str],
    credentials: CredentialStore | None = None,
) -> Literal["none", "environment", "stored credential"]:
    """Describe the effective authentication source without exposing secrets.

    在不暴露敏感信息的情况下说明有效认证来源。
    """
    if state is not None and state.credential_ref:
        try:
            if credentials is None or credentials.get(state.credential_ref):
                return "stored credential"
        except Exception:
            pass
    if environment.get(LLAMA_CPP_API_KEY_ENV):
        return "environment"
    return "none"


def _noop_context(action: str) -> LocalOperationContext:
    """Build an always-current operation context for internal status refreshes.

    为内部状态刷新构建始终有效的操作上下文。
    """
    signal = SimpleCancellationToken()
    return LocalOperationContext(
        signal=signal,
        action=action,  # type: ignore[arg-type]
        generation_id="reset",
        backend_id=LLAMA_CPP_BACKEND_ID,
        source_id="built-in:llama.cpp",
        _is_current=lambda: True,
        _progress=lambda _: None,
    )


__all__ = [
    "DEFAULT_LLAMA_CPP_TIMEOUT_SECONDS",
    "LLAMA_CPP_API_KEY_ENV",
    "LLAMA_CPP_BACKEND_ID",
    "LLAMA_CPP_DEFAULT_ENDPOINT",
    "LLAMA_CPP_DISPLAY_NAME",
    "LLAMA_CPP_ENDPOINT_ENV",
    "LLAMA_CPP_PROVIDER_ID",
    "LlamaCppDiscovery",
    "LlamaCppEndpoint",
    "LlamaCppEndpointError",
    "LlamaCppError",
    "LlamaCppService",
    "normalize_llama_cpp_endpoint",
]
