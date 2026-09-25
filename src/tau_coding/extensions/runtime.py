"""Extension runtime: hook dispatch, tool wrapping, and session binding.

扩展运行时：钩子分发、工具包装与会话绑定。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from pathlib import Path
from time import time_ns
from typing import Literal, Protocol, cast

import httpx

import tau_coding.built_in_extensions as built_in_extension_registry
from tau_agent.events import AgentEvent, AgentStartEvent
from tau_agent.events import TurnEndEvent as AgentTurnEndEvent
from tau_agent.events import TurnStartEvent as AgentTurnStartEvent
from tau_agent.messages import AgentMessage, TextContent
from tau_agent.tools import (
    AgentTool,
    AgentToolResult,
    ToolCancellationToken,
    ToolUpdateCallback,
)
from tau_agent.types import JSONValue
from tau_coding.built_in_extensions import BuiltInExtension, BuiltInExtensionContext
from tau_coding.commands import (
    CommandContext,
    CommandRegistry,
    CommandResult,
    SlashCommand,
    create_default_command_registry,
)
from tau_coding.credentials import CredentialStore, FileCredentialStore, credentials_path
from tau_coding.extensions.api import (
    AGENT_EVENT_TYPES,
    AGENT_EVENT_WILDCARD,
    LIFECYCLE_EVENT_TYPES,
    CustomMessageView,
    ExtensionAPI,
    ExtensionCommandContext,
    ExtensionCommandHandler,
    ExtensionContext,
    ExtensionError,
    ExtensionGeneration,
    ExtensionHandler,
    InputEvent,
    InputHookResult,
    MessageRenderer,
    MessageRenderOptions,
    NullUiBridge,
    RegisteredExtension,
    SessionLifecycleReason,
    SessionShutdownEvent,
    SessionStartEvent,
    ToolCallHookEvent,
    ToolCallHookResult,
    ToolResultHookEvent,
    ToolResultHookResult,
    TurnEndEvent,
    TurnStartEvent,
    UiBridge,
)
from tau_coding.extensions.loader import (
    ExtensionSourceMetadata,
    LoadedExtension,
    load_extensions,
    unload_extension_modules,
)
from tau_coding.extensions.provider_registry import (
    DynamicProviderRegistry,
    ProviderRegistryCloseResult,
)
from tau_coding.extensions.providers import CredentialReader, DynamicProvider
from tau_coding.local_backends import LocalBackend, LocalBackendRegistry
from tau_coding.paths import TauPaths
from tau_coding.project_trust import ExtensionTrustResult, ProjectTrustEvent
from tau_coding.provider_config import ProviderConfig
from tau_coding.resources import ResourceDiagnostic, TauResourcePaths
from tau_coding.system_prompt import PromptSection

# Host callback that delivers a message through the frontend's serialized run
# path when the session is idle. Carries the same presentation metadata as a
# queued message so custom messages render correctly whether they trigger a new
# turn or are injected into a running one.
# 宿主回调会在会话空闲时通过前端的串行运行路径传递消息。它携带与排队消息相同的展示
# 元数据，使自定义消息无论触发新轮次还是注入正在运行的轮次都能正确渲染。
TurnRequestedCallback = Callable[[str, "str | None", "dict[str, JSONValue] | None"], None]


class BoundSession(Protocol):
    """The slice of `CodingSession` the extension runtime binds to.

    扩展运行时绑定的 `CodingSession` 接口片段。
    """

    # Return the working directory bound to this session.
    #
    # 返回此会话绑定的工作目录。
    @property
    def cwd(self) -> Path: ...

    # Return the selected model identifier.
    #
    # 返回当前选定的模型标识符。
    @property
    def model(self) -> str: ...

    # Return the display name of the selected provider.
    #
    # 返回当前选定提供者的显示名称。
    @property
    def provider_name(self) -> str: ...

    # Return the selected inference-provider route, if configured.
    #
    # 如果已配置，则返回选定的推理提供者路由。
    @property
    def inference_provider(self) -> str | None: ...

    # Return the active inference-provider routing mode.
    #
    # 返回当前生效的推理提供者路由模式。
    @property
    def inference_provider_mode(self) -> str: ...

    # Return this session's persisted identifier, if available.
    #
    # 如果存在，则返回此会话的持久化标识符。
    @property
    def session_id(self) -> str | None: ...

    # Return the user-assigned session name, if any.
    #
    # 如果用户设置了会话名称，则返回该名称。
    @property
    def session_name(self) -> str | None: ...

    # Return the active model thinking level.
    #
    # 返回当前模型的思考级别。
    @property
    def thinking_level(self) -> str: ...

    # Return the assembled system prompt for this session.
    #
    # 返回此会话组装完成的系统提示词。
    @property
    def system_prompt(self) -> str: ...

    # Return whether the session is currently running an agent turn.
    #
    # 返回会话当前是否正在运行代理轮次。
    @property
    def is_running(self) -> bool: ...

    # Return an immutable snapshot of the current transcript.
    #
    # 返回当前会话记录的不可变快照。
    @property
    def messages(self) -> tuple[AgentMessage, ...]: ...

    # Queue a message to steer the active run.
    #
    # 将一条消息加入队列，以引导当前运行。
    def queue_steering_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None: ...

    # Queue a message to run after the active tool work.
    #
    # 将一条消息加入队列，以便当前工具工作完成后运行。
    def queue_follow_up_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None: ...

    # Persist extension-owned data in the session history.
    #
    # 将扩展拥有的数据持久化到会话历史中。
    async def append_custom_entry(self, namespace: str, data: dict[str, JSONValue]) -> None: ...

    # Set or clear a bookmark label on a session entry.
    #
    # 为会话条目设置或清除书签标签。
    async def set_label(self, target_id: str, label: str | None) -> object: ...

    # Select an inference-provider route and return the resulting value.
    #
    # 选择推理提供者路由，并返回最终生效的值。
    def set_inference_provider(self, route: str | None) -> str: ...


@dataclass(frozen=True, slots=True)
class ExtensionCommand:
    """A slash command registered by an extension.

    扩展注册的斜杠命令。
    """

    extension: str
    source_id: str
    name: str
    description: str
    usage: str
    aliases: tuple[str, ...]
    handler: ExtensionCommandHandler


@dataclass(frozen=True, slots=True)
class RegisteredExtensionTool:
    """A tool registered by an extension.

    扩展注册的工具。
    """

    extension: str
    source_id: str
    tool: AgentTool


@dataclass(frozen=True, slots=True)
class InputHookOutcome:
    """Combined outcome of running all `input` hooks over prompt text.

    对提示词文本运行所有 `input` 钩子后的组合结果。
    """

    handled: bool
    text: str
    message: str | None = None


class ExtensionRuntime:
    """Owns loaded extensions and dispatches events between them and a session.

    管理已加载的扩展，并在扩展与会话之间分发事件。

    Each runtime belongs to one prepared session snapshot. Reload and
    destination replacement stage a fresh runtime, then retire the prior
    generation only after preparation succeeds. This prevents project extension
    registrations from crossing cwd trust boundaries.

    每个运行时都属于一个已准备好的会话快照。重新加载和目标替换会先准备新的运行时，
    只有准备成功后才退役旧代际，从而防止项目扩展注册跨越工作目录信任边界。
    """

    def __init__(
        self,
        *,
        ui: UiBridge | None = None,
        durable_providers: Sequence[ProviderConfig] = (),
        credentials: CredentialReader | None = None,
        environment: Mapping[str, str] | None = None,
        built_in_extensions: Sequence[BuiltInExtension] | None = None,
        paths: TauPaths | None = None,
        built_in_credentials: CredentialStore | None = None,
        built_in_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create one extension generation and its owned registries.

        创建一个扩展代际及其拥有的注册表。
        """
        self._generation = ExtensionGeneration()
        self._built_in_extensions = tuple(
            built_in_extension_registry.BUILT_IN_EXTENSIONS
            if built_in_extensions is None
            else built_in_extensions
        )
        self._built_ins_loaded = False
        self._durable_providers = tuple(durable_providers)
        self._provider_credentials = credentials
        self._provider_environment = environment
        built_in_paths = paths or TauPaths()
        resolved_built_in_credentials: CredentialStore = (
            built_in_credentials
            if built_in_credentials is not None
            else (
                cast(CredentialStore, credentials)
                if credentials is not None
                and all(
                    callable(getattr(credentials, name, None))
                    for name in ("set", "delete", "names")
                )
                else FileCredentialStore(credentials_path(built_in_paths))
            )
        )
        self._built_in_context = BuiltInExtensionContext(
            paths=built_in_paths,
            credential_store=resolved_built_in_credentials,
            environment=dict(environment) if environment is not None else dict(os.environ),
            http_client=built_in_http_client,
        )
        self._provider_registry = DynamicProviderRegistry(
            self._durable_providers,
            generation_id=self._generation.id,
            credentials=credentials,
            environment=environment,
        )
        self._local_backend_registry = LocalBackendRegistry(
            self._provider_registry,
            generation_id=self._generation.id,
        )
        self._retired_provider_registries: list[DynamicProviderRegistry] = []
        self._retired_local_backend_registries: list[LocalBackendRegistry] = []
        self._extensions: list[RegisteredExtension] = []
        self._tools: dict[str, RegisteredExtensionTool] = {}
        self._commands: dict[str, ExtensionCommand] = {}
        self._prompt_guidelines: list[tuple[str, str, str]] = []
        self._prompt_sections: list[tuple[str, str, PromptSection]] = []
        self._message_renderers: dict[str, tuple[str, str, MessageRenderer]] = {}
        self._renderer_failures_reported: set[str] = set()
        self._load_diagnostics: list[ResourceDiagnostic] = []
        self._runtime_diagnostics: list[ResourceDiagnostic] = []
        # Keep constructor-provided paths visible until ``load`` installs the
        # authoritative resource-path snapshot.
        #
        # 在 ``load`` 安装权威资源路径快照之前，保留构造函数传入的路径。
        self._paths: TauPaths = paths or TauPaths()
        self._session: BoundSession | None = None
        self._ui: UiBridge = ui or NullUiBridge()
        self._turn_requested: TurnRequestedCallback | None = None
        self._harness_unsubscribe: Callable[[], None] | None = None
        self._extension_turn_index = 0

    # -- loading -----------------------------------------------------------
    # 加载

    def load(
        self,
        paths: TauResourcePaths,
        *,
        extra_paths: Sequence[Path] = (),
        include_resource_dirs: bool = True,
        include_project_dir: bool = False,
        include_user_dir: bool = True,
    ) -> None:
        """Load built-ins, then discover extensions and run isolated setup.

        先加载内置扩展，再发现扩展并运行隔离的设置流程。
        """
        self._paths = paths.paths or TauPaths(
            home=paths.root,
            agents_home=paths.agents_root or Path.home() / ".agents",
        )
        self._load_built_ins()
        result = load_extensions(
            paths,
            extra_paths=extra_paths,
            include_resource_dirs=include_resource_dirs,
            include_project_dir=include_project_dir,
            include_user_dir=include_user_dir,
        )
        self._load_diagnostics.extend(result.diagnostics)
        for extension in result.extensions:
            self._setup_extension(extension)

    def _load_built_ins(self) -> None:
        """Load each trusted declaration once, before filesystem sources.

        在文件系统来源之前加载每个可信声明一次。
        """
        if self._built_ins_loaded:
            return
        self._built_ins_loaded = True
        for declaration in self._built_in_extensions:
            setup = declaration.setup
            if declaration.setup_with_context is not None:

                def setup_with_runtime_context(
                    api: ExtensionAPI,
                    declaration: BuiltInExtension = declaration,
                ) -> None:
                    """Run a built-in setup callback with its injected context.

                    使用注入的上下文运行内置扩展的设置回调。
                    """
                    assert declaration.setup_with_context is not None
                    declaration.setup_with_context(api, self._built_in_context)

                setup = setup_with_runtime_context
            self._setup_extension(
                LoadedExtension(
                    name=declaration.name,
                    path=None,
                    source_id=declaration.source_id,
                    setup=setup,
                    source="built-in",
                    hidden=declaration.hidden,
                )
            )

    @property
    def active(self) -> bool:
        """Return whether this runtime generation still owns live registrations.

        返回此运行时代际是否仍拥有活动注册项。
        """
        return self._generation.active

    def retire(self) -> None:
        """Invalidate this generation and release all source-owned work.

        使此代际失效并释放所有来源拥有的工作。
        """
        if not self._generation.active:
            return
        # Replacement callers clear host UI before a successor uses the shared
        # bridge; clearing here could erase that successor's freshly mounted UI.
        # Provider retirement synchronously detaches every layer and requests
        # cancellation of generation-owned provider and backend work.
        #
        # 替换调用方会先清理宿主界面，再由后继代际使用共享桥接器；若在此处清理，
        # 可能会误删后继代际刚挂载的界面。提供者退役会同步分离所有层，
        # 并请求取消此代际拥有的提供者与后端工作。
        self._local_backend_registry.retire()
        self._provider_registry.retire()
        self._generation.invalidate()
        if self._harness_unsubscribe is not None:
            self._harness_unsubscribe()
            self._harness_unsubscribe = None
        self._extensions.clear()
        self._tools.clear()
        self._commands.clear()
        self._prompt_guidelines.clear()
        self._prompt_sections.clear()
        self._message_renderers.clear()
        self._renderer_failures_reported.clear()
        self._turn_requested = None
        self._session = None

    async def aclose(self) -> ProviderRegistryCloseResult:
        """Retire this generation and report drain or bounded containment.

        退役此代际，并报告工作已排空还是仍处于有界收容状态。
        """
        self.retire()
        provider_registries = (*self._retired_provider_registries, self._provider_registry)
        backend_registries = (
            *self._retired_local_backend_registries,
            self._local_backend_registry,
        )
        try:
            _, provider_results = await asyncio.gather(
                asyncio.gather(*(registry.aclose() for registry in backend_registries)),
                asyncio.gather(*(registry.aclose() for registry in provider_registries)),
            )
            self._retired_provider_registries = [
                registry
                for registry, result in zip(provider_registries, provider_results, strict=True)
                if not result.drained and registry is not self._provider_registry
            ]
            self._retired_local_backend_registries = []
            contained = sum(result.contained_discovery_tasks for result in provider_results)
            return ProviderRegistryCloseResult(
                drained=contained == 0,
                contained_discovery_tasks=contained,
            )
        finally:
            self._generation.invalidate()
            if self._harness_unsubscribe is not None:
                self._harness_unsubscribe()
                self._harness_unsubscribe = None

    def reset_for_reload(self) -> None:
        """Drop all registrations and imported modules ahead of a re-load.

        在重新加载前清除所有注册项和已导入模块。

        Also invalidates the current extension generation (Pi's ``invalidate``
        parity): any `tau` API object, context, or ui facade captured before
        the reload — including one held by a still-running background task —
        raises :class:`ExtensionError` on its next use instead of acting
        against the fresh registration set. Session rebinding does not come
        through here and never invalidates.

        此操作也会使当前扩展代际失效（对应 Pi 的 ``invalidate``）：重新加载前捕获的
        任何 `tau` API 对象、上下文或 UI 门面（包括仍在运行的后台任务持有的对象），
        下次使用时都会抛出 :class:`ExtensionError`，而不会作用于新的注册集合。
        会话重新绑定不会经过此处，也不会使代际失效。
        """
        # Host-side extension UI (slot widgets, main views, key interceptors)
        # belongs to the outgoing generation. Tear it down while that generation
        # is still active so host cleanup triggered by component disposal can
        # safely use its API; only then make every captured API/context stale.
        #
        # 宿主侧扩展界面属于即将退出的代际。在该代际仍有效时先拆除界面，
        # 以便组件释放触发的宿主清理仍可安全调用其 API；随后再使所有已捕获的
        # API 和上下文失效。
        self.clear_ui_components()
        self._local_backend_registry.retire()
        self._retired_local_backend_registries.append(self._local_backend_registry)
        self._provider_registry.retire()
        self._retired_provider_registries.append(self._provider_registry)
        self._generation.invalidate()
        self._generation = ExtensionGeneration()
        self._built_ins_loaded = False
        self._provider_registry = DynamicProviderRegistry(
            self._durable_providers,
            generation_id=self._generation.id,
            credentials=self._provider_credentials,
            environment=self._provider_environment,
        )
        self._local_backend_registry = LocalBackendRegistry(
            self._provider_registry,
            generation_id=self._generation.id,
        )
        if self._harness_unsubscribe is not None:
            self._harness_unsubscribe()
            self._harness_unsubscribe = None
        self._extensions.clear()
        self._tools.clear()
        self._commands.clear()
        self._prompt_guidelines.clear()
        self._prompt_sections.clear()
        self._message_renderers.clear()
        self._renderer_failures_reported.clear()
        self._load_diagnostics.clear()
        self._runtime_diagnostics.clear()
        unload_extension_modules()

    def _setup_extension(self, extension: LoadedExtension) -> None:
        """Register an extension and run setup inside a failure-isolation boundary.

        注册扩展，并在隔离错误的边界内运行设置过程。
        """
        source_id = extension.source_id
        if self._extension_by_source(source_id) is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension.name,
                    path=extension.path,
                    message="duplicate extension source ignored (first-loaded wins)",
                )
            )
            return
        api = ExtensionAPI(
            self,
            extension.name,
            self._generation,
            source_id=source_id,
        )
        registered = RegisteredExtension(
            name=extension.name,
            source_id=source_id,
            path=extension.path,
            api=api,
            source=extension.source,
            hidden=extension.hidden,
        )
        self._extensions.append(registered)
        try:
            extension.setup(api)
        except Exception as exc:  # noqa: BLE001 - extensions are an isolation boundary

            # BLE001：扩展是异常隔离边界。
            self._extensions.remove(registered)
            self._remove_registrations(source_id)
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension.name,
                    path=extension.path,
                    message=(
                        f"built-in setup failed: {exc!r}"
                        if extension.source == "built-in"
                        else f"setup failed: {exc!r}"
                    ),
                    severity="error",
                )
            )

    def _remove_registrations(self, source_id: str) -> None:
        """Remove all registrations owned by one extension source.

        移除一个扩展来源拥有的所有注册项。
        """
        self._tools = {
            name: registration
            for name, registration in self._tools.items()
            if registration.source_id != source_id
        }
        self._commands = {
            name: command
            for name, command in self._commands.items()
            if command.source_id != source_id
        }
        self._prompt_guidelines = [
            (owner, extension, guideline)
            for owner, extension, guideline in self._prompt_guidelines
            if owner != source_id
        ]
        self._prompt_sections = [
            (owner, extension, section)
            for owner, extension, section in self._prompt_sections
            if owner != source_id
        ]
        self._message_renderers = {
            custom_type: registration
            for custom_type, registration in self._message_renderers.items()
            if registration[0] != source_id
        }
        self._provider_registry.unregister_source(source_id)
        self._local_backend_registry.unregister_source(source_id)

    # -- registration (called through ExtensionAPI) -------------------------
    # 注册（通过 ExtensionAPI 调用）

    def register_provider(self, source_id: str, provider: DynamicProvider) -> None:
        """Register or atomically replace one exact extension source layer.

        注册或原子替换一个精确扩展来源层。
        """
        self._provider_registry.register(source_id, provider)

    def update_provider(self, source_id: str, provider: DynamicProvider) -> bool:
        """Publish a provider snapshot without invalidating paired backends.

        发布提供商快照，而不使配对后端失效。
        """
        return self._provider_registry.update(source_id, provider)

    def register_local_backend(self, source_id: str, backend: LocalBackend) -> None:
        """Register a backend against its exact source-owned provider layer.

        针对来源拥有的精确提供商层注册后端。
        """
        self._local_backend_registry.register(source_id, backend)

    def register_tool(self, source_id: str, extension_name: str, tool: AgentTool) -> None:
        """Register an extension tool; first registration per name wins.

        注册扩展工具；每个名称的首次注册生效。
        """
        existing = self._tools.get(tool.name)
        if existing is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message=(
                        f"tool `{tool.name}` already registered by extension"
                        f" `{existing.extension}`; ignoring duplicate"
                    ),
                )
            )
            return
        self._tools[tool.name] = RegisteredExtensionTool(
            extension=extension_name,
            source_id=source_id,
            tool=tool,
        )

    def register_command(
        self,
        source_id: str,
        extension_name: str,
        name: str,
        handler: ExtensionCommandHandler,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        """Register an extension slash command; first registration wins.

        注册扩展斜杠命令；首次注册生效。
        """
        normalized = name.strip().removeprefix("/").lower()
        existing = self._commands.get(normalized)
        if existing is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message=(
                        f"command `/{normalized}` already registered by extension"
                        f" `{existing.extension}`; ignoring duplicate"
                    ),
                )
            )
            return
        self._commands[normalized] = ExtensionCommand(
            extension=extension_name,
            source_id=source_id,
            name=normalized,
            description=description,
            usage=usage or f"/{normalized}",
            aliases=aliases,
            handler=handler,
        )

    def register_message_renderer(
        self,
        source_id: str,
        extension_name: str,
        custom_type: str,
        renderer: MessageRenderer,
    ) -> None:
        """Register a custom-message renderer; first registration per type wins.

        注册自定义消息渲染器；每种类型的首次注册生效。
        """
        normalized = custom_type.strip()
        if not normalized:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="empty custom_type for message renderer ignored",
                )
            )
            return
        existing = self._message_renderers.get(normalized)
        if existing is not None:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message=(
                        f"message renderer for `{normalized}` already registered by"
                        f" extension `{existing[1]}`; ignoring duplicate"
                    ),
                )
            )
            return
        self._message_renderers[normalized] = (source_id, extension_name, renderer)

    def render_custom_message(
        self,
        custom_type: str,
        content: str,
        details: Mapping[str, JSONValue] | None,
        expanded: bool,
    ) -> str | None:
        """Render a custom message to markup, or ``None`` to fall back to raw text.

        将自定义消息渲染为标记文本；若无法渲染则返回 ``None``。

        Installed into every render path (TUI state, print transcript). A missing
        renderer or a renderer that raises or returns a non-string yields
        ``None`` so the frontend renders the raw ``content`` instead of crashing.
        Failures are diagnosed once per ``custom_type`` (render paths re-run on
        every redraw, which would otherwise grow diagnostics without bound).

        此方法安装在所有渲染路径中（TUI 状态和打印记录）。如果没有对应渲染器，
        或渲染器抛出异常、返回非字符串，则返回 ``None``，由前端显示原始 ``content``，
        避免渲染失败导致崩溃。每种 ``custom_type`` 只记录一次失败诊断；渲染路径会在
        每次重绘时重新执行，否则诊断数量会无限增长。
        """
        registration = self._message_renderers.get(custom_type)
        if registration is None:
            return None
        _, extension_name, renderer = registration
        view = CustomMessageView(custom_type=custom_type, content=content, details=details)
        options = MessageRenderOptions(expanded=expanded)
        try:
            markup = renderer(view, options)
        except Exception as exc:  # noqa: BLE001 - a renderer must never crash the frontend

            # BLE001：渲染器绝不能导致前端崩溃。
            if custom_type not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(custom_type)
                self._record_runtime_failure(extension_name, f"message_renderer:{custom_type}", exc)
            return None
        if not isinstance(markup, str):
            if custom_type not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(custom_type)
                self._record_bad_result(extension_name, f"message_renderer:{custom_type}", markup)
            return None
        return markup

    def render_tool_call(
        self,
        name: str,
        arguments: Mapping[str, JSONValue],
    ) -> str | None:
        """Render a tool call via its tool's `render_call`, or ``None``.

        通过工具的 `render_call` 渲染工具调用；无法渲染时返回 ``None``。

        Installed into frontends as the tool-call display resolver. A tool
        without a `render_call`, or a renderer that raises or returns a
        non-string, yields ``None`` so the frontend falls back to its
        generic invocation formatting. Failures are diagnosed once per tool
        name (render paths re-run on every redraw).

        此方法作为前端的工具调用显示解析器。工具没有 `render_call`，或渲染器抛出异常、
        返回非字符串时，返回 ``None``，由前端回退到通用调用格式。每个工具名称只记录
        一次失败诊断，因为渲染路径会在每次重绘时重新执行。
        """
        registered = self._tools.get(name)
        if registered is None or registered.tool.render_call is None:
            return None
        try:
            line = registered.tool.render_call(arguments)
        except Exception as exc:  # noqa: BLE001 - a renderer must never crash the frontend

            # BLE001：渲染器绝不能导致前端崩溃。
            if name not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(name)
                self._record_runtime_failure(registered.extension, f"render_call:{name}", exc)
            return None
        if line is not None and not isinstance(line, str):
            if name not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(name)
                self._record_bad_result(registered.extension, f"render_call:{name}", line)
            return None
        return line

    def render_tool_result(
        self,
        tool_name: str,
        result: AgentToolResult,
        expanded: bool,
    ) -> str | None:
        """Render a named tool's result via `render_result`, or ``None``.

        通过指定工具的 `render_result` 渲染结果；无法渲染时返回 ``None``。

        Installed into frontends as the tool-result display resolver, the
        counterpart of `render_tool_call` for the other end of the row's
        lifecycle. A tool without a `render_result`, or a renderer that raises
        or returns a non-string, yields ``None`` so the frontend falls back to
        its generic result formatting. Failures are diagnosed once per tool
        name (render paths re-run on every redraw).

        此方法作为前端的工具结果显示解析器，对应于该行生命周期另一端的
        `render_tool_call`。工具没有 `render_result`，或渲染器抛出异常、返回非字符串时，
        返回 ``None``，由前端回退到通用结果格式。每个工具名称只记录一次失败诊断，
        因为渲染路径会在每次重绘时重新执行。
        """
        registered = self._tools.get(tool_name)
        if registered is None or registered.tool.render_result is None:
            return None
        failure_key = f"render_result:{tool_name}"
        try:
            markup = registered.tool.render_result(result, expanded=expanded)
        except Exception as exc:  # noqa: BLE001 - a renderer must never crash the frontend

            # BLE001：渲染器绝不能导致前端崩溃。
            if failure_key not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(failure_key)
                self._record_runtime_failure(
                    registered.extension, f"render_result:{tool_name}", exc
                )
            return None
        if markup is not None and not isinstance(markup, str):
            if failure_key not in self._renderer_failures_reported:
                self._renderer_failures_reported.add(failure_key)
                self._record_bad_result(registered.extension, f"render_result:{tool_name}", markup)
            return None
        return markup

    def register_prompt_guideline(
        self, source_id: str, extension_name: str, guideline: str
    ) -> None:
        """Register a standalone system-prompt guideline line.

        注册独立的系统提示词准则行。
        """
        normalized = guideline.strip()
        if not normalized:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="empty prompt guideline ignored",
                )
            )
            return
        self._prompt_guidelines.append((source_id, extension_name, normalized))

    def register_prompt_section(
        self,
        source_id: str,
        extension_name: str,
        title: str | None,
        body: str,
    ) -> None:
        """Register a free-form system-prompt section.

        注册自由格式的系统提示词区块。
        """
        normalized_body = body.strip()
        if not normalized_body:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="empty prompt section ignored",
                )
            )
            return
        normalized_title = title.strip() if title is not None else None
        if normalized_title == "":
            normalized_title = None
        if normalized_title is not None and any(
            separator in normalized_title for separator in ("\r", "\n")
        ):
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=extension_name,
                    message="prompt section ignored because its title spans multiple lines",
                )
            )
            return
        self._prompt_sections.append(
            (
                source_id,
                extension_name,
                PromptSection(title=normalized_title, body=normalized_body),
            )
        )

    def subscribe(self, source_id: str, event: str, handler: ExtensionHandler) -> None:
        """Subscribe an extension handler to a named event.

        将扩展处理器订阅到命名事件。
        """
        known = (
            event in AGENT_EVENT_TYPES
            or event in LIFECYCLE_EVENT_TYPES
            or event == AGENT_EVENT_WILDCARD
        )
        if not known:
            self._load_diagnostics.append(
                ResourceDiagnostic(
                    kind="extension",
                    name=self._extension_display_name(source_id),
                    message=f"unknown event `{event}`; handler ignored",
                )
            )
            return
        extension = self._extension_by_source(source_id)
        if extension is None:
            raise ExtensionError(f"unknown extension source: {source_id}")
        extension.handlers.setdefault(event, []).append(handler)

    # -- binding -------------------------------------------------------------
    # 会话绑定

    def bind(self, session: BoundSession) -> None:
        """Bind (or re-bind) the runtime to a coding session.

        将运行时绑定或重新绑定到编码会话。
        """
        self._session = session

    def attach_harness_listener(
        self,
        subscribe: Callable[[Callable[[AgentEvent], Awaitable[None] | None]], Callable[[], None]],
    ) -> None:
        """Subscribe the event fan-out to a harness, replacing any prior one.

        将事件扇出订阅到 harness，并替换任何先前订阅。
        """
        if self._harness_unsubscribe is not None:
            self._harness_unsubscribe()
        self._harness_unsubscribe = subscribe(self._on_agent_event)

    @property
    def provider_credentials(self) -> CredentialReader | None:
        """Return the read-only credential reader used by dynamic runtimes.

        返回动态运行时使用的只读凭据读取器。
        """
        return self._provider_credentials

    @property
    def provider_environment(self) -> Mapping[str, str] | None:
        """Return the environment snapshot used by dynamic provider auth.

        返回动态提供者认证使用的环境变量快照。
        """
        return self._provider_environment

    @property
    def built_in_credentials(self) -> CredentialStore:
        """Return the injected mutable store used by trusted built-ins.

        返回注入给可信内置扩展的可变凭据存储。
        """
        return self._built_in_context.credential_store

    @property
    def built_in_http_client(self) -> httpx.AsyncClient | None:
        """Return the externally owned HTTP client used by trusted built-ins.

        返回可信内置扩展使用的外部管理 HTTP 客户端。
        """
        return self._built_in_context.http_client

    def set_ui_bridge(self, ui: UiBridge) -> None:
        """Install the frontend UI bridge (TUI, print-mode fallback, or test).

        安装前端 UI 桥接器（TUI、打印模式回退实现或测试实现）。
        """
        self._ui = ui

    def clear_ui_components(self) -> None:
        """Ask the host frontend to tear down all extension-owned UI.

        请求宿主前端拆除所有扩展拥有的 UI。

        Invoked on `/reload` (via ``reset_for_reload``) and by session
        replacement flows (resume/new) before ``session_start`` fires, so
        widgets and key interceptors never outlive the world that mounted
        them while handlers keep the chance to re-mount.

        此方法会在 `/reload`（通过 ``reset_for_reload``）以及会话替换流程中调用，
        并且早于 `session_start` 事件执行。这样可避免控件和按键拦截器在其所属界面
        生命周期结束后继续存在，同时仍允许处理器重新挂载它们。
        """
        self._ui.clear_components()

    def set_turn_requested_callback(self, callback: TurnRequestedCallback | None) -> None:
        """Install the host callback used to deliver messages while idle.

        安装会话空闲时用于投递消息的宿主回调。

        The callback receives the message content plus optional custom-message
        metadata and is expected to submit it through the host's serialized run
        path (the TUI uses the same exclusive worker as user submissions, so
        extension turns cannot race user runs).

        回调接收消息内容以及可选的自定义消息元数据，并应通过宿主串行运行路径提交消息。
        TUI 使用与用户提交相同的独占工作器，确保扩展触发的轮次不会与用户运行竞争。
        """
        self._turn_requested = callback

    @property
    def ui(self) -> UiBridge:
        """Return the active UI bridge.

        返回活动 UI 桥接器。
        """
        return self._ui

    @property
    def session_view(self) -> BoundSession:
        """Return the bound session, raising if the runtime is unbound.

        返回已绑定会话；运行时未绑定时抛出异常。
        """
        if self._session is None:
            raise ExtensionError(
                "extension API used before the session was bound; "
                "register handlers in setup() and act on events instead"
            )
        return self._session

    @property
    def provider_registry(self) -> DynamicProviderRegistry:
        """Return this staged runtime generation's process-local provider registry.

        返回此暂存运行时代际的进程内提供者注册表。
        """
        return self._provider_registry

    @property
    def local_backend_registry(self) -> LocalBackendRegistry:
        """Return this staged runtime generation's local-backend registry.

        返回此暂存运行时代际的本地后端注册表。
        """
        return self._local_backend_registry

    @property
    def paths(self) -> TauPaths:
        """Return the resolved Tau filesystem paths for this runtime.

        返回此运行时解析后的 Tau 文件系统路径。
        """
        return self._paths

    @property
    def extension_names(self) -> tuple[str, ...]:
        """Return visible extension names in load order.

        按加载顺序返回可见扩展名称。
        """
        return tuple(extension.name for extension in self._extensions if not extension.hidden)

    @property
    def extension_metadata(self) -> tuple[ExtensionSourceMetadata, ...]:
        """Return active visible and hidden source metadata in load order.

        按加载顺序返回活动的可见及隐藏来源元数据。
        """
        return tuple(
            ExtensionSourceMetadata(
                name=extension.name,
                source_id=extension.source_id,
                source=extension.source,
                hidden=extension.hidden,
                path=extension.path,
            )
            for extension in self._extensions
        )

    @property
    def diagnostics(self) -> tuple[ResourceDiagnostic, ...]:
        """Return load-time, handler, and provider-refresh diagnostics.

        返回加载阶段、事件处理器和提供者刷新的诊断信息。
        """
        provider_diagnostics = tuple(
            ResourceDiagnostic(
                kind="provider",
                name=diagnostic.token.source_id,
                message=(
                    f"{diagnostic.message} for provider "
                    f"`{diagnostic.token.provider_id}` ({diagnostic.reason})"
                ),
            )
            for diagnostic in self._provider_registry.diagnostics
        )
        return (
            tuple(self._load_diagnostics) + tuple(self._runtime_diagnostics) + provider_diagnostics
        )

    @property
    def extension_tools(self) -> tuple[AgentTool, ...]:
        """Return extension-registered tools in registration order.

        按注册顺序返回扩展注册的工具。
        """
        return tuple(registration.tool for registration in self._tools.values())

    @property
    def extension_tool_sources(self) -> dict[str, str]:
        """Map extension-registered tool names to their owning extension.

        返回扩展注册工具名称到所属扩展的映射。
        """
        return {name: registration.extension for name, registration in self._tools.items()}

    @property
    def prompt_guidelines(self) -> tuple[str, ...]:
        """Return standalone guideline lines in registration order.

        按注册顺序返回独立的提示词准则行。
        """
        return tuple(guideline for _, _, guideline in self._prompt_guidelines)

    @property
    def prompt_sections(self) -> tuple[PromptSection, ...]:
        """Return free-form prompt sections in registration order.

        按注册顺序返回自由格式的提示词区块。
        """
        return tuple(section for _, _, section in self._prompt_sections)

    @property
    def sourced_prompt_sections(self) -> tuple[PromptSection, ...]:
        """Return prompt sections annotated with their owning extension.

        返回标注了所属扩展的提示词区块。
        """
        return tuple(
            PromptSection(
                title=section.title,
                body=section.body,
                source=f"extension: {extension}",
            )
            for _owner, extension, section in self._prompt_sections
        )

    # -- actions (called through ExtensionAPI) --------------------------------
    # 操作（通过 ExtensionAPI 调用）

    def send_user_message(self, content: str, *, deliver_as: str = "follow_up") -> None:
        """Deliver a user message into the active run, or start one when idle.

        将用户消息传入活动运行，空闲时则启动一次运行。
        """
        self._deliver_message(content, deliver_as=deliver_as, trigger_turn=True)

    def send_custom_message(
        self,
        content: str,
        *,
        custom_type: str,
        details: dict[str, JSONValue] | None = None,
        deliver_as: str = "follow_up",
        trigger_turn: bool = True,
    ) -> None:
        """Deliver a custom message carrying render metadata through the pipeline.

        将携带渲染元数据的自定义消息传递到整个处理流程。
        """
        self._deliver_message(
            content,
            deliver_as=deliver_as,
            trigger_turn=trigger_turn,
            custom_type=custom_type,
            details=details,
        )

    def _deliver_message(
        self,
        content: str,
        *,
        deliver_as: str,
        trigger_turn: bool,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Queue a message for the active run or request a new turn when idle.

        为当前运行排队消息；会话空闲时则按需请求启动新轮次。
        """
        session = self.session_view
        if session.is_running:
            if deliver_as == "steer":
                session.queue_steering_message(content, custom_type=custom_type, details=details)
            else:
                session.queue_follow_up_message(content, custom_type=custom_type, details=details)
            return
        if trigger_turn and self._turn_requested is not None:
            self._turn_requested(content, custom_type, details)
            return
        # No host run-path registered (print mode, tests) or trigger_turn=False:
        # queue for whichever run happens next.
        #
        # 如果未注册宿主运行路径（例如打印模式或测试），或 ``trigger_turn=False``，
        # 则将消息排队，留待下一次运行处理。
        session.queue_follow_up_message(content, custom_type=custom_type, details=details)

    async def append_custom_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:
        """Persist a `CustomEntry` through the bound session.

        通过已绑定会话持久化 `CustomEntry`。
        """
        await self.session_view.append_custom_entry(namespace, data)

    async def set_label(self, target_id: str, label: str | None) -> None:
        """Set or clear a per-entry session bookmark through the bound session.

        通过已绑定的会话为单条记录设置或清除书签。
        """
        await self.session_view.set_label(target_id, label)

    # -- tools ----------------------------------------------------------------
    # 工具

    def compose_tools(self, builtin_tools: Sequence[AgentTool]) -> list[AgentTool]:
        """Merge built-in and extension tools, then wrap all with hook seams.

        合并内置工具与扩展工具，再为所有工具包装钩子接口。

        Extension tools override built-ins with the same name in place;
        extension-only tools append in registration order.

        同名扩展工具会在原位置覆盖内置工具；仅由扩展提供的工具则按注册顺序追加。
        """
        merged: list[AgentTool] = []
        extension_tools = dict(self._tools)
        for tool in builtin_tools:
            override = extension_tools.pop(tool.name, None)
            merged.append(override.tool if override is not None else tool)
        merged.extend(registration.tool for registration in extension_tools.values())
        return [self._wrap_tool(tool) for tool in merged]

    def _wrap_tool(self, tool: AgentTool) -> AgentTool:
        """Wrap a tool so extension hooks can inspect and transform its calls.

        包装工具，以便扩展钩子检查并转换工具调用。
        """
        async def executor(
            tool_call_id: str,
            arguments: Mapping[str, JSONValue],
            signal: ToolCancellationToken | None = None,
            on_update: ToolUpdateCallback | None = None,
        ) -> AgentToolResult:
            """Run call hooks, execute the tool, then run result hooks.

            依次运行调用钩子、执行工具并运行结果钩子。
            """
            call_outcome = await self._run_tool_call_hooks(tool.name, arguments)
            if call_outcome.block:
                reason = call_outcome.reason or "blocked by an extension"
                return AgentToolResult(content=[TextContent(text=f"Tool call blocked: {reason}")])
            effective_arguments = (
                call_outcome.arguments if call_outcome.arguments is not None else arguments
            )
            result = await tool.execute(
                tool_call_id,
                effective_arguments,
                signal=signal,
                on_update=on_update,
            )
            return await self._run_tool_result_hooks(tool.name, effective_arguments, result)

        return AgentTool(
            name=tool.name,
            label=tool.label,
            description=tool.description,
            parameters=tool.parameters,
            execute_fn=executor,
            prompt_snippet=tool.prompt_snippet,
            prompt_guidelines=tool.prompt_guidelines,
            prepare_arguments=tool.prepare_arguments,
            execution_mode=tool.execution_mode,
            render_call=tool.render_call,
            render_result=tool.render_result,
        )

    async def _run_tool_call_hooks(
        self,
        tool_name: str,
        arguments: Mapping[str, JSONValue],
    ) -> ToolCallHookResult:
        """Apply tool-call hooks in order, stopping when one blocks the call.

        按顺序应用工具调用钩子，并在任一钩子阻止调用时停止。
        """
        effective: Mapping[str, JSONValue] = arguments
        for owner, handler in self._handlers_for("tool_call"):
            event = ToolCallHookEvent(tool_name=tool_name, arguments=effective)
            try:
                result = await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:  # noqa: BLE001 - fail-safe: an error blocks the tool

                # 安全失败处理：发生错误时阻止工具调用。
                self._record_runtime_failure(owner.name, "tool_call", exc)
                return ToolCallHookResult(
                    block=True,
                    reason=f"extension `{owner.name}` tool_call hook failed: {exc}",
                )
            if result is None:
                continue
            if not isinstance(result, ToolCallHookResult):
                self._record_bad_result(owner.name, "tool_call", result)
                continue
            if result.block:
                return ToolCallHookResult(block=True, reason=result.reason)
            if result.arguments is not None:
                effective = result.arguments
        if effective is arguments:
            return ToolCallHookResult()
        return ToolCallHookResult(arguments=effective)

    async def _run_tool_result_hooks(
        self,
        tool_name: str,
        arguments: Mapping[str, JSONValue],
        result: AgentToolResult,
    ) -> AgentToolResult:
        """Apply observational result hooks and combine their supported updates.

        应用观察型结果钩子，并合并其中受支持的更新。
        """
        current = result
        for owner, handler in self._handlers_for("tool_result"):
            event = ToolResultHookEvent(tool_name=tool_name, arguments=arguments, result=current)
            try:
                outcome = await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:  # noqa: BLE001 - result hooks are observational-ish

                # 结果钩子主要用于观察，因此失败时继续处理。
                self._record_runtime_failure(owner.name, "tool_result", exc)
                continue
            if outcome is None:
                continue
            if not isinstance(outcome, ToolResultHookResult):
                self._record_bad_result(owner.name, "tool_result", outcome)
                continue
            updates: dict[str, object] = {}
            if outcome.content is not None:
                updates["content"] = [TextContent(text=outcome.content)]
            if outcome.details is not None:
                updates["details"] = outcome.details
            if updates:
                current = current.model_copy(update=updates)
        return current

    # -- commands ---------------------------------------------------------------
    # 命令

    def build_command_registry(self) -> CommandRegistry:
        """Build a session command registry: defaults plus extension commands.

        构建会话命令注册表，其中包含默认命令和扩展命令。
        """
        registry = create_default_command_registry()
        for command in self._commands.values():
            slash_command = SlashCommand(
                name=command.name,
                description=command.description or f"Extension command ({command.extension}).",
                usage=command.usage,
                handler=self._command_handler(command),
                aliases=command.aliases,
                search_terms=(command.extension, "extension"),
            )
            try:
                registry.register(slash_command)
            except ValueError as exc:
                self._load_diagnostics.append(
                    ResourceDiagnostic(
                        kind="extension",
                        name=command.extension,
                        message=f"could not register command `/{command.name}`: {exc}",
                    )
                )
        return registry

    def _command_handler(
        self, command: ExtensionCommand
    ) -> Callable[[CommandContext], CommandResult]:
        """Adapt an extension command callback to the host command interface.

        将扩展命令回调适配为宿主命令接口。
        """
        def handler(context: CommandContext) -> CommandResult:
            """Invoke the extension callback and convert failures to command output.

            调用扩展回调，并将错误转换为命令输出。
            """
            extension_context = ExtensionCommandContext(
                name=command.name,
                args=context.args,
                api=self._api_for(command.source_id),
            )
            try:
                message = command.handler(context.args, extension_context)
            except Exception as exc:  # noqa: BLE001 - extensions are an isolation boundary

                # BLE001：扩展是异常隔离边界。
                self._record_runtime_failure(command.extension, f"command:/{command.name}", exc)
                return CommandResult(
                    handled=True,
                    message=f"Extension command /{command.name} failed: {exc}",
                )
            return CommandResult(handled=True, message=message)

        return handler

    # -- event dispatch -----------------------------------------------------------
    # 事件分发

    async def decide_project_trust(self, event: ProjectTrustEvent) -> ExtensionTrustResult | None:
        """Return the first decisive eligible extension trust result.

        返回第一个符合条件且给出明确决策的扩展信任结果。

        This runtime must contain only built-in, user, and explicit extensions;
        callers load project extensions only after this method resolves.

        此运行时只能包含内置、用户级和显式指定的扩展；调用方必须等待此方法完成后，
        才能加载项目级扩展。
        """
        for owner, handler in self._handlers_for("project_trust"):
            try:
                result = await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:  # noqa: BLE001 - trust handlers fail closed/defer

                # 信任处理器发生错误时安全关闭或延后决策。
                self._record_runtime_failure(owner.name, "project_trust", exc)
                continue
            if result is None:
                continue
            if not isinstance(result, ExtensionTrustResult):
                self._record_bad_result(owner.name, "project_trust", result)
                continue
            if result.decision != "defer":
                return result
        return None

    async def emit_session_start(self, reason: SessionLifecycleReason) -> None:
        """Dispatch `session_start` to subscribed extensions.

        向已订阅的扩展分发 `session_start` 事件。
        """
        await self._emit_lifecycle("session_start", SessionStartEvent(reason=reason))

    async def emit_session_shutdown(self, reason: SessionLifecycleReason) -> None:
        """Dispatch `session_shutdown` to subscribed extensions.

        向已订阅的扩展分发 `session_shutdown` 事件。
        """
        await self._emit_lifecycle("session_shutdown", SessionShutdownEvent(reason=reason))

    async def run_input_hooks(
        self,
        text: str,
        *,
        source: Literal["interactive", "extension"] = "interactive",
        streaming_behavior: Literal["steer", "follow_up"] | None = None,
    ) -> InputHookOutcome:
        """Run `input` hooks over prompt text; transforms chain, handled wins.

        对提示文本运行 `input` 钩子；转换结果依次传递，处理完成的结果优先。

        `source`/`streaming_behavior` are surfaced to handlers on the
        `InputEvent` payload; they do not change chaining semantics.

        `source` 和 `streaming_behavior` 会通过 `InputEvent` 负载提供给处理器，
        但不会改变钩子的串联规则。
        """
        current = text
        for owner, handler in self._handlers_for("input"):
            try:
                result = await _resolve(
                    handler(
                        InputEvent(
                            text=current,
                            source=source,
                            streaming_behavior=streaming_behavior,
                        ),
                        self._fresh_context(owner.source_id),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - extensions are an isolation boundary

                # BLE001：扩展是异常隔离边界。
                self._record_runtime_failure(owner.name, "input", exc)
                continue
            if result is None:
                continue
            if not isinstance(result, InputHookResult):
                self._record_bad_result(owner.name, "input", result)
                continue
            if result.action == "handled":
                return InputHookOutcome(handled=True, text=current, message=result.message)
            if result.action == "transform" and result.text is not None:
                current = result.text
        return InputHookOutcome(handled=False, text=current)

    async def emit_event(self, event: object) -> None:
        """Dispatch one canonical agent or coding-session event to extensions.

        向扩展分发一个规范的代理事件或编码会话事件。
        """
        event_type = getattr(event, "type", None)
        if not isinstance(event_type, str):
            raise TypeError("Extension events must expose a string type")
        handlers = list(self._handlers_for(event_type))
        handlers.extend(self._handlers_for(AGENT_EVENT_WILDCARD))
        for owner, handler in handlers:
            try:
                await _resolve(handler(event, self._fresh_context(owner.source_id)))
            except Exception as exc:  # noqa: BLE001 - extensions are an isolation boundary

                # BLE001：扩展是异常隔离边界。
                self._record_runtime_failure(owner.name, event_type, exc)

    async def _on_agent_event(self, event: AgentEvent) -> None:
        """Adapt core turn events to Pi's extension-facing session metadata.

        将核心轮次事件转换为 Pi 扩展接口使用的会话元数据。
        """
        extension_event: object = event
        if isinstance(event, AgentStartEvent):
            self._extension_turn_index = 0
        elif isinstance(event, AgentTurnStartEvent):
            extension_event = TurnStartEvent(
                turn_index=self._extension_turn_index,
                timestamp=time_ns() // 1_000_000,
            )
        elif isinstance(event, AgentTurnEndEvent):
            extension_event = TurnEndEvent(
                turn_index=self._extension_turn_index,
                message=event.message,
                tool_results=list(event.tool_results),
            )
        await self.emit_event(extension_event)
        if isinstance(event, AgentTurnEndEvent):
            self._extension_turn_index += 1

    async def _emit_lifecycle(self, event_name: str, payload: object) -> None:
        """Dispatch one lifecycle payload to each subscribed extension handler.

        将一个生命周期负载分发给每个已订阅的扩展处理器。
        """
        for owner, handler in self._handlers_for(event_name):
            try:
                await _resolve(handler(payload, self._fresh_context(owner.source_id)))
            except Exception as exc:  # noqa: BLE001 - extensions are an isolation boundary

                # BLE001：扩展是异常隔离边界。
                self._record_runtime_failure(owner.name, event_name, exc)

    # -- internals -------------------------------------------------------------
    # 内部方法

    def _handlers_for(self, event: str) -> Iterator[tuple[RegisteredExtension, ExtensionHandler]]:
        """Yield handlers registered for an event in extension load order.

        按扩展加载顺序生成针对指定事件注册的处理器。
        """
        for extension in self._extensions:
            for handler in extension.handlers.get(event, ()):
                yield extension, handler

    def _extension_by_source(self, source_id: str) -> RegisteredExtension | None:
        """Find a loaded extension by its stable source identifier.

        按稳定来源标识符查找已加载的扩展。
        """
        for extension in self._extensions:
            if extension.source_id == source_id:
                return extension
        return None

    def _extension_display_name(self, source_id: str) -> str:
        """Return the extension name for a source, falling back to its ID.

        返回来源对应的扩展名称；未找到时回退为来源 ID。
        """
        extension = self._extension_by_source(source_id)
        return extension.name if extension is not None else source_id

    def _fresh_context(self, source_id: str) -> ExtensionContext:
        """Return a fresh context for one handler invocation.

        为一次处理器调用创建新的上下文。
        """
        api = self._api_for(source_id)
        return ExtensionContext(
            self,
            api._generation,
            extension_name=self._extension_display_name(source_id),
        )

    def _api_for(self, source_id: str) -> ExtensionAPI:
        """Return the API owned by a source or raise for an unknown source.

        返回来源拥有的 API；来源未知时抛出异常。
        """
        extension = self._extension_by_source(source_id)
        if extension is None:
            raise ExtensionError(f"unknown extension source: {source_id}")
        return extension.api

    def record_ui_failure(self, extension: str, context: str, exc: BaseException) -> None:
        """Record a host-isolated extension UI failure in session diagnostics.

        将宿主隔离的扩展 UI 故障记录到会话诊断信息中。
        """
        self._runtime_diagnostics.append(
            ResourceDiagnostic(
                kind="extension",
                name=extension,
                message=f"UI component `{context}` failed: {exc!r}",
                severity="error",
            )
        )

    def _record_runtime_failure(self, extension: str, event: str, exc: Exception) -> None:
        """Append a diagnostic for an exception raised by an extension handler.

        为扩展处理器引发的异常追加诊断信息。
        """
        self._runtime_diagnostics.append(
            ResourceDiagnostic(
                kind="extension",
                name=extension,
                message=f"handler for `{event}` raised: {exc!r}",
                severity="error",
            )
        )

    def _record_bad_result(self, extension: str, event: str, result: object) -> None:
        """Append a diagnostic when a handler returns an unsupported result.

        处理器返回不受支持的结果时追加诊断信息。
        """
        self._runtime_diagnostics.append(
            ResourceDiagnostic(
                kind="extension",
                name=extension,
                message=(
                    f"handler for `{event}` returned unsupported"
                    f" result type {type(result).__name__}; ignored"
                ),
            )
        )


async def _resolve(value: object) -> object:
    """Await an awaitable handler result while passing through plain values.

    对可等待的处理器结果执行 await，普通值则直接返回。
    """
    if isawaitable(value):
        return await value
    return value
