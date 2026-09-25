"""Persistent coding-session wrapper built on AgentHarness.

基于 AgentHarness 构建的持久化编码会话包装器。
"""

from __future__ import annotations

import asyncio
import string
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from os import environ
from pathlib import Path
from typing import Literal

from tau_agent.events import AgentEndEvent, AgentEvent, MessageEndEvent, ToolExecutionEndEvent
from tau_agent.harness import AgentHarness, AgentHarnessConfig, QueuedMessages
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    Usage,
    UserMessage,
    message_text,
    sum_usage,
)
from tau_agent.provider import ModelProvider
from tau_agent.provider_events import AssistantDoneEvent, AssistantErrorEvent, TextDeltaEvent
from tau_agent.session import (
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    CustomMessageEntry,
    JsonlSessionStorage,
    LabelEntry,
    MessageEntry,
    ModelChangeEntry,
    SessionInfoEntry,
    SessionState,
    SessionStorage,
    ThinkingLevelChangeEntry,
)
from tau_agent.session.entries import SessionEntry
from tau_agent.session.jsonl import entry_to_json_line
from tau_agent.session.tree import SessionTreeError, path_to_entry
from tau_agent.tool_history import ToolHistoryRepair, repair_tool_history
from tau_agent.tools import AgentTool
from tau_agent.types import JSONValue
from tau_ai.model_catalog import ModelCatalogProvider, RuntimeModel, RuntimeModelCatalog
from tau_ai.model_limits import ModelLimitsProvider, RuntimeModelLimits
from tau_coding.branch_summary import summarize_branch_messages_with_model
from tau_coding.codex_model_store import (
    cached_codex_model_catalog,
    save_codex_model_catalog,
)
from tau_coding.commands import CommandRegistry, CommandResult, create_default_command_registry
from tau_coding.context import discover_project_context_with_diagnostics
from tau_coding.context_window import (
    DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    SUMMARIZATION_SYSTEM_PROMPT,
    ContextUsageEstimate,
    auto_compaction_threshold_for_context_window,
    build_compaction_summary_prompt,
    estimate_context_usage,
    estimate_message_tokens,
    summarize_messages_for_compaction,
)
from tau_coding.credentials import FileCredentialStore, credentials_path
from tau_coding.diagnostics import (
    AgentCallDiagnosticContext,
    AgentCallDiagnosticLogger,
    new_agent_call_run_id,
)
from tau_coding.events import (
    AgentSettledEvent,
    AutoRetryEndEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    QueueUpdateEvent,
    SessionAgentEndEvent,
    SessionInfoChangedEvent,
    ThinkingLevelChangedEvent,
)
from tau_coding.extensions.provider_registry import DynamicProviderRegistry
from tau_coding.extensions.providers import DynamicProvider, ProviderModel
from tau_coding.extensions.runtime import ExtensionRuntime
from tau_coding.models_dev_store import ModelsDevRefreshResult, refresh_models_dev_catalog
from tau_coding.oauth import account_id_from_access_token
from tau_coding.paths import TauPaths
from tau_coding.project_trust import (
    CanonicalProjectPath,
    ProjectTrustCoordinator,
    ProjectTrustResolution,
    ProjectTrustStore,
    TrustDefault,
    TrustOverride,
    TrustPrompt,
    format_trust_diagnostic,
)
from tau_coding.prompt_templates import (
    PromptTemplate,
    expand_prompt_template_command,
    load_prompt_templates_with_diagnostics,
)
from tau_coding.provider_config import (
    OpenAICodexProviderConfig,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    ProviderModelMetadata,
    ProviderSettings,
    load_provider_settings,
    provider_default_thinking_level,
    provider_has_usable_credentials,
    provider_model_supports_images,
    provider_thinking_levels,
    provider_thinking_unavailable_reason,
    resolve_provider_selection,
    resolve_startup_thinking_level,
    save_default_provider_model,
    save_provider_thinking_level,
    toggle_saved_scoped_model,
    toggle_saved_stable_scoped_model,
    validate_huggingface_inference_provider,
    validate_provider_model,
)
from tau_coding.provider_runtime import (
    ClosableModelProvider,
    create_dynamic_model_provider,
    create_model_provider,
)
from tau_coding.reload import CodingReloadSummary, ReloadCategorySummary
from tau_coding.resources import (
    ResourceDiagnostic,
    ResourceError,
    TauResourcePaths,
    discover_system_prompt_resources,
    resource_paths_with_cwd,
    resource_paths_with_project_trust,
)
from tau_coding.session_export import (
    default_session_export_artifact_path,
    export_session_artifact,
    normalize_export_format,
)
from tau_coding.session_manager import (
    InferenceProviderMode,
    SessionManager,
    normalize_session_name,
)
from tau_coding.session_stats import SessionStats, calculate_session_stats
from tau_coding.skills import Skill, expand_skill_command, load_skills_with_diagnostics
from tau_coding.system_prompt import (
    BuildSystemPromptOptions,
    ProjectContextFile,
    PromptSection,
    SystemPromptInspection,
    SystemPromptSource,
    build_system_prompt,
    build_system_prompt_inspection,
)
from tau_coding.thinking import (
    DEFAULT_THINKING_LEVEL,
    THINKING_LEVELS,
    ThinkingLevel,
    next_thinking_level,
    normalize_thinking_level,
)
from tau_coding.tools import ImageSupportState, create_bash_tool, create_coding_tools

StreamingBehavior = Literal["steer", "follow_up"]
SESSION_NAME_SYSTEM_PROMPT = (
    "You write concise coding-agent session names. Reply with only a short title, "
    "maximum four words, no quotes, no punctuation-only output."
)
TREE_RUNNING_MESSAGE = "Tau is still working. Press Escape to interrupt before using /tree."


async def _await_cleanup_completion[CleanupResult](
    task: asyncio.Task[CleanupResult],
) -> bool:
    """Wait through caller cancellation without forwarding it into cleanup.

    等待清理完成，同时不把调用方取消传递给清理任务。

    Returns whether cancellation was observed. Repeated requests remain
    contained until the independently owned cleanup task reaches a terminal
    state.

    返回是否观察到取消。重复取消请求会继续被隔离，直到独立拥有的清理任务
    到达终止状态。
    """
    cancelled = False
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            if not task.done():
                continue
        except BaseException:  # cleanup outcome is inspected by its owner
            # 清理结果由其所有者检查。
            pass
        return cancelled


async def _finish_adopted_runtime_close(runtime: ExtensionRuntime) -> None:
    """Finish outgoing cleanup after publication without failing adoption.

    在发布后完成退出清理，同时不让接纳操作失败。
    """
    task = asyncio.create_task(
        runtime.aclose(),
        name="tau-adopted-extension-runtime-close",
    )
    await _await_cleanup_completion(task)
    # Publication is already committed. Cleanup cancellation/failure must not
    # masquerade as rollback while the task's outcome still gets retrieved.
    #
    # 发布已经提交。清理取消或失败不能伪装成回滚，同时仍需取得任务结果。
    with suppress(BaseException):
        task.result()


async def _finish_aborted_session_close(session: CodingSession) -> None:
    """Discharge an unpublished session's resources without masking its abort.

    释放未发布会话的资源，同时不掩盖其中止结果。
    """
    task = asyncio.create_task(
        session.aclose(),
        name="tau-aborted-coding-session-close",
    )
    await _await_cleanup_completion(task)
    # The pre-publication failure remains primary, but every owned provider was
    # attempted by CodingSession's durable, idempotent close task.
    #
    # 发布前失败仍是主要结果，但 CodingSession 的持久且幂等关闭任务会尝试
    # 关闭每一个已拥有的提供者。
    with suppress(BaseException):
        task.result()


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """A selectable model and the provider that serves it.

    可选择的模型及为其提供服务的提供者。
    """

    provider_name: str
    model: str


@dataclass(frozen=True, slots=True)
class ModelSelectionResult:
    """Result of a candidate-first provider/model selection.

    候选优先的提供者与模型选择结果。
    """

    choice: ModelChoice
    changed: bool


@dataclass(frozen=True, slots=True)
class TerminalCommandResult:
    """Result of an input-bar terminal command.

    输入栏终端命令的结果。
    """

    command: str
    output: str
    exit_code: int | None
    ok: bool
    added_to_context: bool


@dataclass(frozen=True, slots=True)
class SessionTreeChoice:
    """One branchable entry in the active session tree.

    活动会话树中的一个可分支条目。
    """

    entry_id: str
    label: str
    active: bool = False
    is_tool_call: bool = False
    bookmark_label: str | None = None
    label_timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class SessionTreeBranchResult:
    """Result of moving the active session tree leaf.

    移动活动会话树叶节点的结果。
    """

    message: str
    input_prefill: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalCommandRequest:
    """Parsed input-bar terminal command request.

    已解析的输入栏终端命令请求。
    """

    command: str
    add_to_context: bool


@dataclass(frozen=True, slots=True)
class SessionResources:
    """Tau-owned resources loaded around a coding session.

    围绕编码会话加载且由 Tau 拥有的资源。
    """

    skills: tuple[Skill, ...]
    prompt_templates: tuple[PromptTemplate, ...]
    context_files: tuple[ProjectContextFile, ...]
    custom_system_prompt: str | None
    custom_system_prompt_path: Path | None
    append_system_prompt: str | None
    append_system_prompts: tuple[str, ...]
    append_system_prompt_paths: tuple[Path, ...]
    diagnostics: tuple[ResourceDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    """Prepared active-context entries for a compaction run.

    为压缩运行准备的活动上下文条目。
    """

    first_kept_entry_id: str
    replaced_entry_count: int
    messages_to_summarize: tuple[AgentMessage, ...]


@dataclass(frozen=True, slots=True)
class _GeneratedSummary:
    """Summary text paired with the provider usage spent producing it.

    摘要文本及生成该文本所消耗的提供者用量。
    """

    text: str
    usage: Usage | None
    provider: str | None = None
    model: str | None = None
    response_provider: str | None = None


@dataclass(frozen=True, slots=True)
class ManualCompactionResult:
    """Structured result from one manual compaction.

    一次手动压缩的结构化结果。
    """

    summary: str
    first_kept_entry_id: str
    tokens_before: int
    estimated_tokens_after: int
    replaced_entry_count: int


@dataclass(frozen=True, slots=True)
class _PendingMessageWrite:
    """Stable entry retained while a message persistence attempt is retried.

    重试消息持久化时保留的稳定条目。
    """

    message: AgentMessage
    entry: MessageEntry | CustomMessageEntry


@dataclass(frozen=True, slots=True)
class CodingSessionConfig:
    """Configuration for a persistent coding session.

    持久化编码会话的配置。
    """

    provider: ModelProvider | None
    model: str
    storage: SessionStorage
    cwd: Path
    system: str | None = None
    custom_system_prompt: str | None = None
    append_system_prompt: str | None = None
    context_files: tuple[ProjectContextFile, ...] = ()
    tools: list[AgentTool] | None = None
    resource_paths: TauResourcePaths | None = None
    session_id: str | None = None
    session_manager: SessionManager | None = None
    command_registry: CommandRegistry | None = None
    provider_name: str = "openai"
    inference_provider: str | None = None
    inference_provider_mode: InferenceProviderMode | None = None
    requested_provider: str | None = None
    requested_model: str | None = None
    session_provider_name: str | None = None
    provider_settings: ProviderSettings | None = None
    runtime_model_catalogs: Mapping[str, RuntimeModelCatalog] = field(default_factory=dict)
    runtime_provider_config: ProviderConfig | None = None
    dynamic_provider: DynamicProvider | None = None
    owns_initial_provider: bool = False
    auto_compact_token_threshold: int | None = None
    auto_compact_enabled: bool = True
    thinking_level: ThinkingLevel = DEFAULT_THINKING_LEVEL
    thinking_level_override: ThinkingLevel | None = None
    """One-shot startup override (e.g. ``--thinking``) for the session's level.

    会话思考等级的一次性启动覆盖值，例如 ``--thinking``。

    Takes precedence over remembered per-model defaults and replayed session
    state, is validated strictly against the active model's available levels
    when the provider configuration is known, and is never persisted as a new
    remembered default.

    它优先于记忆的每模型默认值和重放的会话状态；当提供者配置已知时，会严格
    按活动模型的可用等级验证，并且绝不会持久化为新的记忆默认值。
    """
    index_on_first_persist: bool = False
    shell_command_prefix: str | None = None
    skills_enabled: bool = True
    """Whether skill discovery is enabled for this session.

    当前会话是否启用技能发现。

    When ``True`` (the default), skills are discovered from the resource paths
    and their index is injected into the system prompt as ``<available_skills>``,
    and ``/skill:`` commands expand against them. When ``False``, skill discovery
    is suppressed for the whole session: no skills load, the ``<available_skills>``
    index is omitted, and ``/skill:`` commands find nothing to expand. This mirrors
    Pi's loader-level ``noSkills`` flag, which suppresses only skill discovery and
    leaves prompt templates and project context files (AGENTS.md) unaffected. It is
    the seam hosts use to construct skill-less sessions (e.g. a subagent type that
    gets no skills).

    为 ``True``（默认值）时，从资源路径发现技能，将可用技能索引注入系统提示词，
    并让 ``/skill:`` 命令针对这些技能展开。为 ``False`` 时，整个会话都禁用
    技能发现：不加载技能、省略可用技能索引，且 ``/skill:`` 命令找不到可展开
    内容。这与 Pi
    加载器级别的 ``noSkills`` 标志一致：它只抑制技能发现，不影响提示词模板和
    项目上下文文件（AGENTS.md）。宿主可通过此接口构造无技能会话，例如不获取
    任何技能的子代理类型。
    """
    extension_paths: tuple[Path, ...] = ()
    extensions_enabled: bool = True
    project_extensions_enabled: bool = False
    extension_runtime: ExtensionRuntime | None = None
    project_trust_coordinator: ProjectTrustCoordinator | None = None
    trust_override: TrustOverride | None = None
    trust_default: TrustDefault = "ask"
    trust_interactive: bool = False
    trust_prompt: TrustPrompt | None = None
    # Shared preparation keeps transcript/trust/index writes staged until
    # PreparedCodingSession.adopt() reaches its durable commit point.
    #
    # 共享准备流程会暂存会话记录、信任和索引写入，直到
    # PreparedCodingSession.adopt() 到达持久提交点。
    defer_authoritative_writes: bool = False


class CodingSession:
    """Tau's coding-agent environment wrapper.

    Tau 的编码代理环境包装器。

    `AgentHarness` owns the in-memory agent brain. `CodingSession` owns the
    coding-session environment around it: durable session entries, default coding
    tools, and a small command seam for later phases.

    `AgentHarness` 管理内存中的代理核心。`CodingSession` 管理其外围的编码会话
    环境，包括持久化会话条目、默认编码工具，以及供后续阶段使用的小型命令接口。
    """

    def __init__(
        self,
        config: CodingSessionConfig,
        *,
        state: SessionState,
        harness: AgentHarness,
        last_parent_id: str | None,
        skills: tuple[Skill, ...] = (),
        prompt_templates: tuple[PromptTemplate, ...] = (),
        context_files: tuple[ProjectContextFile, ...] = (),
        custom_system_prompt: str | None = None,
        custom_system_prompt_path: Path | None = None,
        append_system_prompt: str | None = None,
        append_system_prompts: tuple[str, ...] = (),
        append_system_prompt_paths: tuple[Path, ...] = (),
        resource_diagnostics: tuple[ResourceDiagnostic, ...] = (),
        command_registry: CommandRegistry | None = None,
        pending_initial_entries: tuple[SessionEntry, ...] = (),
        extension_runtime: ExtensionRuntime | None = None,
        image_support: ImageSupportState | None = None,
        project_trust_resolution: ProjectTrustResolution | None = None,
    ) -> None:
        """Initialize a coding session from prepared runtime state and resources.

        根据准备好的运行时状态和资源初始化编码会话。
        """
        self._config = config
        self._state = state
        self._harness = harness
        self._extension_runtime = extension_runtime or ExtensionRuntime()
        self._provider_registry = self._extension_runtime.provider_registry
        self._image_support = image_support or ImageSupportState()
        self._session_start_pending = False
        self._last_parent_id = last_parent_id
        self._pending_initial_entries = pending_initial_entries
        self._prepared_entries: list[SessionEntry] = list(pending_initial_entries)
        self._skills = skills
        self._prompt_templates = prompt_templates
        self._context_files = context_files
        self._custom_system_prompt = custom_system_prompt
        self._custom_system_prompt_path = custom_system_prompt_path
        self._append_system_prompt = append_system_prompt
        self._append_system_prompts = append_system_prompts
        self._append_system_prompt_paths = append_system_prompt_paths
        self._resource_diagnostics = resource_diagnostics
        self._command_registry = command_registry or create_default_command_registry()
        self._provider_name = config.provider_name
        self._inference_provider = config.inference_provider
        self._inference_provider_mode: InferenceProviderMode = config.inference_provider_mode or (
            "fixed" if config.inference_provider is not None else "automatic"
        )
        self._provider_settings = config.provider_settings
        self._durable_provider_settings = config.provider_settings
        self._runtime_model_catalogs = dict(config.runtime_model_catalogs)
        self._model_catalog_discovery_errors: dict[str, str] = {}
        self._runtime_provider_config = config.runtime_provider_config
        self._resource_paths = resource_paths_with_cwd(config.resource_paths, config.cwd)
        self._auto_compact_token_threshold = config.auto_compact_token_threshold
        self._auto_compact_enabled = config.auto_compact_enabled
        self._thinking_level = _state_thinking_level(
            state,
            default=_default_thinking_level_for_active_model(self),
        )
        self._context_usage_cache: ContextUsageEstimate | None = None
        self._owned_providers: list[ClosableModelProvider] = []
        self._close_task: asyncio.Task[None] | None = None
        self._diagnostic_logger = AgentCallDiagnosticLogger.from_paths(self._resource_paths.paths)
        self._credential_store = FileCredentialStore(
            credentials_path(self._resource_paths.paths) if self._resource_paths.paths else None
        )
        self._last_diagnostic_log_path: Path | None = None
        self._runtime_model_limits: RuntimeModelLimits | None = None
        self._runtime_model_limits_key: tuple[str, str] | None = None
        self._model_limits_discovery_error: str | None = None
        self._project_trust_resolution = project_trust_resolution
        self._project_trust_commit_pending = False
        self._persistence_unsubscribe: Callable[[], None] | None = None
        self._persisted_message_ids: set[int] = set()
        self._ended_message_ids: set[int] = set()
        self._pending_message_writes: dict[int, _PendingMessageWrite] = {}
        self._attach_persistence_listener()

    @classmethod
    async def load(cls, config: CodingSessionConfig) -> CodingSession:
        """Load a coding session from append-only storage.

        从仅追加存储加载编码会话。
        """
        entries = await config.storage.read_all()
        pending_initial_entries: tuple[SessionEntry, ...] = ()
        if not entries:
            info = SessionInfoEntry(cwd=str(config.cwd))
            initial_model = _initial_model_for_config(config)
            model = ModelChangeEntry(
                parent_id=info.id,
                model=initial_model,
                provider=config.requested_provider or config.provider_name,
            )
            thinking = ThinkingLevelChangeEntry(
                parent_id=model.id,
                thinking_level=_initial_thinking_level_for_config(config, model=initial_model),
            )
            entries = [info, model, thinking]
            pending_initial_entries = (info, model, thinking)
        else:
            entries = _detach_missing_parents(entries)

        state = SessionState.from_entries(entries)
        unfiltered_resource_paths = resource_paths_with_cwd(config.resource_paths, config.cwd)

        # A runtime is cwd-bound because it may contain project registrations.
        # Always stage a fresh eligible-only runtime for a destination snapshot;
        # never reuse source-project code across replacement trust boundaries.
        #
        # 运行时与工作目录绑定，因为其中可能包含项目注册项。始终为目标快照
        # 暂存全新的、仅含合格资源的运行时，绝不跨替换信任边界复用源项目代码。
        previous_runtime = config.extension_runtime
        runtime_paths = unfiltered_resource_paths.paths or TauPaths(
            home=unfiltered_resource_paths.root,
            agents_home=unfiltered_resource_paths.agents_root or Path.home() / ".agents",
        )
        credential_store = FileCredentialStore(credentials_path(runtime_paths))
        injected_credentials = (
            previous_runtime.provider_credentials if previous_runtime is not None else None
        )
        extension_runtime = ExtensionRuntime(
            paths=runtime_paths,
            credentials=injected_credentials or credential_store,
            environment=(
                previous_runtime.provider_environment if previous_runtime is not None else None
            ),
            built_in_credentials=(
                previous_runtime.built_in_credentials if previous_runtime is not None else None
            ),
            built_in_http_client=(
                previous_runtime.built_in_http_client if previous_runtime is not None else None
            ),
            durable_providers=(
                config.provider_settings.providers if config.provider_settings else ()
            ),
        )
        extension_runtime.load(
            unfiltered_resource_paths,
            extra_paths=config.extension_paths,
            include_resource_dirs=config.extensions_enabled,
            include_project_dir=False,
        )

        coordinator = config.project_trust_coordinator or ProjectTrustCoordinator(
            ProjectTrustStore(
                unfiltered_resource_paths.paths or TauPaths(home=unfiltered_resource_paths.root)
            )
        )
        summary, trust_resolution = await coordinator.resolve(
            config.cwd,
            override=config.trust_override,
            default=config.trust_default,
            interactive=config.trust_interactive,
            prompt=config.trust_prompt,
            extension_deciders=(extension_runtime.decide_project_trust,),
            cache_result=False,
            persist=not config.defer_authoritative_writes,
        )
        canonical_cwd = summary.cwd.value
        resource_paths = resource_paths_with_project_trust(
            resource_paths_with_cwd(config.resource_paths, canonical_cwd),
            trusted=trust_resolution.trusted,
        )
        config = replace(
            config,
            cwd=canonical_cwd,
            resource_paths=resource_paths,
            project_trust_coordinator=coordinator,
            extension_runtime=extension_runtime,
        )
        resources = _load_session_resources(
            resource_paths,
            config.context_files,
            skills_enabled=config.skills_enabled,
            system_prompt_enabled=config.system is None,
            custom_system_prompt_explicit=config.custom_system_prompt is not None,
        )
        if summary.categories:
            resources = replace(
                resources,
                diagnostics=(
                    *resources.diagnostics,
                    ResourceDiagnostic(
                        kind="project-trust",
                        message=format_trust_diagnostic(summary, trust_resolution),
                        severity="info" if trust_resolution.trusted else "warning",
                    ),
                ),
            )

        if trust_resolution.trusted and config.project_extensions_enabled:
            extension_runtime.load(
                resource_paths,
                include_resource_dirs=True,
                include_project_dir=True,
                include_user_dir=False,
            )

        runtime_model_catalogs = dict(config.runtime_model_catalogs)
        if config.provider_settings is not None and "openai-codex" not in runtime_model_catalogs:
            codex_config = _provider_config_or_none(config.provider_settings, "openai-codex")
            if isinstance(codex_config, OpenAICodexProviderConfig):
                account_id = _codex_account_id(codex_config, credential_store)
                cached_catalog = cached_codex_model_catalog(
                    runtime_paths,
                    account_id=account_id,
                )
                if cached_catalog is not None:
                    runtime_model_catalogs["openai-codex"] = cached_catalog
        config = replace(config, runtime_model_catalogs=runtime_model_catalogs)
        selection_settings = _provider_settings_with_runtime_catalogs(
            config.provider_settings,
            runtime_model_catalogs,
        )
        selected_provider_name = (
            config.requested_provider or state.provider or config.session_provider_name
        )
        selected_model = config.requested_model or state.model
        rediscover_codex = False
        if selected_provider_name == "openai-codex" and config.provider_settings is not None:
            selected_config = _provider_config_or_none(
                config.provider_settings,
                selected_provider_name,
            )
            rediscover_codex = (
                isinstance(selected_config, OpenAICodexProviderConfig)
                and selected_model not in selected_config.models
            )
        preparation_settings = config.provider_settings
        startup_catalog: RuntimeModelCatalog | None = None
        effective_selected_config = (
            _provider_config_or_none(selection_settings, selected_provider_name)
            if selection_settings is not None
            else None
        )
        if (
            isinstance(effective_selected_config, OpenAICodexProviderConfig)
            and selected_model in effective_selected_config.models
        ):
            preparation_settings = selection_settings
        if config.provider is None or rediscover_codex:
            prepared = await _prepare_provider_selection(
                replace(config, provider_settings=preparation_settings),
                state=state,
                provider_registry=extension_runtime.provider_registry,
                credential_store=credential_store,
            )
            if config.provider is not None and config.owns_initial_provider:
                try:
                    await config.provider.aclose()  # type: ignore[attr-defined]
                except BaseException:
                    await prepared.provider.aclose()
                    raise
            startup_catalog = prepared.runtime_model_catalog
            if startup_catalog is not None:
                runtime_model_catalogs[prepared.provider_name] = startup_catalog
            config = replace(
                config,
                provider=prepared.provider,
                runtime_model_catalogs=runtime_model_catalogs,
                model=prepared.model,
                provider_name=prepared.provider_name,
                inference_provider=prepared.inference_provider,
                inference_provider_mode=prepared.inference_provider_mode,
                runtime_provider_config=prepared.runtime_provider_config,
                dynamic_provider=prepared.dynamic_provider,
                owns_initial_provider=True,
            )
            if pending_initial_entries:
                pending_initial_entries = tuple(
                    entry.model_copy(
                        update={"model": config.model, "provider": config.provider_name}
                    )
                    if isinstance(entry, ModelChangeEntry)
                    else entry
                    for entry in pending_initial_entries
                )
                # Initial fallback metadata was created before live discovery.
                # Replay the corrected entries so runtime and durable selection agree.
                #
                # 初始后备元数据创建于实时发现之前，因此重放修正后的条目，
                # 使运行时选择与持久化选择保持一致。
                state = SessionState.from_entries(list(pending_initial_entries))
        assert config.provider is not None
        active_model = _runtime_model_for_state(config, state)
        image_support = ImageSupportState(
            supported=_configured_model_supports_images(config, active_model)
        )
        base_tools = (
            config.tools
            if config.tools is not None
            else create_coding_tools(
                cwd=config.cwd,
                shell_command_prefix=config.shell_command_prefix,
                image_support=image_support,
            )
        )
        tools = extension_runtime.compose_tools(base_tools)
        system = (
            config.system
            if config.system is not None
            else build_system_prompt(
                BuildSystemPromptOptions(
                    cwd=config.cwd,
                    tools=tools,
                    skills=resources.skills,
                    custom_prompt=(
                        config.custom_system_prompt
                        if config.custom_system_prompt is not None
                        else resources.custom_system_prompt
                    ),
                    append_sections=_append_prompt_sections(
                        resources.append_system_prompts,
                        resources.append_system_prompt_paths,
                        config.append_system_prompt,
                    ),
                    context_files=resources.context_files,
                    extra_guidelines=extension_runtime.prompt_guidelines,
                    extra_sections=extension_runtime.sourced_prompt_sections,
                    custom_prompt_source=_custom_prompt_source(
                        explicit=config.custom_system_prompt is not None,
                        path=resources.custom_system_prompt_path,
                    ),
                )
            )
        )
        harness = AgentHarness(
            AgentHarnessConfig(
                provider=config.provider,
                model=active_model,
                system=system,
                tools=tools,
                session_id=config.session_id,
            ),
            messages=state.messages,
        )
        if previous_runtime is not None:
            extension_runtime.set_ui_bridge(previous_runtime.ui)
        session = cls(
            config,
            state=state,
            harness=harness,
            last_parent_id=_last_parent_id_from_state(state),
            skills=resources.skills,
            prompt_templates=resources.prompt_templates,
            context_files=resources.context_files,
            custom_system_prompt=resources.custom_system_prompt,
            custom_system_prompt_path=resources.custom_system_prompt_path,
            append_system_prompt=resources.append_system_prompt,
            append_system_prompts=resources.append_system_prompts,
            append_system_prompt_paths=resources.append_system_prompt_paths,
            resource_diagnostics=resources.diagnostics,
            command_registry=config.command_registry or extension_runtime.build_command_registry(),
            pending_initial_entries=pending_initial_entries,
            extension_runtime=extension_runtime,
            image_support=image_support,
            project_trust_resolution=trust_resolution,
        )
        if config.owns_initial_provider:
            # Ownership starts before any repair/discovery work so every
            # failure path has exactly one closer for the candidate.
            #
            # 在任何修复或发现工作前取得所有权，使每条失败路径都恰好有一个
            # 候选提供者关闭方。
            session._owned_providers.append(config.provider)  # type: ignore[arg-type]
        if startup_catalog is not None and config.provider_name == "openai-codex":
            session._persist_codex_model_catalog(
                config.provider,
                startup_catalog,
                (
                    config.runtime_provider_config
                    if isinstance(config.runtime_provider_config, OpenAICodexProviderConfig)
                    else None
                ),
            )
        await session._persist_active_tool_history_repairs()
        try:
            session._apply_runtime_model_catalogs()
            session._apply_thinking_level_override()
            session._sync_thinking_level_to_active_model()
            if not config.owns_initial_provider and config.provider is not None:
                session._refresh_runtime_provider()
            await session._refresh_runtime_model_limits()
            extension_runtime.bind(session)
            # Attach to session._harness, not the local `harness`:
            # Tool-history repair above updates the active harness before extension
            # listeners attach.
            #
            # 监听器应附加到 session._harness，而不是局部变量 `harness`：
            # 上面的工具历史修复会在扩展监听器附加前更新活动核心。
            extension_runtime.attach_harness_listener(session._harness.subscribe)
            # session_start is deferred: hosts emit it via emit_pending_session_start()
            # after installing their UI bridge.
            #
            # session_start 会延迟发出：宿主安装界面桥接后，通过
            # emit_pending_session_start() 发出该事件。
            session._session_start_pending = True
            session._project_trust_commit_pending = not trust_resolution.cancelled
        except BaseException:
            # Once constructed, this session explicitly owns every candidate
            # provider until load returns it to the caller.
            #
            # 会话一旦构造完成，就显式拥有每个候选提供者，直到 load 将会话
            # 返回给调用方。
            await _finish_aborted_session_close(session)
            raise
        return session

    @property
    def cwd(self) -> Path:
        """Return the session working directory.

        返回会话工作目录。
        """
        return self._config.cwd

    @property
    def model(self) -> str:
        """Return the active model for this session.

        返回当前会话的活动模型。
        """
        return self._harness.config.model

    @property
    def provider_name(self) -> str:
        """Return the active provider name.

        返回活动提供者名称。
        """
        return self._provider_name

    @property
    def provider(self) -> ModelProvider:
        """Return the currently active runtime provider.

        返回当前活动的运行时提供者。
        """
        return self._harness.config.provider

    @property
    def inference_provider(self) -> str | None:
        """Return the pinned Hugging Face backing provider, if any.

        返回固定的 Hugging Face 后端提供者（如果存在）。
        """
        return self._inference_provider

    @property
    def inference_provider_mode(self) -> InferenceProviderMode:
        """Return whether Hugging Face routing is automatic or explicitly fixed.

        返回 Hugging Face 路由是自动选择还是显式固定。
        """
        return self._inference_provider_mode

    @property
    def _active_dynamic_provider(self) -> DynamicProvider | None:
        """Return the active dynamic provider definition when present.

        在存在时返回活动的动态提供者定义。
        """
        effective = self._provider_registry.effective(self._provider_name)
        if effective is None or not isinstance(effective.definition, DynamicProvider):
            return None
        return effective.definition

    @property
    def _active_dynamic_model(self) -> ProviderModel | None:
        """Return the active model row from a dynamic provider.

        返回动态提供者中的活动模型记录。
        """
        dynamic = self._active_dynamic_provider
        if dynamic is None:
            return None
        return next((model for model in dynamic.models if model.id == self.model), None)

    @property
    def available_providers(self) -> tuple[str, ...]:
        """Return provider names Tau can call with available credentials.

        返回 Tau 可使用现有凭据调用的提供者名称。
        """
        names: list[str] = []
        if self._provider_settings is not None:
            names.extend(provider.name for provider in self._usable_provider_configs())
        for effective in self._provider_registry.effective_providers():
            if (
                isinstance(effective.definition, DynamicProvider)
                and (effective.definition.models or effective.definition.id == self._provider_name)
                and effective.definition.id not in names
            ):
                names.append(effective.definition.id)
        return tuple(names) or (self._provider_name,)

    def provider_config(self, provider_name: str) -> ProviderConfig | None:
        """Return configured metadata for a provider available to this session.

        返回当前会话可用提供者的配置元数据。
        """
        if self._provider_settings is None:
            return self._runtime_provider_config if provider_name == self.provider_name else None
        try:
            return self._provider_settings.get_provider(provider_name)
        except ProviderConfigError:
            return None

    @property
    def available_models(self) -> tuple[str, ...]:
        """Return model names for the active provider when it is usable.

        当活动提供者可用时返回其模型名称。
        """
        dynamic = self._active_dynamic_provider
        if dynamic is not None:
            return tuple(model.id for model in dynamic.models)
        if self._provider_settings is None:
            return (self.model,)
        try:
            provider = self._provider_settings.get_provider(self._provider_name)
        except ProviderConfigError:
            return (self.model,)
        if not self._provider_is_usable(provider):
            return ()
        return provider.models

    @property
    def available_model_choices(self) -> tuple[ModelChoice, ...]:
        """Return provider/model choices from the effective runtime view.

        从有效运行时视图返回提供者与模型选项。
        """
        choices: list[ModelChoice] = []
        dynamic_ids: set[str] = set()
        for effective in self._provider_registry.effective_providers():
            if isinstance(effective.definition, DynamicProvider):
                dynamic = effective.definition
                dynamic_ids.add(dynamic.id)
                choices.extend(ModelChoice(dynamic.id, model.id) for model in dynamic.models)
        if self._provider_settings is not None:
            choices.extend(
                ModelChoice(provider.name, model)
                for provider in self._usable_provider_configs()
                if provider.name not in dynamic_ids
                for model in provider.models
            )
        if not choices and self._provider_settings is None:
            return (ModelChoice(provider_name=self._provider_name, model=self.model),)
        return tuple(choices)

    @property
    def scoped_model_choices(self) -> tuple[ModelChoice, ...]:
        """Return scoped references, including inert trusted built-in references.

        返回限定范围的引用，包括未激活但可信的内置引用。
        """
        if self._provider_settings is None:
            return ()
        available = set(self.available_model_choices)
        choices: list[ModelChoice] = []
        for item in self._provider_settings.scoped_models:
            choice = ModelChoice(provider_name=item.provider, model=item.model)
            if choice in available or self._stable_dynamic_scoped_provider(item.provider):
                choices.append(choice)
        return tuple(choices)

    @property
    def unavailable_scoped_model_choices(self) -> tuple[ModelChoice, ...]:
        """Return persisted references that have no current provider snapshot row.

        返回当前提供者快照中没有对应记录的持久化引用。
        """
        available = set(self.available_model_choices)
        return tuple(choice for choice in self.scoped_model_choices if choice not in available)

    def _stable_dynamic_scoped_provider(self, provider_name: str) -> bool:
        """Return whether any built-in layer preserves stable scoped references.

        返回是否有内置层保留稳定的限定范围引用。
        """
        return any(
            layer.token.source_id.startswith("built-in:")
            and layer.provider.stable_scoped_references
            for layer in self._provider_registry.layers(provider_name)
        )

    def _effective_stable_dynamic_scoped_provider(self, provider_name: str) -> bool:
        """Return whether the effective dynamic provider preserves scoped references.

        返回有效动态提供者是否保留限定范围引用。
        """
        effective = self._provider_registry.effective(provider_name)
        return bool(
            effective is not None
            and effective.source_id.startswith("built-in:")
            and isinstance(effective.definition, DynamicProvider)
            and effective.definition.stable_scoped_references
        )

    @property
    def tools(self) -> tuple[AgentTool, ...]:
        """Return the tools available to the agent.

        返回代理可用的工具。
        """
        return tuple(self._harness.config.tools)

    @property
    def extension_tool_sources(self) -> dict[str, str]:
        """Map active extension-provided tools to their owning extension.

        将活动扩展提供的工具映射到所属扩展。
        """
        return self._extension_runtime.extension_tool_sources

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        """Return the restored/current transcript.

        返回已恢复或当前的会话记录。
        """
        return self._harness.messages

    @property
    def state(self) -> SessionState:
        """Return the last replayed durable session state.

        返回最近一次重放得到的持久化会话状态。
        """
        return self._state

    async def session_entries(self) -> tuple[SessionEntry, ...]:
        """Return append-only entries for frontend session inspection.

        返回仅追加条目，供前端检查会话。
        """
        return tuple(await self._read_session_entries())

    async def tree_choices(self) -> tuple[SessionTreeChoice, ...]:
        """Return branchable session entries for a tree picker.

        返回可分支会话条目，供树形选择器使用。
        """
        entries = await self._read_session_entries()
        ordered_entries, branch_indents = _tree_layout(entries)
        labels_by_id, label_timestamps_by_id = _resolved_labels(entries)
        active_choice_id = _active_branchable_entry_id(entries, self._state.active_leaf_id)
        return tuple(
            SessionTreeChoice(
                entry_id=entry.id,
                label=_tree_choice_label(entry, branch_indent=branch_indents.get(entry.id, 0)),
                active=entry.id == active_choice_id,
                is_tool_call=_is_tool_call_tree_entry(entry),
                bookmark_label=labels_by_id.get(entry.id),
                label_timestamp=label_timestamps_by_id.get(entry.id),
            )
            for entry in ordered_entries
            if _is_branchable_tree_entry(entry)
        )

    async def set_label(self, target_id: str, label: str | None) -> LabelEntry:
        """Set or clear the bookmark on an existing session entry.

        设置或清除现有会话条目上的书签。
        """
        if self._harness.is_running:
            raise RuntimeError(TREE_RUNNING_MESSAGE)
        await self._flush_pending_message_writes(context=self._diagnostic_context())
        entries = await self._read_session_entries()
        if target_id not in {entry.id for entry in entries}:
            raise ValueError(f"Unknown session entry: {target_id}")
        normalized = label.strip() if label is not None else ""
        entry = LabelEntry(
            parent_id=self._last_parent_id,
            target_id=target_id,
            label=normalized or None,
        )
        await self._append_session_entry(entry)
        self._last_parent_id = entry.id
        await self._refresh_persisted_state(leaf_id=entry.id)
        return entry

    async def branch_to_entry(
        self,
        entry_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
        replace_instructions: bool = False,
    ) -> SessionTreeBranchResult:
        """Move the active leaf to a previous entry, preserving existing history.

        将活动叶节点移动到之前的条目，同时保留现有历史。
        """
        if self._harness.is_running:
            raise RuntimeError(TREE_RUNNING_MESSAGE)
        await self._flush_pending_message_writes(context=self._diagnostic_context())
        entries = await self._read_session_entries()
        by_id = {entry.id: entry for entry in entries}
        if entry_id not in by_id:
            raise ValueError(f"Unknown session entry: {entry_id}")
        selected_entry = by_id[entry_id]
        if not _is_branchable_tree_entry(selected_entry):
            raise ValueError(f"Session entry cannot be branched from: {entry_id}")

        target_id: str | None = entry_id
        input_prefill: str | None = None
        summary_entry: BranchSummaryEntry | None = None
        if summarize:
            abandoned_messages = _messages_after_entry_on_active_path(
                entries,
                entry_id,
                self._last_parent_id,
            )
            if abandoned_messages:
                generated = await self._summarize_branch_messages(
                    abandoned_messages,
                    custom_instructions=custom_instructions,
                    replace_instructions=replace_instructions,
                )
                summary_entry = BranchSummaryEntry(
                    parent_id=entry_id,
                    branch_root_id=entry_id,
                    summary=generated.text,
                    usage=generated.usage,
                    provider=generated.provider,
                    model=generated.model,
                    response_provider=generated.response_provider,
                )
                await self._append_session_entry(summary_entry)
                target_id = summary_entry.id
        elif selected_entry.type == "message" and isinstance(selected_entry.message, UserMessage):
            target_id = selected_entry.parent_id
            input_prefill = selected_entry.message.text

        self._last_parent_id = target_id

        # Plain navigation is in-memory only. A summary above is the sole write,
        # and becomes the file-order tip when present.
        #
        # 普通导航仅发生在内存中。上方摘要是唯一写入内容，存在时会成为文件
        # 顺序中的最新条目。
        await self._refresh_persisted_state(leaf_id=target_id)
        history_repair = await self._persist_active_tool_history_repairs()
        if history_repair is None:
            self._harness.replace_messages(self._state.messages)
        self._invalidate_context_usage_cache()
        self._thinking_level = _state_thinking_level(
            self._state,
            default=_default_thinking_level_for_active_model(self),
        )
        self._sync_thinking_level_to_active_model()
        self._refresh_runtime_provider()
        suffix = " with branch summary" if summary_entry is not None else ""
        if history_repair is not None:
            suffix += " and repaired malformed tool history"
        if input_prefill is not None:
            return SessionTreeBranchResult(
                message=f"Branched session before {entry_id}{suffix}.",
                input_prefill=input_prefill,
            )
        return SessionTreeBranchResult(message=f"Branched session at {target_id}{suffix}.")

    @property
    def thinking_level(self) -> ThinkingLevel:
        """Return the active thinking mode for future turns.

        返回后续轮次使用的活动思考模式。
        """
        return self._thinking_level

    @property
    def available_thinking_levels(self) -> tuple[ThinkingLevel, ...]:
        """Return thinking modes supported by the active provider/model.

        返回活动提供者和模型支持的思考模式。
        """
        dynamic = self._active_dynamic_provider
        if dynamic is not None:
            model = self._active_dynamic_model
            if model is None:
                return ()
            return model.thinking_levels or ()
        if self._provider_settings is None:
            return THINKING_LEVELS
        provider = self._active_provider_config()
        if provider is None:
            return ()
        return provider_thinking_levels(provider, model=self.model)

    @property
    def thinking_unavailable_reason(self) -> str | None:
        """Return why thinking controls are unavailable for the active model.

        返回活动模型的思考控制不可用的原因。
        """
        if self.available_thinking_levels:
            return None
        dynamic = self._active_dynamic_provider
        if dynamic is not None:
            model = self._active_dynamic_model
            if model is None:
                return f"{self.provider_name}:{self.model} metadata is not available"
            if model.thinking_levels is None:
                return (
                    f"{self.provider_name}:{self.model} does not declare configurable "
                    "thinking levels"
                )
            return f"{self.provider_name}:{self.model} declares no configurable thinking levels"
        provider = self._active_provider_config()
        if provider is None:
            return "Active provider settings are not available"
        return provider_thinking_unavailable_reason(provider, model=self.model)

    @property
    def storage(self) -> SessionStorage:
        """Return the backing session storage.

        返回底层会话存储。
        """
        return self._config.storage

    async def export(
        self,
        destination: Path | None = None,
        *,
        format: str | None = None,
    ) -> Path:
        """Export the current session to a user-facing artifact.

        将当前会话导出为面向用户的产物。
        """
        entries = await self._read_session_entries()
        session_path = _storage_path(self._config.storage)
        export_format = normalize_export_format(
            format or (destination.suffix.removeprefix(".") if destination else "html")
        )
        output_path = _resolve_export_destination(
            destination,
            cwd=self.cwd,
            session_path=session_path,
            format=export_format,
        )
        return export_session_artifact(
            entries,
            output_path,
            title=_session_export_title(self),
            source=str(session_path) if session_path is not None else self.session_id,
            format=export_format,
            system_prompt=self.system_prompt,
        )

    @property
    def skills(self) -> tuple[Skill, ...]:
        """Return loaded skills.

        返回已加载的技能。
        """
        return self._skills

    @property
    def prompt_templates(self) -> tuple[PromptTemplate, ...]:
        """Return loaded prompt templates.

        返回已加载的提示词模板。
        """
        return self._prompt_templates

    @property
    def context_files(self) -> tuple[ProjectContextFile, ...]:
        """Return active project context files.

        返回活动项目上下文文件。
        """
        return self._context_files

    @property
    def system_prompt_files(self) -> tuple[Path, ...]:
        """Return active discovered system-prompt resource files.

        返回已发现且处于活动状态的系统提示词资源文件。
        """
        custom_paths = (
            (self._custom_system_prompt_path,)
            if self._custom_system_prompt_path is not None
            else ()
        )
        return (*custom_paths, *self._append_system_prompt_paths)

    @property
    def context_token_estimate(self) -> int:
        """Return the best available token count for the active provider context.

        返回活动提供者上下文中最可靠的可用令牌计数。
        """
        return self.context_usage.total_tokens

    @property
    def has_provider_context_usage(self) -> bool:
        """Return whether valid provider usage anchors the active context count.

        返回有效提供者用量是否作为活动上下文计数的依据。
        """
        return self.context_usage.uses_provider_usage

    @property
    def context_usage(self) -> ContextUsageEstimate:
        """Return structured context accounting for the active provider context.

        返回活动提供者上下文的结构化上下文计量信息。
        """
        if self._context_usage_cache is None:
            self._context_usage_cache = estimate_context_usage(
                system=self._harness.config.system,
                messages=self._harness.messages,
                tools=tuple(self._harness.config.tools),
            )
        return self._context_usage_cache

    @property
    def system_prompt(self) -> str:
        """Return the effective system prompt sent to the model.

        返回发送给模型的有效系统提示词。
        """
        return self._harness.config.system

    @property
    def system_prompt_inspection(self) -> SystemPromptInspection:
        """Return the effective prompt with source attribution for local inspection.

        返回带来源标注的有效提示词，供本地检查。
        """
        if self._config.system is not None:
            return SystemPromptInspection(
                text=self.system_prompt,
                sources=(
                    SystemPromptSource(
                        kind="system",
                        label="System prompt override",
                        source="CodingSessionConfig.system",
                        content=self.system_prompt,
                    ),
                ),
            )
        inspection = build_system_prompt_inspection(
            BuildSystemPromptOptions(
                cwd=self.cwd,
                tools=self.tools,
                skills=self.skills,
                custom_prompt=(
                    self._config.custom_system_prompt
                    if self._config.custom_system_prompt is not None
                    else self._custom_system_prompt
                ),
                append_sections=_append_prompt_sections(
                    self._append_system_prompts,
                    self._append_system_prompt_paths,
                    self._config.append_system_prompt,
                ),
                context_files=self.context_files,
                extra_guidelines=self._extension_runtime.prompt_guidelines,
                extra_sections=self._extension_runtime.sourced_prompt_sections,
                custom_prompt_source=_custom_prompt_source(
                    explicit=self._config.custom_system_prompt is not None,
                    path=self._custom_system_prompt_path,
                ),
            )
        )
        if inspection.text == self.system_prompt:
            return inspection
        return SystemPromptInspection(
            text=self.system_prompt,
            sources=(
                SystemPromptSource(
                    kind="runtime",
                    label="Effective system prompt",
                    source="active Tau session (runtime-composed)",
                    content=self.system_prompt,
                ),
            ),
        )

    @property
    def auto_compact_token_threshold(self) -> int | None:
        """Return the effective automatic compaction threshold, if any.

        返回有效的自动压缩阈值（如果存在）。
        """
        if not self._auto_compact_enabled:
            return None
        if self._auto_compact_token_threshold is not None:
            return self._auto_compact_token_threshold
        if self._runtime_model_limits_key == (self.provider_name, self.model):
            limits = self._runtime_model_limits
            if limits is not None:
                return limits.effective_auto_compact_token_limit
        return auto_compaction_threshold_for_context_window(self.context_window_tokens)

    def set_auto_compaction_enabled(self, enabled: bool) -> None:
        """Enable or disable automatic compaction for future turns.

        为后续轮次启用或禁用自动压缩。
        """
        self._auto_compact_enabled = enabled
        self._config = replace(self._config, auto_compact_enabled=enabled)

    @property
    def auto_compaction_enabled(self) -> bool:
        """Return whether automatic compaction is enabled.

        返回是否已启用自动压缩。
        """
        return self._auto_compact_enabled

    @property
    def context_window_tokens(self) -> int:
        """Return the active model's discovered or configured context window.

        返回活动模型经发现或配置的上下文窗口。
        """
        if self._runtime_model_limits_key == (self.provider_name, self.model):
            limits = self._runtime_model_limits
            if limits is not None:
                return limits.context_window
        provider = self._active_provider_config()
        if provider is None:
            return DEFAULT_CONTEXT_WINDOW_TOKENS
        return provider.context_windows.get(self.model, DEFAULT_CONTEXT_WINDOW_TOKENS)

    @property
    def context_window_source(self) -> str:
        """Return where the active context-window limit came from.

        返回活动上下文窗口限制的来源。
        """
        if (
            self._runtime_model_limits_key == (self.provider_name, self.model)
            and self._runtime_model_limits is not None
        ):
            return "provider live catalog"
        return "configured catalog"

    @property
    def model_limits_discovery_error(self) -> str | None:
        """Return the last non-fatal live model-limit discovery error.

        返回最近一次非致命的实时模型限制发现错误。
        """
        return self._model_limits_discovery_error

    @property
    def command_registry(self) -> CommandRegistry:
        """Return the slash-command registry used by this session.

        返回当前会话使用的斜杠命令注册表。
        """
        return self._command_registry

    @property
    def project_trust_resolution(self) -> ProjectTrustResolution | None:
        """Return this cwd's completed project-input trust resolution.

        返回当前工作目录已完成的项目输入信任解析结果。
        """
        return self._project_trust_resolution

    @property
    def theme_dirs(self) -> tuple[Path, ...]:
        """Return theme directories permitted by this session's trust snapshot.

        返回当前会话信任快照允许的主题目录。
        """
        return self._resource_paths.themes_dirs

    @property
    def resource_diagnostics(self) -> tuple[ResourceDiagnostic, ...]:
        """Return non-fatal resource and extension diagnostics.

        返回非致命的资源和扩展诊断信息。
        """
        trust_diagnostics: tuple[ResourceDiagnostic, ...] = ()
        if self._project_trust_resolution is not None:
            trust_diagnostics = tuple(
                ResourceDiagnostic(kind="project-trust", message=message, severity="error")
                for message in self._project_trust_resolution.diagnostics
            )
        return self._resource_diagnostics + trust_diagnostics + self._extension_runtime.diagnostics

    @property
    def extension_runtime(self) -> ExtensionRuntime:
        """Return the extension runtime bound to this session.

        返回绑定到当前会话的扩展运行时。
        """
        return self._extension_runtime

    @property
    def extension_names(self) -> tuple[str, ...]:
        """Return loaded extension names in load order.

        按加载顺序返回已加载扩展名称。
        """
        return self._extension_runtime.extension_names

    @property
    def session_stats(self) -> SessionStats:
        """Return cumulative activity and billed usage for the active branch.

        返回活动分支的累计活动和计费用量。
        """
        return calculate_session_stats(
            self._state.entries,
            pricing=self._pricing_for_response,
        )

    # Resolve catalog pricing for one provider, model, and input size.
    #
    # 为指定提供者、模型和输入规模解析目录定价。
    def _pricing_for_response(
        self,
        provider_name: str,
        model: str,
        input_tokens: int,
    ) -> dict[str, float] | None:
        provider = _provider_config_for_name(self._config, provider_name)
        if (
            provider is None
            or provider.name != provider_name
            or not hasattr(provider, "model_metadata")
        ):
            return None
        metadata = provider.model_metadata.get(model)
        if metadata is None:
            return None
        for tier in metadata.cost_tiers:
            if tier.max_input_tokens is None or input_tokens <= tier.max_input_tokens:
                return dict(tier.cost)
        return dict(metadata.cost) if metadata.cost else None

    async def emit_pending_session_start(self) -> None:
        """Emit the `session_start` deferred by `load`, once per session.

        每个会话仅发出一次由 `load` 延迟的 `session_start`。

        Hosts call this after installing their UI bridge so `session_start`
        handlers can use notifications and dialogs (Pi's ordering: the UI
        starts before extensions initialize). Idempotent; a no-op for
        sessions that adopted an already-started extension runtime.

        宿主安装界面桥接后调用此方法，使 `session_start` 处理器能够使用通知和
        对话框，这符合 Pi 的顺序：界面先于扩展初始化启动。此操作幂等；对于
        已接纳已启动扩展运行时的会话，它不执行任何操作。
        """
        if not self._session_start_pending:
            return
        await self._extension_runtime.emit_session_start("startup")
        self._commit_project_trust_resolution()
        self._session_start_pending = False

    def _commit_project_trust_resolution(self) -> None:
        """Publish staged trust only after the session becomes live.

        仅在会话生效后发布暂存的信任结果。
        """
        if not self._project_trust_commit_pending:
            return
        coordinator = self._config.project_trust_coordinator
        resolution = self._project_trust_resolution
        if coordinator is not None and resolution is not None:
            coordinator.commit(CanonicalProjectPath(self.cwd), resolution)
        self._project_trust_commit_pending = False

    def queue_steering_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Queue a steering user message (extension runtime seam).

        将用户引导消息加入队列，作为扩展运行时接口。
        """
        message: AgentMessage = (
            CustomMessage(custom_type=custom_type, content=content, details=details)
            if custom_type is not None
            else UserMessage(content=content)
        )
        self._harness.steer_message(message)

    def queue_follow_up_message(
        self,
        content: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Queue a follow-up user message (extension runtime seam).

        将用户后续消息加入队列，作为扩展运行时接口。
        """
        message: AgentMessage = (
            CustomMessage(custom_type=custom_type, content=content, details=details)
            if custom_type is not None
            else UserMessage(content=content)
        )
        self._harness.follow_up_message(message)

    async def append_custom_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:
        """Persist an extension-owned custom entry on the active branch path.

        在活动分支路径上持久化扩展拥有的自定义条目。

        The entry advances the append-only tree parent chain so it stays on the
        replayed root-to-leaf path (off-path custom entries would be invisible
        to `SessionState` after resume).

        该条目会推进仅追加树的父链，使其保持在重放的根到叶路径上；路径外的
        自定义条目在恢复后对 `SessionState` 不可见。
        """
        entry = CustomEntry(parent_id=self._last_parent_id, namespace=namespace, data=data)
        await self._append_session_entry(entry)
        self._last_parent_id = entry.id
        await self._refresh_persisted_state(leaf_id=entry.id)

    @property
    def session_id(self) -> str | None:
        """Return this session's manager id, if indexed.

        如果会话已索引，则返回其管理器标识符。
        """
        return self._config.session_id

    @property
    def session_title(self) -> str | None:
        """Return this session's indexed human-friendly title, if named.

        如果会话已命名，则返回已索引的易读标题。
        """
        if self._config.session_id is None or self._config.session_manager is None:
            return None
        record = self._config.session_manager.get_session(self._config.session_id)
        if record is None:
            return None
        return record.title

    @property
    def session_name(self) -> str | None:
        """Return this session's indexed human-friendly name, if named.

        如果会话已命名，则返回已索引的易读名称。
        """
        return self.session_title

    @property
    def session_manager(self) -> SessionManager | None:
        """Return the session manager, if available.

        返回会话管理器（如果可用）。
        """
        return self._config.session_manager

    @property
    def is_running(self) -> bool:
        """Return whether this session currently has an active agent run.

        返回当前会话是否存在活动的代理运行。
        """
        return self._harness.is_running

    def _require_idle(self, operation: str) -> None:
        """Reject replacement-like operations until an active turn is drained.

        在活动轮次结束前拒绝类似替换的操作。
        """
        if self._harness.is_running:
            raise RuntimeError(
                f"Cannot {operation} while Tau is working. "
                "Press Escape to interrupt and wait for it to finish."
            )

    @property
    def queued_message_count(self) -> int:
        """Return the number of queued steering and follow-up messages.

        返回已排队引导消息和后续消息的数量。
        """
        return self._harness.pending_message_count

    @property
    def queued_messages(self) -> QueuedMessages:
        """Return queued steering and follow-up messages.

        返回已排队的引导消息和后续消息。
        """
        return self._harness.queued_messages

    @property
    def queued_steering_messages(self) -> tuple[str, ...]:
        """Return queued steering message text for UI display.

        返回已排队的引导消息文本以供界面显示。
        """
        return tuple(message_text(message) for message in self._harness.queued_messages.steering)

    @property
    def queued_follow_up_messages(self) -> tuple[str, ...]:
        """Return queued follow-up message text for UI display.

        返回已排队的后续消息文本以供界面显示。
        """
        return tuple(message_text(message) for message in self._harness.queued_messages.follow_up)

    @property
    def last_diagnostic_log_path(self) -> Path | None:
        """Return the last diagnostic log path written by this session.

        返回当前会话最近写入的诊断日志路径。
        """
        return self._last_diagnostic_log_path

    def set_project_trust_prompt(self, prompt: TrustPrompt) -> None:
        """Install the active frontend's trust prompt for reload/replacement.

        安装活动前端用于重新加载或替换的信任提示。
        """
        self._config = replace(
            self._config,
            trust_interactive=True,
            trust_prompt=prompt,
        )

    def cancel(self) -> None:
        """Cancel the currently running agent turn, if any.

        取消当前正在运行的代理轮次（如果存在）。
        """
        self._harness.cancel()

    def queue_update_event(self) -> QueueUpdateEvent:
        """Return the current queue state as a coding-session event.

        将当前队列状态作为编码会话事件返回。
        """
        return QueueUpdateEvent(
            steering=self.queued_steering_messages,
            follow_up=self.queued_follow_up_messages,
        )

    def clear_queued_messages(self) -> QueuedMessages:
        """Clear queued steering and follow-up messages.

        清空已排队的引导消息和后续消息。
        """
        return self._harness.clear_queues()

    def pop_latest_follow_up_message(self) -> str | None:
        """Remove and return the most recently queued follow-up message.

        移除并返回最近加入队列的后续消息。
        """
        message = self._harness.pop_latest_follow_up()
        return None if message is None else message_text(message)

    def pop_latest_steering_message(self) -> str | None:
        """Remove and return the most recently queued steering message.

        移除并返回最近加入队列的引导消息。
        """
        message = self._harness.pop_latest_steering()
        return None if message is None else message_text(message)

    def set_model(self, model: str) -> None:
        """Switch the active model for future turns and make it the default.

        切换后续轮次的活动模型，并将其设为默认值。
        """
        provider = self._active_provider_config()
        if provider is not None:
            validate_provider_model(provider, model)
        self._harness.config.model = model
        self._inference_provider = _configured_inference_provider(provider, model)
        self._inference_provider_mode = _configured_inference_provider_mode(provider, model)
        self._sync_thinking_level_to_active_model()
        self._refresh_runtime_provider()
        self._sync_image_support()
        self._persist_default_model_choice()
        if self._config.session_id is not None and self._config.session_manager is not None:
            self._config.session_manager.touch_session(
                self._config.session_id,
                model=model,
                provider_name=self.provider_name,
                inference_provider=self._inference_provider,
                inference_provider_mode=self._inference_provider_mode,
                preserve_inference_provider=False,
            )

    async def apply_startup_model_override(self, model: str) -> None:
        """Activate and persist an explicit startup model before the next turn.

        在下一轮前激活并持久化显式指定的启动模型。
        """
        provider = self._active_provider_config()
        if provider is not None:
            validate_provider_model(provider, model)
        if self.model == model:
            return

        self._harness.config.model = model
        self._inference_provider = _configured_inference_provider(provider, model)
        self._inference_provider_mode = _configured_inference_provider_mode(provider, model)
        self._sync_thinking_level_to_active_model()
        self._refresh_runtime_provider()
        self._sync_image_support()
        entry = ModelChangeEntry(
            parent_id=self._last_parent_id,
            model=model,
            provider=self.provider_name,
        )
        await self._append_session_entry(entry)
        self._last_parent_id = entry.id
        await self._refresh_persisted_state(leaf_id=entry.id)

    async def select_provider_model(self, choice: ModelChoice) -> ModelSelectionResult:
        """Switch provider/model with candidate-first durable publication.

        使用候选优先的持久发布方式切换提供者和模型。

        No active state changes until the candidate runtime exists and the
        provider-aware model entry is committed.

        在候选运行时存在且包含提供者信息的模型条目提交前，不改变任何活动状态。
        """
        if self._harness.is_running:
            raise RuntimeError(
                "Tau is still working. Press Escape to interrupt before switching models."
            )
        if choice.provider_name == self.provider_name and choice.model == self.model:
            return ModelSelectionResult(choice, changed=False)

        candidate: ClosableModelProvider | None = None
        selected_dynamic: DynamicProvider | None = None
        selected_config: ProviderConfig | None = None
        selected_inference: str | None = None
        selected_inference_mode: InferenceProviderMode = "automatic"
        selected_thinking = self._thinking_level
        selected_image_support: bool | None = None
        try:
            effective = self._provider_registry.effective(choice.provider_name)
            if effective is not None and isinstance(effective.definition, DynamicProvider):
                selected_dynamic = effective.definition
                selected_model = next(
                    (item for item in selected_dynamic.models if item.id == choice.model), None
                )
                if selected_model is None:
                    raise ProviderConfigError(
                        "Model is not available for provider "
                        f"{choice.provider_name}: {choice.model}"
                    )
                candidate = await create_dynamic_model_provider(
                    selected_dynamic,
                    model=choice.model,
                    credential_store=self._credential_store,
                )
                if (
                    selected_model.thinking_levels is not None
                    and selected_thinking not in selected_model.thinking_levels
                ):
                    selected_thinking = (
                        selected_model.thinking_levels[0]
                        if selected_model.thinking_levels
                        else DEFAULT_THINKING_LEVEL
                    )
                selected_image_support = (
                    "image" in selected_model.input_modalities
                    if selected_model.input_modalities is not None
                    else None
                )
            else:
                if self._provider_settings is None:
                    raise ProviderConfigError(f"Provider is not available: {choice.provider_name}")
                selected_config = self._provider_settings.get_provider(choice.provider_name)
                validate_provider_model(selected_config, choice.model)
                selected_inference = _configured_inference_provider(selected_config, choice.model)
                selected_inference_mode = _configured_inference_provider_mode(
                    selected_config, choice.model
                )
                selected_thinking = _coerced_thinking_level(
                    selected_config,
                    model=choice.model,
                    current=self._thinking_level,
                )
                candidate = _create_runtime_provider(
                    selected_config,
                    credential_store=self._credential_store,
                    model=choice.model,
                    thinking_level=selected_thinking,
                    inference_provider=selected_inference,
                    response_headers_observer=(
                        self._observe_response_headers
                        if selected_config.name == "huggingface"
                        else None
                    ),
                )
                selected_image_support = provider_model_supports_images(
                    selected_config, choice.model
                )

            entry = ModelChangeEntry(
                parent_id=self._last_parent_id,
                model=choice.model,
                provider=choice.provider_name,
            )
            await self._append_session_entry(entry)
        except BaseException:
            if candidate is not None:
                await candidate.aclose()
            raise

        # From this point the transcript is authoritative. Only synchronous
        # assignments happen before control returns to the frontend.
        #
        # 从此处开始，会话记录具有权威性。在控制权返回前端之前只会发生同步赋值。
        old_provider = self._harness.config.provider
        assert candidate is not None
        self._owned_providers.append(candidate)
        self._harness.config.provider = candidate
        self._harness.config.model = choice.model
        self._provider_name = choice.provider_name
        self._inference_provider = selected_inference
        self._inference_provider_mode = selected_inference_mode
        self._runtime_provider_config = selected_config
        self._config = replace(
            self._config,
            provider=candidate,
            model=choice.model,
            provider_name=choice.provider_name,
            inference_provider=selected_inference,
            inference_provider_mode=selected_inference_mode,
            runtime_provider_config=selected_config,
            dynamic_provider=selected_dynamic,
        )
        self._thinking_level = selected_thinking
        self._image_support.supported = selected_image_support
        self._last_parent_id = entry.id
        self._invalidate_runtime_model_limits()
        self._invalidate_context_usage_cache()
        try:
            await self._refresh_persisted_state(leaf_id=entry.id)
        except Exception as exc:  # committed history wins over repairable index failure
            # 已提交历史优先于可修复的索引失败。
            self._resource_diagnostics = (
                *self._resource_diagnostics,
                ResourceDiagnostic(
                    kind="session-index",
                    message=f"Committed model switch requires index repair: {type(exc).__name__}",
                    severity="warning",
                ),
            )
        if old_provider is not candidate:
            with suppress(Exception):
                await self._close_replaced_provider(old_provider)
        if selected_config is not None:
            self._persist_default_model_choice()
        return ModelSelectionResult(choice, changed=True)

    def set_inference_provider(self, route: str | None) -> str:
        """Select or reset the active Hugging Face session route.

        选择或重置活动的 Hugging Face 会话路由。
        """
        if self.provider_name != "huggingface":
            raise ProviderConfigError(
                "Inference-provider routing requires the huggingface provider"
            )
        normalized = validate_huggingface_inference_provider(route) if route is not None else None
        mode: InferenceProviderMode = "fixed" if normalized is not None else "automatic"
        provider, provider_config = self._build_runtime_provider(
            inference_provider=normalized,
        )
        self._owned_providers.append(provider)
        if self._config.session_manager is not None and self._config.session_id is not None:
            self._config.session_manager.touch_session(
                self._config.session_id,
                model=self.model,
                provider_name=self.provider_name,
                inference_provider=normalized,
                inference_provider_mode=mode,
                preserve_inference_provider=False,
            )
        self._inference_provider = normalized
        self._inference_provider_mode = mode
        self._config = replace(
            self._config,
            inference_provider=normalized,
            inference_provider_mode=mode,
        )
        self._activate_runtime_provider(provider, provider_config)
        return normalized or "automatic (will pin after the next successful response)"

    def set_model_choice(self, choice: ModelChoice) -> None:
        """Switch provider/model as one operation.

        在一次操作中切换提供者和模型。
        """
        if choice.provider_name == self.provider_name:
            self.set_model(choice.model)
            return
        self._set_provider_model(choice.provider_name, choice.model)

    def is_scoped_model(self, choice: ModelChoice) -> bool:
        """Return whether a provider/model pair is in the scoped model list.

        返回提供者与模型组合是否位于限定模型列表中。
        """
        return choice in self.scoped_model_choices

    def toggle_scoped_model(self, choice: ModelChoice) -> tuple[ModelChoice, ...]:
        """Add or remove a model from the persisted scoped model list.

        在持久化的限定模型列表中添加或移除模型。
        """
        if self._provider_settings is None:
            raise ProviderConfigError("Provider settings are not available for this session")
        available = set(self.available_model_choices)
        existing = choice in self.scoped_model_choices
        durable_settings = getattr(self, "_durable_provider_settings", self._provider_settings)
        effective = self._provider_registry.effective(choice.provider_name)
        if effective is not None and isinstance(effective.definition, DynamicProvider):
            if not (
                self._effective_stable_dynamic_scoped_provider(choice.provider_name)
                or (existing and self._stable_dynamic_scoped_provider(choice.provider_name))
            ):
                raise ProviderConfigError(
                    "Only effective trusted built-in dynamic providers support scoped references"
                )
            if choice not in available and not existing:
                raise ProviderConfigError(
                    f"Model is unavailable: {choice.provider_name}:{choice.model}"
                )
            toggle = toggle_saved_stable_scoped_model
        else:
            if choice not in available:
                raise ProviderConfigError(
                    f"Model is not available: {choice.provider_name}:{choice.model}"
                )
            durable_provider = (
                durable_settings.get_provider(choice.provider_name)
                if durable_settings is not None
                else None
            )
            toggle = (
                toggle_saved_scoped_model
                if durable_provider is not None and choice.model in durable_provider.models
                else toggle_saved_stable_scoped_model
            )

        updated_settings = toggle(
            provider_name=choice.provider_name,
            model=choice.model,
            paths=self._resource_paths.paths,
            fallback_settings=durable_settings,
        )
        if hasattr(self, "_durable_provider_settings"):
            self._durable_provider_settings = updated_settings
            self._apply_runtime_model_catalogs()
        else:  # narrowly supports lightweight host/test session doubles
            # 仅用于有限支持轻量宿主或测试会话替身。
            self._provider_settings = updated_settings
        self._sync_thinking_level_to_active_model()
        return self.scoped_model_choices

    def cycle_scoped_model(self, *, reverse: bool = False) -> ModelChoice:
        """Switch to the next currently available configured scoped model.

        切换到下一个当前可用且已配置的限定模型。
        """
        available = set(self.available_model_choices)
        scoped = tuple(choice for choice in self.scoped_model_choices if choice in available)
        if not scoped:
            raise ProviderConfigError("No scoped models configured.")
        current = ModelChoice(provider_name=self.provider_name, model=self.model)
        try:
            current_index = scoped.index(current)
        except ValueError:
            current_index = -1 if not reverse else 0
        delta = -1 if reverse else 1
        choice = scoped[(current_index + delta) % len(scoped)]
        self.set_model_choice(choice)
        return choice

    def set_provider(self, provider_name: str, *, persist_default: bool = True) -> None:
        """Switch the active provider and reset to that provider's default model.

        切换活动提供者，并重置为该提供者的默认模型。
        """
        if self._provider_settings is None:
            raise ProviderConfigError("Provider settings are not available for this session")
        provider_config = self._provider_settings.get_provider(provider_name)
        self._set_provider_model(
            provider_name,
            provider_config.default_model,
            persist_default=persist_default,
        )

    def _set_provider_model(
        self,
        provider_name: str,
        model: str,
        *,
        persist_default: bool = True,
    ) -> None:
        """Switch active provider/model without constructing an intermediate provider.

        切换活动提供者和模型，而不构造中间提供者。
        """
        if self._provider_settings is None:
            raise ProviderConfigError("Provider settings are not available for this session")

        provider_config = self._provider_settings.get_provider(provider_name)
        if model not in provider_config.models:
            raise ProviderConfigError(f"Model is not configured: {provider_name}:{model}")
        thinking_level = _coerced_thinking_level(
            provider_config,
            model=model,
            current=self._thinking_level,
        )
        try:
            provider = _create_runtime_provider(
                provider_config,
                credential_store=self._credential_store,
                model=model,
                thinking_level=thinking_level,
                inference_provider=_configured_inference_provider(provider_config, model),
                response_headers_observer=(
                    self._observe_response_headers
                    if provider_config.name == "huggingface"
                    else None
                ),
            )
        except RuntimeError as exc:
            raise ProviderConfigError(str(exc)) from exc
        self._owned_providers.append(provider)
        self._harness.config.provider = provider
        self._provider_name = provider_config.name
        self._inference_provider = _configured_inference_provider(provider_config, model)
        self._inference_provider_mode = _configured_inference_provider_mode(provider_config, model)
        self._runtime_provider_config = provider_config
        self._invalidate_runtime_model_limits()
        self._harness.config.model = model
        self._thinking_level = thinking_level
        self._sync_image_support()
        if persist_default:
            self._persist_default_model_choice()
        if self._config.session_id is not None and self._config.session_manager is not None:
            self._config.session_manager.touch_session(
                self._config.session_id,
                model=model,
                provider_name=self.provider_name,
                inference_provider=self._inference_provider,
                inference_provider_mode=self._inference_provider_mode,
                preserve_inference_provider=False,
            )

    async def set_thinking_level(self, level: str) -> str:
        """Persist and activate a thinking mode for future turns.

        持久化并激活后续轮次使用的思考模式。
        """
        normalized = normalize_thinking_level(level)
        available = self.available_thinking_levels
        if not available:
            raise ValueError(_unavailable_thinking_message(self))
        if normalized not in available:
            modes = ", ".join(available)
            raise ValueError(
                f"Thinking mode {normalized} is not available for "
                f"{self._provider_name}:{self.model}. Available modes: {modes}"
            )
        if normalized == self._thinking_level:
            return f"Thinking mode: {normalized}"

        previous = self._thinking_level
        self._thinking_level = normalized
        try:
            self._refresh_runtime_provider()
        except ProviderConfigError:
            self._thinking_level = previous
            raise

        entry = ThinkingLevelChangeEntry(
            parent_id=self._last_parent_id,
            thinking_level=normalized,
        )
        await self._append_session_entry(entry)
        self._last_parent_id = entry.id

        self._persist_thinking_level_choice()
        await self._refresh_persisted_state(leaf_id=entry.id)
        await self._extension_runtime.emit_event(ThinkingLevelChangedEvent(level=normalized))
        return f"Thinking mode: {normalized}"

    async def cycle_thinking_level(self) -> str:
        """Cycle to the next supported thinking mode and persist it.

        循环切换到下一个受支持的思考模式并将其持久化。
        """
        return await self.set_thinking_level(
            next_thinking_level(
                self._thinking_level,
                available=self.available_thinking_levels,
            )
        )

    def _active_provider_config(self) -> ProviderConfig | None:
        """Return the effective configuration for the active provider.

        返回活动提供者的有效配置。
        """
        if self._provider_settings is None:
            return None
        try:
            provider = self._provider_settings.get_provider(self._provider_name)
        except ProviderConfigError:
            return None
        runtime = self._runtime_provider_config
        if (
            isinstance(provider, OpenAICodexProviderConfig)
            and self.model not in provider.models
            and runtime is not None
            and runtime.name == provider.name
            and self.model in runtime.models
        ):
            # Picker visibility must not invalidate an already selected runtime.
            #
            # 选择器可见性不能使已选定的运行时失效。
            return runtime
        return provider

    def _apply_thinking_level_override(self) -> None:
        """Apply the one-shot startup thinking override to the loaded session.

        将一次性启动思考覆盖值应用到已加载会话。

        Runs once during :meth:`load`, after session state is replayed and
        before the level is synced to the active model, so the override wins
        over both remembered defaults and the resumed transcript state. The
        override is validated strictly against either the active dynamic model
        or the durable provider configuration when its capabilities are known.

        此操作在 :meth:`load` 期间执行一次，位于会话状态重放之后、思考等级与
        活动模型同步之前，因此覆盖值优先于记忆默认值和恢复的会话记录状态。
        当能力已知时，会严格依据活动动态模型或持久化提供者配置验证覆盖值。
        """
        override = self._config.thinking_level_override
        if override is None:
            return
        dynamic = self._active_dynamic_provider
        if dynamic is not None:
            model = self._active_dynamic_model
            levels = model.thinking_levels if model is not None else None
            if not levels:
                raise ProviderConfigError(
                    f"Thinking modes are unavailable for {dynamic.id}:{self.model}"
                )
            if override not in levels:
                allowed = ", ".join(levels)
                raise ProviderConfigError(
                    f'Thinking mode "{override}" is not available for '
                    f"{dynamic.id}:{self.model}. Available modes: {allowed}"
                )
            self._thinking_level = override
            return
        provider = self._active_provider_config()
        if provider is None:
            self._thinking_level = override
            return
        resolved = resolve_startup_thinking_level(provider, self.model, cli_override=override)
        if resolved is not None:
            self._thinking_level = resolved

    def _sync_thinking_level_to_active_model(self) -> None:
        """Clamp the current thinking level to active model capabilities.

        根据活动模型能力调整当前思考等级。
        """
        provider = self._active_provider_config()
        if provider is None:
            return
        self._thinking_level = _coerced_thinking_level(
            provider,
            model=self.model,
            current=self._thinking_level,
            preferred=provider.thinking_defaults.get(self.model),
        )

    def _sync_image_support(self) -> None:
        """Synchronize image-tool support with the active model.

        将图像工具支持状态与活动模型同步。
        """
        provider = self._active_provider_config() or self._runtime_provider_config
        self._image_support.supported = (
            provider_model_supports_images(provider, self.model) if provider is not None else None
        )

    def _persist_default_model_choice(self) -> None:
        """Persist the active provider and model as the user default.

        将活动提供者和模型持久化为用户默认值。
        """
        if self._durable_provider_settings is None:
            return
        durable_provider = self._durable_provider_settings.get_provider(self.provider_name)
        if self.model not in durable_provider.models:
            # Account-specific inventory is deliberately not written into catalog.toml.
            #
            # 账户专属清单有意不写入 catalog.toml。
            return
        self._durable_provider_settings = save_default_provider_model(
            provider_name=self.provider_name,
            model=self.model,
            paths=self._resource_paths.paths,
            fallback_settings=self._durable_provider_settings,
        )
        self._apply_runtime_model_catalogs()
        self._sync_thinking_level_to_active_model()

    def _persist_thinking_level_choice(self) -> None:
        """Persist the active model's selected thinking level.

        持久化活动模型选定的思考等级。
        """
        if self._provider_settings is None:
            return
        provider = self._active_provider_config()
        if provider is None or self._thinking_level not in provider_thinking_levels(
            provider,
            model=self.model,
        ):
            return
        try:
            self._durable_provider_settings = save_provider_thinking_level(
                provider_name=self.provider_name,
                model=self.model,
                thinking_level=self._thinking_level,
                paths=self._resource_paths.paths,
                fallback_settings=self._durable_provider_settings,
            )
            self._apply_runtime_model_catalogs()
        except ProviderConfigError:
            return

    def _observe_response_headers(self, headers: Mapping[str, str]) -> None:
        """Update runtime model limits from provider response headers.

        根据提供者响应头更新运行时模型限制。
        """
        if (
            self.provider_name != "huggingface"
            or self._inference_provider_mode != "automatic"
            or self._inference_provider is not None
        ):
            return
        route = next(
            (value for key, value in headers.items() if key.casefold() == "x-inference-provider"),
            None,
        )
        if route is None:
            return
        try:
            route = validate_huggingface_inference_provider(route)
        except ProviderConfigError:
            return
        provider, provider_config = self._build_runtime_provider(
            inference_provider=route,
        )
        # Track staged providers immediately so a later index-write failure does
        # not leak a provider-owned client. The active runtime remains unchanged.
        #
        # 立即跟踪暂存提供者，以免后续索引写入失败导致提供者拥有的客户端泄漏。
        # 活动运行时保持不变。
        self._owned_providers.append(provider)
        if self._config.session_manager is not None and self._config.session_id is not None:
            self._config.session_manager.touch_session(
                self._config.session_id,
                model=self.model,
                provider_name=self.provider_name,
                inference_provider=route,
                inference_provider_mode="automatic",
                preserve_inference_provider=False,
            )
        self._inference_provider = route
        self._config = replace(
            self._config,
            inference_provider=route,
            inference_provider_mode="automatic",
        )
        self._activate_runtime_provider(provider, provider_config)

    def will_auto_retry(self, message: AssistantMessage) -> bool:
        """Return whether session orchestration will retry this assistant error.

        返回会话编排是否会重试此助手错误。
        """
        return is_context_overflow_error(message) or self._should_auto_failover_huggingface_route(
            message
        )

    # Return whether an assistant failure qualifies for automatic route failover.
    #
    # 返回助手失败是否符合自动路由故障转移条件。
    def _should_auto_failover_huggingface_route(
        self,
        message: AssistantMessage,
    ) -> bool:
        return (
            self.provider_name == "huggingface"
            and self._inference_provider_mode == "automatic"
            and self._inference_provider is not None
            and is_retryable_huggingface_route_error(message)
        )

    def _reset_automatic_inference_provider_for_failover(self) -> str:
        """Reset automatic Hugging Face routing and return the failed route.

        重置自动 Hugging Face 路由并返回失败路由。
        """
        failed_route = self._inference_provider
        if failed_route is None:
            raise ProviderConfigError("Hugging Face failover requires a pinned route")
        provider, provider_config = self._build_runtime_provider(inference_provider=None)
        self._owned_providers.append(provider)
        if self._config.session_manager is not None and self._config.session_id is not None:
            self._config.session_manager.touch_session(
                self._config.session_id,
                model=self.model,
                provider_name=self.provider_name,
                inference_provider=None,
                inference_provider_mode="automatic",
                preserve_inference_provider=False,
            )
        self._inference_provider = None
        self._config = replace(
            self._config,
            inference_provider=None,
            inference_provider_mode="automatic",
        )
        self._activate_runtime_provider(provider, provider_config)
        return failed_route

    # Execute one automatic Hugging Face route failover and retry sequence.
    #
    # 执行一次 Hugging Face 自动路由故障转移和重试序列。
    async def _run_huggingface_route_failover(
        self,
        *,
        context: AgentCallDiagnosticContext,
    ) -> AsyncIterator[CodingSessionEvent]:
        """Retry the interrupted run once through unsuffixed Hugging Face routing.

        通过不带后缀的 Hugging Face 路由重试一次中断的运行。
        """
        failed_route = self._reset_automatic_inference_provider_for_failover()
        retry_start = AutoRetryStartEvent(
            attempt=1,
            max_attempts=1,
            delay_ms=0,
            error_message=f"Hugging Face route {failed_route} failed; rerouting automatically",
        )
        await self._extension_runtime.emit_event(retry_start)
        yield retry_start

        retry_events = self._harness.continue_()
        self._invalidate_context_usage_cache()
        final_error: str | None = None
        try:
            async for retry_event in retry_events:
                if isinstance(retry_event, ToolExecutionEndEvent):
                    self._invalidate_context_usage_cache()
                if (
                    isinstance(retry_event, MessageEndEvent)
                    and isinstance(retry_event.message, AssistantMessage)
                    and retry_event.message.stop_reason in {"error", "aborted"}
                ):
                    final_error = retry_event.message.error_message or "Provider request aborted"
                    if retry_event.message.stop_reason == "error":
                        self._last_diagnostic_log_path = (
                            self._diagnostic_logger.log_assistant_error(
                                context=context,
                                phase="agent_loop_route_failover",
                                message=retry_event.message,
                            )
                        )
                if isinstance(retry_event, AgentEndEvent):
                    yield SessionAgentEndEvent(
                        messages=retry_event.messages,
                        will_retry=False,
                    )
                else:
                    yield retry_event
        finally:
            aclose = getattr(retry_events, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()

        failover_succeeded = final_error is None
        self._last_diagnostic_log_path = self._diagnostic_logger.log_huggingface_route_failover(
            context=context,
            failed_route=failed_route,
            replacement_route=self._inference_provider,
            success=failover_succeeded,
            error_message=final_error,
        )
        retry_end = AutoRetryEndEvent(
            success=failover_succeeded,
            attempt=1,
            final_error=final_error,
        )
        await self._extension_runtime.emit_event(retry_end)
        yield retry_end

    # Construct a runtime provider for the selected configuration.
    #
    # 为选定配置构造运行时提供者。
    def _build_runtime_provider(
        self,
        *,
        inference_provider: str | None,
    ) -> tuple[ClosableModelProvider, ProviderConfig]:
        if self._runtime_provider_config is None:
            raise ProviderConfigError("Runtime provider configuration is unavailable")
        provider_config = self._active_provider_config() or self._runtime_provider_config
        validate_provider_model(provider_config, self.model)
        try:
            provider = _create_runtime_provider(
                provider_config,
                credential_store=self._credential_store,
                model=self.model,
                thinking_level=self._thinking_level,
                inference_provider=inference_provider,
                response_headers_observer=(
                    self._observe_response_headers
                    if provider_config.name == "huggingface"
                    else None
                ),
            )
        except RuntimeError as exc:
            raise ProviderConfigError(str(exc)) from exc
        return provider, provider_config

    # Install a runtime provider into the active harness.
    #
    # 将运行时提供者安装到活动代理核心。
    def _activate_runtime_provider(
        self,
        provider: ClosableModelProvider,
        provider_config: ProviderConfig,
    ) -> None:
        self._harness.config.provider = provider
        self._runtime_provider_config = provider_config
        self._invalidate_runtime_model_limits()

    def _refresh_runtime_provider(self) -> None:
        """Rebuild the active runtime provider from current settings.

        根据当前设置重建活动运行时提供者。
        """
        if self._runtime_provider_config is None:
            return
        provider, provider_config = self._build_runtime_provider(
            inference_provider=self._inference_provider,
        )
        self._owned_providers.append(provider)
        self._activate_runtime_provider(provider, provider_config)

    def _invalidate_runtime_model_limits(self) -> None:
        """Clear cached runtime model-limit discovery state.

        清除缓存的运行时模型限制发现状态。
        """
        self._runtime_model_limits = None
        self._runtime_model_limits_key = None
        self._model_limits_discovery_error = None

    async def _refresh_runtime_model_limits(self) -> None:
        """Discover and cache limits for the active runtime model.

        发现并缓存活动运行时模型的限制。
        """
        key = (self.provider_name, self.model)
        if self._runtime_model_limits_key == key:
            return
        self._runtime_model_limits = None
        self._runtime_model_limits_key = key
        self._model_limits_discovery_error = None
        provider = self._harness.config.provider
        if (
            self.provider_name == "openai-codex"
            and isinstance(provider, ModelCatalogProvider)
            and environ.get("TAU_OFFLINE") is not None
        ):
            return
        try:
            cached_catalog = self._runtime_model_catalogs.get(self.provider_name)
            if self.provider_name == "openai-codex" and cached_catalog is not None:
                model = cached_catalog.model(self.model)
                self._runtime_model_limits = model.limits if model is not None else None
                return
            if isinstance(provider, ModelCatalogProvider):
                catalog = await provider.discover_models()
                self._publish_runtime_model_catalog(self.provider_name, catalog)
                if self.provider_name == "openai-codex":
                    self._persist_codex_model_catalog(provider, catalog)
            if isinstance(provider, ModelLimitsProvider):
                self._runtime_model_limits = await provider.discover_model_limits(self.model)
        except Exception as exc:  # noqa: BLE001 - static catalog remains the safe fallback
            # 静态目录仍是安全的后备方案。
            error = f"{type(exc).__name__}: {exc}"
            self._model_limits_discovery_error = error
            if isinstance(provider, ModelCatalogProvider):
                self._model_catalog_discovery_errors[self.provider_name] = error

    async def reload(self) -> CodingReloadSummary:
        """Stage and atomically publish a complete replacement snapshot.

        暂存并原子发布完整的替换快照。
        """
        self._require_idle("reload")
        before_skills = _skill_signatures(self._skills)
        before_prompt_templates = _prompt_template_signatures(self._prompt_templates)
        before_context_files = _context_file_signatures(self._context_files)
        before_diagnostics = _diagnostic_signatures(self.resource_diagnostics)
        before_system_prompt_inputs = _system_prompt_resource_signatures(
            skills=self._skills,
            context_files=self._context_files,
            custom_system_prompt=self._custom_system_prompt,
            custom_system_prompt_path=self._custom_system_prompt_path,
            append_system_prompt=self._append_system_prompt,
            append_system_prompt_paths=self._append_system_prompt_paths,
        )
        before_extensions = _extension_signatures(self._extension_runtime)
        before_tool_names = tuple(tool.name for tool in self._harness.config.tools)
        before_guidelines = self._extension_runtime.prompt_guidelines
        before_sections = self._extension_runtime.sourced_prompt_sections

        # Nothing below mutates the live session. Eligible extensions are loaded
        # first so project code cannot import before the destination decision.
        #
        # 下方操作不会改变实时会话。先加载符合条件的扩展，确保项目代码不会在
        # 目标决策前导入。
        unfiltered_paths = resource_paths_with_cwd(self._config.resource_paths, self.cwd)
        previous_ui = self._extension_runtime.ui
        staged_runtime = ExtensionRuntime(
            durable_providers=(
                self._provider_settings.providers if self._provider_settings else ()
            ),
            credentials=self._extension_runtime.provider_credentials or self._credential_store,
            environment=self._extension_runtime.provider_environment,
            built_in_credentials=self._extension_runtime.built_in_credentials,
            built_in_http_client=self._extension_runtime.built_in_http_client,
            paths=(
                self._resource_paths.paths
                or TauPaths(
                    home=self._resource_paths.root,
                    agents_home=self._resource_paths.agents_root or Path.home() / ".agents",
                )
            ),
        )
        staged_runtime.load(
            unfiltered_paths,
            extra_paths=self._config.extension_paths,
            include_resource_dirs=self._config.extensions_enabled,
            include_project_dir=False,
        )

        staged_resolution = self._project_trust_resolution
        staged_paths = self._resource_paths
        coordinator = self._config.project_trust_coordinator
        trust_summary = None
        if coordinator is not None:
            trust_summary, staged_resolution = await coordinator.resolve(
                self.cwd,
                override=self._config.trust_override,
                default=self._config.trust_default,
                interactive=self._config.trust_interactive,
                prompt=self._config.trust_prompt,
                extension_deciders=(staged_runtime.decide_project_trust,),
                refresh=True,
                cache_result=False,
            )
            if staged_resolution.cancelled:
                raise ValueError("Project trust decision cancelled; keeping current resources")
            staged_paths = resource_paths_with_project_trust(
                resource_paths_with_cwd(self._config.resource_paths, trust_summary.cwd.value),
                trusted=staged_resolution.trusted,
            )

        resources = _load_session_resources(
            staged_paths,
            self._config.context_files,
            skills_enabled=self._config.skills_enabled,
            system_prompt_enabled=self._config.system is None,
            custom_system_prompt_explicit=self._config.custom_system_prompt is not None,
        )
        if trust_summary is not None and trust_summary.categories:
            assert staged_resolution is not None
            resources = replace(
                resources,
                diagnostics=(
                    *resources.diagnostics,
                    ResourceDiagnostic(
                        kind="project-trust",
                        message=format_trust_diagnostic(trust_summary, staged_resolution),
                        severity="info" if staged_resolution.trusted else "warning",
                    ),
                ),
            )
        if (
            staged_resolution is not None
            and staged_resolution.trusted
            and self._config.project_extensions_enabled
        ):
            staged_runtime.load(
                staged_paths,
                include_resource_dirs=True,
                include_project_dir=True,
                include_user_dir=False,
            )

        base_tools = (
            self._config.tools
            if self._config.tools is not None
            else create_coding_tools(
                cwd=self._config.cwd,
                shell_command_prefix=self._config.shell_command_prefix,
                image_support=self._image_support,
            )
        )
        staged_tools = staged_runtime.compose_tools(base_tools)
        staged_commands = self._config.command_registry or staged_runtime.build_command_registry()
        after_system_prompt_inputs = _system_prompt_resource_signatures(
            skills=resources.skills,
            context_files=resources.context_files,
            custom_system_prompt=resources.custom_system_prompt,
            custom_system_prompt_path=resources.custom_system_prompt_path,
            append_system_prompt=resources.append_system_prompt,
            append_system_prompt_paths=resources.append_system_prompt_paths,
        )
        after_guidelines = staged_runtime.prompt_guidelines
        after_sections = staged_runtime.sourced_prompt_sections
        system_prompt_rebuilt = self._config.system is None and (
            before_system_prompt_inputs != after_system_prompt_inputs
            or before_tool_names != tuple(tool.name for tool in staged_tools)
            or before_guidelines != after_guidelines
            or before_sections != after_sections
        )
        staged_system = self._harness.config.system
        if system_prompt_rebuilt:
            staged_system = build_system_prompt(
                BuildSystemPromptOptions(
                    cwd=self._config.cwd,
                    tools=staged_tools,
                    skills=resources.skills,
                    custom_prompt=(
                        self._config.custom_system_prompt
                        if self._config.custom_system_prompt is not None
                        else resources.custom_system_prompt
                    ),
                    append_sections=_append_prompt_sections(
                        resources.append_system_prompts,
                        resources.append_system_prompt_paths,
                        self._config.append_system_prompt,
                    ),
                    context_files=resources.context_files,
                    extra_guidelines=after_guidelines,
                    extra_sections=after_sections,
                    custom_prompt_source=_custom_prompt_source(
                        explicit=self._config.custom_system_prompt is not None,
                        path=resources.custom_system_prompt_path,
                    ),
                )
            )

        # Cancellable lifecycle work stays before the publication boundary. The
        # staged runtime may inspect the still-live session during session_start;
        # cancellation leaves that prior snapshot/runtime/cache untouched.
        #
        # 可取消的生命周期工作位于发布边界之前。暂存运行时可在 session_start
        # 期间检查仍然活动的会话；取消操作会保持先前的快照、运行时和缓存不变。
        old_runtime = self._extension_runtime
        await old_runtime.emit_session_shutdown("reload")
        old_runtime.clear_ui_components()
        staged_runtime.set_ui_bridge(previous_ui)
        staged_runtime.bind(self)
        await staged_runtime.emit_session_start("reload")

        # Publication is synchronous: cancellation can no longer report failure
        # after only part of the live snapshot or trust cache was adopted.
        #
        # 发布过程是同步的：不能在仅接纳部分实时快照或信任缓存后再报告取消失败。
        if coordinator is not None and trust_summary is not None:
            assert staged_resolution is not None
            coordinator.commit(trust_summary.cwd, staged_resolution)
        old_runtime.retire()
        self._resource_paths = staged_paths
        self._config = replace(
            self._config,
            cwd=staged_paths.cwd or self._config.cwd,
            resource_paths=staged_paths,
            extension_runtime=staged_runtime,
        )
        self._project_trust_resolution = staged_resolution
        self._skills = resources.skills
        self._prompt_templates = resources.prompt_templates
        self._context_files = resources.context_files
        self._custom_system_prompt = resources.custom_system_prompt
        self._custom_system_prompt_path = resources.custom_system_prompt_path
        self._append_system_prompt = resources.append_system_prompt
        self._append_system_prompts = resources.append_system_prompts
        self._append_system_prompt_paths = resources.append_system_prompt_paths
        self._resource_diagnostics = resources.diagnostics
        self._command_registry = staged_commands
        self._extension_runtime = staged_runtime
        self._provider_registry = staged_runtime.provider_registry
        self._harness.config.tools = staged_tools
        self._harness.config.system = staged_system
        if system_prompt_rebuilt:
            self._invalidate_context_usage_cache()
        staged_runtime.attach_harness_listener(self._harness.subscribe)
        # Retirement invalidates publication synchronously; async close then
        # waits for cooperative provider callback cleanup or reports bounded
        # containment while the outgoing runtime still owns every task handle.
        # Caller cancellation at this committed seam is contained so reload
        # cannot report failure after the fresh snapshot became active.
        #
        # 退役操作会同步撤销发布；随后异步关闭会等待协作式提供者回调清理，
        # 或在退出运行时仍拥有全部任务句柄时报告有界隔离。调用方在这个已提交
        # 接口处的取消会被隔离，因此重新加载不会在新快照生效后报告失败。
        await _finish_adopted_runtime_close(old_runtime)

        return CodingReloadSummary(
            skills=_category_summary(before_skills, _skill_signatures(resources.skills)),
            prompt_templates=_category_summary(
                before_prompt_templates, _prompt_template_signatures(resources.prompt_templates)
            ),
            context_files=_category_summary(
                before_context_files, _context_file_signatures(resources.context_files)
            ),
            extensions=_category_summary(before_extensions, _extension_signatures(staged_runtime)),
            diagnostics=_category_summary(
                before_diagnostics, _diagnostic_signatures(self.resource_diagnostics)
            ),
            system_prompt_rebuilt=system_prompt_rebuilt,
        )

    async def refresh_model_catalogs(self, *, force: bool = False) -> ModelsDevRefreshResult:
        """Refresh public and authenticated catalogs and publish them to this session.

        刷新公开及已认证目录，并将其发布到当前会话。
        """
        public_error: Exception | None = None
        result: ModelsDevRefreshResult | None = None
        try:
            result = await refresh_models_dev_catalog(
                paths=self._resource_paths.paths,
                force=force,
            )
            self.reload_provider_settings()
        except Exception as error:  # noqa: BLE001 - authenticated refresh still runs
            # 即使公开刷新失败，仍继续执行已认证刷新。
            public_error = error
        await self._refresh_codex_model_catalog()
        if public_error is not None:
            raise public_error
        assert result is not None
        return result

    async def _refresh_codex_model_catalog(self) -> None:
        """Refresh the account-specific Codex inventory without making it durable.

        刷新账户专属的 Codex 清单，但不将其持久化。
        """
        if environ.get("TAU_OFFLINE") is not None or self._durable_provider_settings is None:
            return
        try:
            provider_config = self._durable_provider_settings.get_provider("openai-codex")
        except ProviderConfigError:
            return
        if not isinstance(provider_config, OpenAICodexProviderConfig):
            return
        if not self._provider_is_usable(provider_config):
            return

        # Use a fresh provider even when Codex is active. The active provider caches
        # its startup snapshot so reusing it would make `/model` unable to discover
        # versions or models released during a long-running Tau process.
        #
        # 即使 Codex 当前处于活动状态，也使用全新提供者。活动提供者会缓存其
        # 启动快照，复用它会导致 `/model` 无法发现 Tau 长期运行期间发布的版本或模型。
        temporary_provider = create_model_provider(
            provider_config,
            credential_store=self._credential_store,
            model=None,
            thinking_level=None,
        )
        if not isinstance(temporary_provider, ModelCatalogProvider):
            await temporary_provider.aclose()
            return
        try:
            catalog = await temporary_provider.discover_models()
            self._publish_runtime_model_catalog(provider_config.name, catalog)
            self._persist_codex_model_catalog(temporary_provider, catalog, provider_config)
        except Exception as exc:  # noqa: BLE001 - static catalog remains the safe fallback
            # 静态目录仍是安全的后备方案。
            self._model_catalog_discovery_errors[provider_config.name] = (
                f"{type(exc).__name__}: {exc}"
            )
        finally:
            await temporary_provider.aclose()

    def _persist_codex_model_catalog(
        self,
        provider: object,
        catalog: RuntimeModelCatalog,
        provider_config: OpenAICodexProviderConfig | None = None,
    ) -> None:
        """Persist an authenticated Codex catalog for the current account.

        为当前账户持久化已认证的 Codex 目录。
        """
        config = provider_config or (
            self._runtime_provider_config
            if isinstance(self._runtime_provider_config, OpenAICodexProviderConfig)
            else None
        )
        if config is None:
            return
        account_id = getattr(provider, "account_id", None) or _codex_account_id(
            config,
            self._credential_store,
        )
        if not isinstance(account_id, str) or not account_id:
            return
        with suppress(OSError, TypeError, ValueError):
            save_codex_model_catalog(
                catalog,
                account_id=account_id,
                paths=self._resource_paths.paths,
            )

    def _publish_runtime_model_catalog(
        self,
        provider_name: str,
        catalog: RuntimeModelCatalog,
    ) -> None:
        """Publish one refreshed runtime catalog into session state.

        将一个刷新的运行时目录发布到会话状态。
        """
        if not catalog.models:
            raise ValueError("provider returned an empty model catalog")
        self._runtime_model_catalogs[provider_name] = catalog
        self._model_catalog_discovery_errors.pop(provider_name, None)
        self._invalidate_runtime_model_limits()
        self._apply_runtime_model_catalogs()

    def _apply_runtime_model_catalogs(self) -> None:
        """Overlay runtime catalogs onto effective provider settings.

        将运行时目录叠加到有效提供者设置上。
        """
        settings = self._durable_provider_settings
        if settings is None:
            self._provider_settings = None
            return
        providers = tuple(
            _provider_with_runtime_model_catalog(
                provider,
                self._runtime_model_catalogs.get(provider.name),
            )
            for provider in settings.providers
        )
        self._provider_settings = replace(settings, providers=providers)

    def reload_provider_settings(self) -> None:
        """Reload provider settings for login and model-selection flows.

        为登录和模型选择流程重新加载提供者设置。
        """
        if self._provider_settings is None:
            return
        previous_settings = self._provider_settings
        previous_durable_settings = self._durable_provider_settings
        previous_thinking_level = self._thinking_level
        self._durable_provider_settings = load_provider_settings(self._resource_paths.paths)
        self._apply_runtime_model_catalogs()
        try:
            self._sync_thinking_level_to_active_model()
            self._refresh_runtime_provider()
            self._sync_image_support()
        except ProviderConfigError:
            self._provider_settings = previous_settings
            self._durable_provider_settings = previous_durable_settings
            self._thinking_level = previous_thinking_level
            raise

    async def resume(self, session_id: str) -> str:
        """Replace this session's active state with another indexed session.

        使用另一个已索引会话替换当前会话的活动状态。
        """
        self._require_idle("resume")
        await self._flush_pending_message_writes(context=self._diagnostic_context())
        manager = self._config.session_manager
        if manager is None:
            raise ValueError("Session manager is not available")
        record = manager.get_session(session_id)
        if record is None:
            raise ValueError(f"Unknown session: {session_id}")

        provider_name = self._provider_name
        runtime_provider_config = self._runtime_provider_config
        model = self.model
        restore_record_model = False
        dynamic_resume = False
        if record.provider_name:
            effective = self._provider_registry.effective(record.provider_name)
            if effective is not None and isinstance(effective.definition, DynamicProvider):
                # Dynamic definitions are generation-local. Re-resolve the
                # reference in the fresh destination runtime instead of
                # carrying this runtime's provider object across cwd/trust
                # boundaries.
                #
                # 动态定义只在当前代有效。应在全新的目标运行时中重新解析引用，
                # 而不是跨工作目录或信任边界携带当前运行时的提供者对象。
                provider_name = record.provider_name
                model = record.model
                runtime_provider_config = None
                dynamic_resume = True
                restore_record_model = True
            else:
                provider_name = record.provider_name
                model = record.model
                restore_record_model = True
                if self._provider_settings is None:
                    # The destination runtime may provide a process-local
                    # definition even when the source session did not.
                    #
                    # 即使源会话没有定义，目标运行时也可能提供进程本地定义。
                    dynamic_resume = True
                    runtime_provider_config = None
                else:
                    try:
                        runtime_provider_config = self._provider_settings.get_provider(
                            record.provider_name
                        )
                    except ProviderConfigError:
                        # Do not reject a dynamic overlay merely because the
                        # current cwd has not loaded its destination extension.
                        #
                        # 不要仅因当前工作目录尚未加载目标扩展就拒绝动态覆盖。
                        dynamic_resume = True
                        runtime_provider_config = None
                    else:
                        if (
                            isinstance(runtime_provider_config, OpenAICodexProviderConfig)
                            and model not in runtime_provider_config.models
                        ):
                            # Re-discover a live-only destination model before validation.
                            #
                            # 在验证前重新发现仅实时存在的目标模型。
                            dynamic_resume = True
                            runtime_provider_config = None
                        else:
                            validate_provider_model(runtime_provider_config, model)

        replacement = await type(self).load(
            CodingSessionConfig(
                provider=None if dynamic_resume else self._harness.config.provider,
                model=model,
                cwd=record.cwd,
                storage=jsonl_session_storage(record.path),
                system=self._config.system,
                custom_system_prompt=self._config.custom_system_prompt,
                append_system_prompt=self._config.append_system_prompt,
                context_files=self._config.context_files,
                resource_paths=self._config.resource_paths,
                session_id=record.id,
                session_manager=manager,
                command_registry=self._config.command_registry,
                provider_name=provider_name,
                inference_provider=None if dynamic_resume else record.inference_provider,
                inference_provider_mode=record.inference_provider_mode,
                requested_provider=provider_name if dynamic_resume else None,
                requested_model=model if dynamic_resume else None,
                session_provider_name=record.provider_name,
                provider_settings=self._durable_provider_settings,
                runtime_provider_config=runtime_provider_config,
                auto_compact_token_threshold=self._auto_compact_token_threshold,
                auto_compact_enabled=self._auto_compact_enabled,
                thinking_level=self._thinking_level,
                shell_command_prefix=self._config.shell_command_prefix,
                skills_enabled=self._config.skills_enabled,
                extension_paths=self._config.extension_paths,
                extensions_enabled=self._config.extensions_enabled,
                project_extensions_enabled=self._config.project_extensions_enabled,
                extension_runtime=self._extension_runtime,
                project_trust_coordinator=self._config.project_trust_coordinator,
                trust_override=self._config.trust_override,
                trust_default=self._config.trust_default,
                trust_interactive=self._config.trust_interactive,
                trust_prompt=self._config.trust_prompt,
                defer_authoritative_writes=dynamic_resume,
                owns_initial_provider=dynamic_resume,
            )
        )
        try:
            if not restore_record_model:
                # Only provider-less legacy records inherit the source model.
                # The staged loader has already resolved provider-aware records
                # against the destination's (possibly freshly discovered) catalog.
                #
                # 只有缺少提供者的旧版记录才继承源模型。暂存加载器已经依据目标端
                # 可能刚发现的目录解析了包含提供者信息的记录。
                replacement._harness.config.model = self.model
                replacement._sync_thinking_level_to_active_model()
                replacement._refresh_runtime_provider()
                replacement._sync_image_support()
        except BaseException:
            # resume owns the loaded candidate until adoption takes ownership.
            #
            # 在接纳操作取得所有权之前，resume 拥有已加载的候选会话。
            await _finish_aborted_session_close(replacement)
            raise
        await self._adopt_replacement(replacement, reason="resume")
        return f"Resumed session: {record.id}"

    async def set_session_name(self, name: str) -> str:
        """Persist a session name and notify extensions after it changes.

        持久化会话名称，并在名称变更后通知扩展。
        """
        normalized = normalize_session_name(name)
        persisted = self._persist_session_name(
            normalized,
            only_if_unnamed=False,
            index_if_missing=True,
        )
        if persisted is None:
            return normalized
        await self._extension_runtime.emit_event(SessionInfoChangedEvent(name=persisted))
        return persisted

    def _persist_session_name(
        self,
        name: str,
        *,
        only_if_unnamed: bool,
        index_if_missing: bool,
    ) -> str | None:
        """Persist a normalized name when it differs from the current title.

        当规范化名称不同于当前标题时将其持久化。
        """
        """Persist and return a changed name; return None for a no-op.

        持久化并返回已更改的名称；无变化时返回 None。
        """
        normalized = normalize_session_name(name)
        manager = self._config.session_manager
        session_id = self._config.session_id
        if manager is None or session_id is None:
            raise ValueError("Session manager is not available")
        record = manager.get_session(session_id)
        if record is None and index_if_missing:
            self.ensure_session_indexed()
            record = manager.get_session(session_id)
        if record is None:
            return None
        if only_if_unnamed and record.title:
            return None
        if record.title == normalized:
            return None
        updated = manager.touch_session(
            session_id,
            model=self.model,
            provider_name=self.provider_name,
            title=normalized,
        )
        if updated is None:
            if index_if_missing:
                raise ValueError(f"Unknown session: {session_id}")
            return None
        return updated.title or normalized

    async def new_session(self) -> str:
        """Replace this session's active state with a pending unindexed session.

        使用待处理且未索引的会话替换当前会话的活动状态。
        """
        self._require_idle("start a new session")
        await self._flush_pending_message_writes(context=self._diagnostic_context())
        manager = self._config.session_manager
        if manager is None:
            raise ValueError("Session manager is not available")

        provider_name = self._provider_name
        model = self.model
        runtime_provider_config = self._runtime_provider_config
        thinking_level = self._thinking_level
        if self._provider_settings is not None:
            selection = resolve_provider_selection(self._provider_settings)
            provider_name = selection.provider.name
            model = selection.model
            runtime_provider_config = selection.provider
            thinking_level = _coerced_thinking_level(
                selection.provider,
                model=model,
                current=self._thinking_level,
            )

        effective = self._provider_registry.effective(provider_name)
        dynamic_provider = (
            effective.definition
            if effective is not None and isinstance(effective.definition, DynamicProvider)
            else None
        )
        if dynamic_provider is not None:
            model = next(
                (item.id for item in dynamic_provider.models if item.id == model),
                dynamic_provider.default_model
                or (dynamic_provider.models[0].id if dynamic_provider.models else model),
            )
            runtime_provider_config = None
        inference_provider = (
            None
            if dynamic_provider is not None
            else _configured_inference_provider(runtime_provider_config, model)
        )
        inference_provider_mode: InferenceProviderMode = (
            "automatic"
            if dynamic_provider is not None
            else _configured_inference_provider_mode(runtime_provider_config, model)
        )
        record = (
            manager.prepare_session(
                cwd=self.cwd,
                model=model,
                provider_name=provider_name,
                inference_provider=inference_provider,
                inference_provider_mode=inference_provider_mode,
            )
            if inference_provider is not None
            else manager.prepare_session(
                cwd=self.cwd,
                model=model,
                provider_name=provider_name,
            )
        )
        replacement = await type(self).load(
            replace(
                self._config,
                provider=(None if dynamic_provider is not None else self._harness.config.provider),
                model=record.model or model,
                cwd=record.cwd,
                storage=jsonl_session_storage(record.path),
                session_id=record.id,
                provider_name=provider_name,
                inference_provider=inference_provider,
                inference_provider_mode=inference_provider_mode,
                requested_provider=provider_name if dynamic_provider is not None else None,
                requested_model=model if dynamic_provider is not None else None,
                session_provider_name=provider_name,
                provider_settings=self._durable_provider_settings,
                runtime_provider_config=runtime_provider_config,
                dynamic_provider=dynamic_provider,
                owns_initial_provider=dynamic_provider is not None,
                defer_authoritative_writes=dynamic_provider is not None,
                thinking_level=thinking_level,
                index_on_first_persist=True,
                extension_runtime=self._extension_runtime,
            )
        )
        await self._adopt_replacement(replacement, reason="new")
        return f"Started new session: {record.id}"

    async def _adopt_replacement(
        self,
        replacement: CodingSession,
        *,
        reason: Literal["new", "resume", "branch"],
    ) -> None:
        """Adopt a replacement session's state and re-bind the extension runtime.

        接纳替换会话的状态，并重新绑定扩展运行时。

        The extension runtime is long-lived and shared with the replacement; it
        must be re-bound to this outer session object because later state
        (transcript persistence, parent ids) mutates here, not on the discarded
        replacement instance.

        扩展运行时生命周期较长，并与替换会话共享；它必须重新绑定到此外层会话
        对象，因为后续状态（会话记录持久化、父标识符）会在这里变化，而不是在
        被丢弃的替换实例上变化。
        """
        old_runtime = self._extension_runtime
        try:
            if (
                replacement.project_trust_resolution is not None
                and replacement.project_trust_resolution.cancelled
            ):
                raise ValueError("Project trust decision cancelled; current session unchanged")

            # Destination transcript initialization/repair is the durable
            # commit point. It must succeed before the outgoing runtime enters
            # its shutdown path.
            #
            # 目标会话记录的初始化或修复是持久提交点。它必须在退出运行时进入
            # 关闭路径前成功完成。
            await replacement._commit_prepared_entries()

            # The replacement remains the explicit owner of its providers
            # through every cancellable/erroring pre-publication seam.
            #
            # 在发布前每个可取消或可能出错的接口处，替换会话始终明确拥有其提供者。
            await old_runtime.emit_session_shutdown(reason)
            old_runtime.clear_ui_components()
            await replacement._extension_runtime.emit_session_start(reason)
            replacement._commit_project_trust_resolution()
            replacement._session_start_pending = False
        except BaseException:
            await _finish_aborted_session_close(replacement)
            raise

        # Every cancellable boundary has completed. Adopt synchronously so a
        # reported cancellation cannot expose only part of the destination.
        #
        # 所有可取消边界均已完成。同步执行接纳，以免报告的取消暴露不完整的目标状态。
        old_runtime.retire()
        self._config = replacement._config
        self._state = replacement._state
        self._harness = replacement._harness
        # Detach the replacement's persistence listener so writes advance
        # this session's parent pointers, not the discarded replacement's.
        #
        # 分离替换会话的持久化监听器，使写入推进当前会话的父指针，而不是已丢弃
        # 替换会话的父指针。
        if replacement._persistence_unsubscribe is not None:
            replacement._persistence_unsubscribe()
            replacement._persistence_unsubscribe = None
        self._attach_persistence_listener()
        self._invalidate_context_usage_cache()
        self._last_parent_id = replacement._last_parent_id
        self._skills = replacement._skills
        self._prompt_templates = replacement._prompt_templates
        self._context_files = replacement._context_files
        self._custom_system_prompt = replacement._custom_system_prompt
        self._custom_system_prompt_path = replacement._custom_system_prompt_path
        self._append_system_prompt = replacement._append_system_prompt
        self._append_system_prompts = replacement._append_system_prompts
        self._append_system_prompt_paths = replacement._append_system_prompt_paths
        self._resource_diagnostics = replacement._resource_diagnostics
        self._command_registry = replacement._command_registry
        self._provider_name = replacement._provider_name
        self._inference_provider = replacement._inference_provider
        self._inference_provider_mode = replacement._inference_provider_mode
        self._provider_settings = replacement._provider_settings
        self._durable_provider_settings = replacement._durable_provider_settings
        self._runtime_model_catalogs = replacement._runtime_model_catalogs
        self._model_catalog_discovery_errors = replacement._model_catalog_discovery_errors
        self._runtime_provider_config = replacement._runtime_provider_config
        self._resource_paths = replacement._resource_paths
        self._auto_compact_token_threshold = replacement._auto_compact_token_threshold
        self._auto_compact_enabled = replacement._auto_compact_enabled
        self._thinking_level = replacement._thinking_level
        self._pending_initial_entries = replacement._pending_initial_entries
        self._pending_message_writes = replacement._pending_message_writes
        self._extension_runtime = replacement._extension_runtime
        self._provider_registry = replacement._provider_registry
        self._credential_store = replacement._credential_store
        self._diagnostic_logger = replacement._diagnostic_logger
        self._runtime_model_limits = replacement._runtime_model_limits
        self._runtime_model_limits_key = replacement._runtime_model_limits_key
        self._model_limits_discovery_error = replacement._model_limits_discovery_error
        self._prepared_entries = replacement._prepared_entries
        self._owned_providers.extend(replacement._owned_providers)
        replacement._owned_providers.clear()
        self._image_support = replacement._image_support
        self._project_trust_resolution = replacement._project_trust_resolution
        self._project_trust_commit_pending = False
        self._session_start_pending = False
        self._extension_runtime.bind(self)
        self._extension_runtime.attach_harness_listener(self._harness.subscribe)
        # Adoption is already committed. Finish outgoing cleanup under a
        # shielded owner and contain cancellation rather than reporting that
        # the requested destination failed to replace the source session.
        #
        # 接纳已经提交。通过受屏蔽的所有者完成退出清理并隔离取消，而不报告请求
        # 的目标会话未能替换源会话。
        await _finish_adopted_runtime_close(old_runtime)

    async def compact_detailed(self, instructions: str | None = None) -> ManualCompactionResult:
        """Compact older context while preserving a real recent-entry boundary.

        压缩较早的上下文，同时保留真实的近期条目边界。
        """
        await self._flush_pending_message_writes(context=self._diagnostic_context())
        plan = self._recent_preserving_compaction_plan()
        if plan is None:
            raise ValueError("Not enough context to compact while preserving recent entries")
        tokens_before = self.context_token_estimate
        generated = await self._generate_compaction_summary(
            plan.messages_to_summarize,
            custom_instructions=instructions,
        )
        await self._append_compaction(
            generated.text,
            first_kept_entry_id=plan.first_kept_entry_id,
            tokens_before=tokens_before,
            usage=generated.usage,
            provider=generated.provider,
            model=generated.model,
            response_provider=generated.response_provider,
        )
        return ManualCompactionResult(
            summary=generated.text,
            first_kept_entry_id=plan.first_kept_entry_id,
            tokens_before=tokens_before,
            estimated_tokens_after=self.context_token_estimate,
            replaced_entry_count=plan.replaced_entry_count,
        )

    async def compact(self, instructions: str | None = None) -> str:
        """Generate a manual compaction summary and rebuild active context.

        生成手动压缩摘要并重建活动上下文。
        """
        result = await self.compact_detailed(instructions)
        return f"Compacted {result.replaced_entry_count} context entries."

    async def aclose(self) -> None:
        """Close every owned extension/provider resource exactly once.

        对每个拥有的扩展和提供者资源执行一次且仅一次关闭。

        Caller cancellation is remembered but cannot cancel the durable close
        task. The first call propagates it only after all ownership ledgers are
        discharged; later calls observe the same completed task idempotently.

        调用方取消会被记住，但不能取消持久关闭任务。首次调用只在所有所有权
        账本清空后传播取消；后续调用以幂等方式观察同一个已完成任务。
        """
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._close_owned_resources(),
                name="tau-coding-session-close",
            )
            self._close_task = close_task

        cancelled = await _await_cleanup_completion(close_task)
        error: BaseException | None = None
        try:
            close_task.result()
        except BaseException as exc:  # all resources were attempted before this outcome
            # 在得到此结果前已尝试处理所有资源。
            error = exc
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error

    async def _close_owned_resources(self) -> None:
        """Run the sole close pass, continuing after individual failures.

        执行唯一的关闭流程，并在个别关闭失败后继续。
        """
        error: BaseException | None = None
        try:
            if self._extension_runtime.active:
                await self._extension_runtime.emit_session_shutdown("quit")
        except BaseException as exc:
            error = exc

        # Final close has no successor sharing the UI bridge. Remove any
        # source-owned widgets/interceptors before invalidating the API.
        #
        # 最终关闭时没有继任者共享界面桥接。使 API 失效前先移除源运行时拥有的
        # 小组件和拦截器。
        try:
            self._extension_runtime.clear_ui_components()
        except BaseException as exc:
            if error is None:
                error = exc

        # A runtime may already be synchronously retired by replacement. Close
        # still owns the async drain/containment step and must never skip it.
        #
        # 运行时可能已经被替换流程同步退役。关闭流程仍负责异步排空和隔离步骤，
        # 绝不能跳过。
        try:
            await self._extension_runtime.aclose()
        except BaseException as exc:
            if error is None:
                error = exc

        # Remove each provider from the ownership ledger before its only close
        # attempt. One hostile provider cannot prevent later providers closing.
        #
        # 在提供者唯一一次关闭尝试前，将其从所有权账本移除。单个异常提供者不能
        # 阻止后续提供者关闭。
        providers = tuple(self._owned_providers)
        self._owned_providers.clear()
        for provider in providers:
            try:
                await provider.aclose()
            except BaseException as exc:
                if error is None:
                    error = exc

        if error is not None:
            raise error

    def handle_command(self, text: str) -> CommandResult:
        """Handle coding-session slash commands.

        处理编码会话斜杠命令。

        Prompt-template slash commands are expansion directives, so they remain
        unhandled here and flow through `prompt()` for on-the-fly replacement.

        提示词模板斜杠命令属于展开指令，因此在这里保持未处理，并流经
        `prompt()` 进行即时替换。
        """
        if expand_prompt_template_command(text, self._prompt_templates) is not None:
            return CommandResult(handled=False)
        return self._command_registry.execute(self, text)

    def ensure_session_indexed(self) -> None:
        """Persist pending session metadata and add this session to the resume index.

        持久化待处理会话元数据，并将当前会话加入恢复索引。
        """
        if self._config.session_id is None or self._config.session_manager is None:
            return
        if self._config.session_manager.get_session(self._config.session_id) is None:
            self._config.session_manager.create_session(
                cwd=self.cwd,
                model=self.model,
                provider_name=self.provider_name,
                inference_provider=self._inference_provider,
                inference_provider_mode=self._inference_provider_mode,
                session_id=self._config.session_id,
            )
        self._config = replace(self._config, index_on_first_persist=False)
        self._ensure_session_file_initialized()

    def expand_prompt_text(self, text: str) -> str:
        """Expand prompt text using loaded markdown resources.

        使用已加载的 Markdown 资源展开提示词文本。
        """
        expanded_prompt = expand_prompt_template_command(text, self._prompt_templates)
        if expanded_prompt is not None:
            return expanded_prompt
        expanded_skill = expand_skill_command(text, self._skills)
        return expanded_skill if expanded_skill is not None else text

    async def run_terminal_command(
        self,
        command: str,
        *,
        add_to_context: bool,
    ) -> TerminalCommandResult:
        """Run a shell command in the session cwd, optionally adding output to context.

        在会话工作目录中运行 shell 命令，并可选择将输出加入上下文。
        """
        normalized_command = command.strip()
        if not normalized_command:
            raise ValueError("Terminal command cannot be empty")

        bash_tool = create_bash_tool(
            cwd=self.cwd,
            shell_command_prefix=self._config.shell_command_prefix,
        )
        result = await bash_tool.execute("terminal-command", {"command": normalized_command})
        exit_code = None
        if isinstance(result.details, dict):
            raw_exit_code = result.details.get("exit_code")
            exit_code = raw_exit_code if isinstance(raw_exit_code, int) else None

        if add_to_context:
            await self._flush_pending_message_writes(context=self._diagnostic_context())
            context_message = UserMessage(
                content=_terminal_command_context_message(
                    normalized_command,
                    result.text,
                )
            )
            self._harness.append_message(context_message)
            self._invalidate_context_usage_cache()
            await self._persist_message(context_message)

        return TerminalCommandResult(
            command=normalized_command,
            output=result.text,
            exit_code=exit_code,
            ok=exit_code == 0,
            added_to_context=add_to_context,
        )

    async def prompt(
        self,
        content: str,
        *,
        streaming_behavior: StreamingBehavior | None = None,
        source: Literal["interactive", "extension"] = "interactive",
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> AsyncIterator[CodingSessionEvent]:
        """Append a user prompt, run the agent, and persist new messages.

        追加用户提示词、运行代理并持久化新消息。

        ``custom_type``/``details`` attach custom-message render metadata to the
        appended ``UserMessage`` (used when an extension delivers a custom
        message that starts an idle session's turn). ``source`` marks who
        initiated the turn for the `input` hook (``"extension"`` when an
        extension started it, ``"interactive"`` otherwise).

        ``custom_type`` 和 ``details`` 会把自定义消息渲染元数据附加到追加的
        ``UserMessage``，用于扩展投递自定义消息并启动空闲会话轮次的情况。
        ``source`` 为 `input` 钩子标记轮次发起方：扩展发起时为
        ``"extension"``，否则为 ``"interactive"``。
        """
        context = self._diagnostic_context()
        input_outcome = await self._extension_runtime.run_input_hooks(
            content, source=source, streaming_behavior=streaming_behavior
        )
        if input_outcome.handled:
            if input_outcome.message:
                self._extension_runtime.ui.notify(input_outcome.message)
            return
        content = input_outcome.text
        try:
            expanded_content = self.expand_prompt_text(content)
        except ResourceError:
            raise
        except Exception as exc:
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase="expand_prompt",
                exc=exc,
            )
            raise

        if self._harness.is_running:
            if streaming_behavior == "steer":
                self._harness.steer(expanded_content)
                session_event_0 = self.queue_update_event()
                await self._extension_runtime.emit_event(session_event_0)
                yield session_event_0
                return
            if streaming_behavior == "follow_up":
                self._harness.follow_up(expanded_content)
                session_event_0 = self.queue_update_event()
                await self._extension_runtime.emit_event(session_event_0)
                yield session_event_0
                return
            raise RuntimeError(
                "CodingSession is already running; pass streaming_behavior to queue a message."
            )

        await self._flush_pending_message_writes(context=context)
        await self._refresh_runtime_model_limits()
        await self._try_auto_compact(context=context, phase="auto_compact_before_prompt")
        # id() values can be reused once earlier message objects are freed.
        #
        # 早期消息对象释放后，id() 值可能被复用。
        self._ended_message_ids.clear()
        self._persisted_message_ids.clear()
        events: AsyncIterator[AgentEvent] | None = None
        settled_event: AgentSettledEvent | None = None
        auto_name_attempted = False
        overflow_message: AssistantMessage | None = None
        route_failure_message: AssistantMessage | None = None
        try:
            prompt_message: AgentMessage
            if custom_type is not None:
                prompt_message = CustomMessage(
                    custom_type=custom_type,
                    content=expanded_content,
                    display=True,
                    details=details,
                )
            else:
                prompt_message = UserMessage(content=expanded_content)
            events = self._harness.prompt_message(prompt_message)
            self._invalidate_context_usage_cache()
            async for event in events:
                auto_name_message: str | None = None
                if (
                    isinstance(event, MessageEndEvent)
                    and not auto_name_attempted
                    and isinstance(event.message, UserMessage)
                ):
                    auto_name_attempted = True
                    auto_name_message = event.message.text
                if isinstance(event, ToolExecutionEndEvent):
                    self._invalidate_context_usage_cache()
                if (
                    isinstance(event, MessageEndEvent)
                    and isinstance(event.message, AssistantMessage)
                    and event.message.stop_reason == "error"
                ):
                    self._last_diagnostic_log_path = self._diagnostic_logger.log_assistant_error(
                        context=context,
                        phase="agent_loop",
                        message=event.message,
                    )
                    if is_context_overflow_error(event.message):
                        overflow_message = event.message
                    elif self._should_auto_failover_huggingface_route(event.message):
                        route_failure_message = event.message
                if isinstance(event, AgentEndEvent):
                    yield SessionAgentEndEvent(
                        messages=event.messages,
                        will_retry=(
                            overflow_message is not None or route_failure_message is not None
                        ),
                    )
                else:
                    yield event
                # Let frontends render the confirmed, expanded prompt before
                # session naming performs its separate provider request.
                #
                # 让前端先渲染已确认且展开的提示词，然后会话命名再执行独立的
                # 提供者请求。
                if auto_name_message is not None:
                    await self._try_auto_name_session(auto_name_message, context=context)
            if overflow_message is not None:
                session_event_1 = CompactionStartEvent(reason="overflow")
                await self._extension_runtime.emit_event(session_event_1)
                yield session_event_1
                compacted = await self._try_overflow_compact(context=context)
                compaction_end = CompactionEndEvent(
                    reason="overflow",
                    result=None,
                    aborted=not compacted,
                    will_retry=compacted,
                    error_message=None if compacted else "Overflow compaction failed",
                )
                await self._extension_runtime.emit_event(compaction_end)
                yield compaction_end
                if compacted:
                    retry_start = AutoRetryStartEvent(
                        attempt=1,
                        max_attempts=1,
                        delay_ms=0,
                        error_message=overflow_message.error_message or "Context overflow",
                    )
                    await self._extension_runtime.emit_event(retry_start)
                    yield retry_start
                    events = self._harness.continue_()
                    self._invalidate_context_usage_cache()
                    overflow_retry_error: str | None = None
                    async for retry_event in events:
                        if isinstance(retry_event, ToolExecutionEndEvent):
                            self._invalidate_context_usage_cache()
                        if (
                            isinstance(retry_event, MessageEndEvent)
                            and isinstance(retry_event.message, AssistantMessage)
                            and retry_event.message.stop_reason in {"error", "aborted"}
                        ):
                            overflow_retry_error = (
                                retry_event.message.error_message or "Provider request aborted"
                            )
                            if retry_event.message.stop_reason == "error":
                                self._last_diagnostic_log_path = (
                                    self._diagnostic_logger.log_assistant_error(
                                        context=context,
                                        phase="agent_loop_retry",
                                        message=retry_event.message,
                                    )
                                )
                        if isinstance(retry_event, AgentEndEvent):
                            yield SessionAgentEndEvent(
                                messages=retry_event.messages,
                                will_retry=False,
                            )
                        else:
                            yield retry_event
                    session_event_4 = AutoRetryEndEvent(
                        success=overflow_retry_error is None,
                        attempt=1,
                        final_error=overflow_retry_error,
                    )
                    await self._extension_runtime.emit_event(session_event_4)
                    yield session_event_4
            elif route_failure_message is not None:
                async for failover_event in self._run_huggingface_route_failover(context=context):
                    yield failover_event
            else:
                await self._try_auto_compact(context=context, phase="auto_compact_after_prompt")
        except Exception as exc:
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase="agent_loop",
                exc=exc,
            )
            raise
        finally:
            try:
                await self._reconcile_run_persistence(events, context=context)
            finally:
                if events is not None:
                    settled_event = await self._dispatch_agent_settled()
        if settled_event is not None:
            yield settled_event

    async def continue_(self) -> AsyncIterator[CodingSessionEvent]:
        """Continue the agent from restored state and persist new messages.

        从恢复的状态继续运行代理，并持久化新消息。
        """
        context = self._diagnostic_context()
        await self._flush_pending_message_writes(context=context)
        await self._refresh_runtime_model_limits()
        # id() values can be reused once earlier message objects are freed.
        #
        # 早期消息对象释放后，id() 值可能被复用。
        self._ended_message_ids.clear()
        self._persisted_message_ids.clear()
        events: AsyncIterator[AgentEvent] | None = None
        settled_event: AgentSettledEvent | None = None
        route_failure_message: AssistantMessage | None = None
        try:
            events = self._harness.continue_()
            self._invalidate_context_usage_cache()
            async for event in events:
                if isinstance(event, ToolExecutionEndEvent):
                    self._invalidate_context_usage_cache()
                if (
                    isinstance(event, MessageEndEvent)
                    and isinstance(event.message, AssistantMessage)
                    and event.message.stop_reason == "error"
                ):
                    self._last_diagnostic_log_path = self._diagnostic_logger.log_assistant_error(
                        context=context,
                        phase="agent_loop",
                        message=event.message,
                    )
                    if self._should_auto_failover_huggingface_route(event.message):
                        route_failure_message = event.message
                if isinstance(event, AgentEndEvent):
                    yield SessionAgentEndEvent(
                        messages=event.messages,
                        will_retry=route_failure_message is not None,
                    )
                else:
                    yield event
            if route_failure_message is not None:
                async for failover_event in self._run_huggingface_route_failover(context=context):
                    yield failover_event
            await self._try_auto_compact(context=context, phase="auto_compact_after_continue")
        except Exception as exc:
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase="agent_loop",
                exc=exc,
            )
            raise
        finally:
            try:
                await self._reconcile_run_persistence(events, context=context)
            finally:
                if events is not None:
                    settled_event = await self._dispatch_agent_settled()
        if settled_event is not None:
            yield settled_event

    async def _dispatch_agent_settled(self) -> AgentSettledEvent:
        """Dispatch and return the final session event for one started run.

        分派并返回一次已启动运行的最终会话事件。
        """
        event = AgentSettledEvent()
        await self._extension_runtime.emit_event(event)
        return event

    def _diagnostic_context(self) -> AgentCallDiagnosticContext:
        """Build non-secret diagnostic context for the next agent call.

        为下一次代理调用构建非敏感诊断上下文。
        """
        return AgentCallDiagnosticContext(
            provider_name=self._provider_name,
            model=self.model,
            cwd=self.cwd,
            session_id=self.session_id,
            run_id=new_agent_call_run_id(),
        )

    async def _persist_active_tool_history_repairs(self) -> ToolHistoryRepair | None:
        """Stage or append one complete repair branch for malformed history.

        为格式异常的历史暂存或追加一条完整修复分支。
        """
        plan = _tool_history_repair_plan(
            self._state.messages,
            context_entry_ids=self._state.context_entry_ids,
            entries=self._state.entries,
        )
        if plan is None:
            return None

        parent_id, suffix, repair = plan
        active_model = self._state.model
        active_thinking_level = self._state.thinking_level
        active_entries = self._state.entries
        parent_index = next(
            (index for index, entry in enumerate(active_entries) if entry.id == parent_id),
            -1,
        )
        custom_entries = [
            entry
            for entry in active_entries[parent_index + 1 :]
            if isinstance(entry, CustomEntry) and entry.namespace != "tau.session-history-repair"
        ]
        staged: list[SessionEntry] = [
            CustomEntry(
                parent_id=parent_id,
                namespace="tau.session-history-repair",
                data={"version": 1, **repair.diagnostic_data()},
            )
        ]
        parent_id = staged[-1].id
        for message in suffix:
            entry = _session_entry_for_message(parent_id=parent_id, message=message)
            staged.append(entry)
            parent_id = entry.id
        if active_model is not None:
            model_entry = ModelChangeEntry(
                parent_id=parent_id,
                model=active_model,
                provider=self.provider_name,
            )
            staged.append(model_entry)
            parent_id = model_entry.id
        thinking_entry = ThinkingLevelChangeEntry(
            parent_id=parent_id,
            thinking_level=active_thinking_level,
        )
        staged.append(thinking_entry)
        parent_id = thinking_entry.id
        for custom_entry in custom_entries:
            copied_entry = CustomEntry(
                parent_id=parent_id,
                namespace=custom_entry.namespace,
                data=custom_entry.data,
            )
            staged.append(copied_entry)
            parent_id = copied_entry.id
        if self._config.defer_authoritative_writes:
            self._prepared_entries.extend(staged)
            self._last_parent_id = parent_id
            replay_entries = [*self._state.entries, *staged]
            self._state = SessionState.from_entries(replay_entries, leaf_id=parent_id)
        else:
            await self._append_session_batch(staged)
            self._last_parent_id = parent_id
            await self._refresh_persisted_state(leaf_id=parent_id)
        self._harness.replace_messages(self._state.messages)
        self._invalidate_context_usage_cache()
        return repair

    def _attach_persistence_listener(self) -> None:
        """(Re-)attach push persistence to the current harness.

        将推送式持久化重新附加到当前代理核心。

        Persistence subscribes to harness events rather than running in the
        event consumer, which the TUI tears down on interrupt.

        持久化订阅代理核心事件，而不是在事件消费者中运行；TUI 会在中断时
        拆除事件消费者。
        """
        if self._persistence_unsubscribe is not None:
            self._persistence_unsubscribe()
            self._persistence_unsubscribe = None
        # Command-only tests construct sessions with stub harnesses.
        #
        # 仅测试命令的场景会使用桩核心构造会话。
        subscribe = getattr(self._harness, "subscribe", None)
        if subscribe is not None:
            self._persistence_unsubscribe = subscribe(self._persist_on_message_end)

    async def _persist_on_message_end(self, event: AgentEvent) -> None:
        """Persist a message when its lifecycle emits a completion event.

        当消息生命周期发出完成事件时将消息持久化。
        """
        if isinstance(event, MessageEndEvent):
            self._ended_message_ids.add(id(event.message))
            await self._persist_message(event.message)

    async def _persist_message(self, message: AgentMessage) -> None:
        """Persist one completed message at the active branch tip, idempotently.

        以幂等方式在活动分支末端持久化一条已完成消息。

        Message lifecycle events are the durable-message boundary. A stable entry
        id lets a retry recognize an append that reached disk before raising.
        Only retries pay for a full-file read.

        消息生命周期事件是持久消息边界。稳定的条目标识符使重试能够识别先写入
        磁盘后抛错的追加操作。只有重试才会付出读取整个文件的成本。
        """
        message_id = id(message)
        pending = self._pending_message_writes.get(message_id)
        is_retry = pending is not None
        if pending is None:
            entry = _session_entry_for_message(parent_id=self._last_parent_id, message=message)
            pending = _PendingMessageWrite(message=message, entry=entry)
            self._pending_message_writes[message_id] = pending

        durable_ids = (
            {entry.id for entry in await self._read_session_entries()} if is_retry else frozenset()
        )
        if pending.entry.id not in durable_ids:
            await self._append_session_entry(pending.entry)
        self._last_parent_id = pending.entry.id

        await self._refresh_persisted_state(leaf_id=self._last_parent_id)
        self._persisted_message_ids.add(message_id)
        self._pending_message_writes.pop(message_id, None)
        self._invalidate_context_usage_cache()

    async def _reconcile_run_persistence(
        self,
        events: AsyncIterator[AgentEvent] | None,
        *,
        context: AgentCallDiagnosticContext,
    ) -> None:
        """Close a run and retry failed persists still present in the transcript.

        关闭一次运行，并重试会话记录中仍存在的持久化失败。

        Keyed on message identity, not counts: the loop emits an assistant's
        ``message_end`` before appending it to the transcript. A message whose
        persist and append both failed cannot be retried here; the repair at
        the next run start re-synthesizes and persists its tool result.

        此逻辑按消息身份而非数量匹配：循环在把助手消息追加到会话记录前先发出
        ``message_end``。如果某条消息的持久化和追加都失败，就无法在这里重试；
        下一次运行开始时的修复会重新合成并持久化其工具结果。
        """
        if events is not None:
            aclose = getattr(events, "aclose", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()
        for message in self._harness.messages:
            message_id = id(message)
            if (
                message_id in self._ended_message_ids
                and message_id not in self._persisted_message_ids
            ):
                # Runs in a finally: a repeat failure must not mask cancellation.
                #
                # 此逻辑在 finally 中运行：重复失败不能掩盖取消结果。
                try:
                    await self._persist_message(message)
                except Exception as exc:  # noqa: BLE001 - preserve cancellation
                    # 保留取消结果。
                    self._log_persistence_failure(context=context, exc=exc)
        self._ended_message_ids.clear()
        self._persisted_message_ids.clear()

    async def _flush_pending_message_writes(self, *, context: AgentCallDiagnosticContext) -> None:
        """Finish earlier partial writes before another storage-dependent action.

        在执行另一个依赖存储的操作前完成先前的部分写入。
        """
        harness_message_ids = {id(message) for message in self._harness.messages}
        for pending in tuple(self._pending_message_writes.values()):
            if id(pending.message) not in harness_message_ids:
                self._harness.append_message(pending.message)
                harness_message_ids.add(id(pending.message))
            try:
                await self._persist_message(pending.message)
            except Exception as exc:
                self._log_persistence_failure(context=context, exc=exc)
                raise

    def _log_persistence_failure(
        self, *, context: AgentCallDiagnosticContext, exc: BaseException
    ) -> None:
        """Record a message-persistence failure without exposing message content.

        记录消息持久化失败，同时不暴露消息内容。
        """
        with suppress(Exception):
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase="session_persistence_reconcile",
                exc=exc,
            )

    def _invalidate_context_usage_cache(self) -> None:
        """Clear the cached active-context usage estimate.

        清除缓存的活动上下文用量估算值。
        """
        """Mark context accounting dirty after transcript/system/tool changes.

        在会话记录、系统提示词或工具变更后将上下文计量标记为失效。
        """
        self._context_usage_cache = None

    async def _refresh_persisted_state(self, *, leaf_id: str | None) -> None:
        """Replay durable entries and refresh harness state at a selected leaf.

        重放持久化条目，并在选定叶节点刷新代理核心状态。
        """
        entries = await self._read_session_entries()
        self._state = SessionState.from_entries(entries, leaf_id=leaf_id)
        if self._config.session_id is not None and self._config.session_manager is not None:
            self._config.session_manager.touch_session(
                self._config.session_id,
                model=self.model,
                provider_name=self.provider_name,
                inference_provider=self._inference_provider,
                inference_provider_mode=self._inference_provider_mode,
                preserve_inference_provider=False,
            )

    async def _read_session_entries(self) -> list[SessionEntry]:
        """Read stored entries, detaching roots imported from external history.

        读取已存储条目，并分离从外部历史导入的根节点。
        """
        return _detach_missing_parents(await self._config.storage.read_all())

    async def _commit_prepared_entries(self) -> None:
        """Durably commit the staged startup/repair batch exactly once.

        对暂存的启动或修复批次执行一次且仅一次持久提交。
        """
        if not self._config.defer_authoritative_writes:
            return
        durable_ids = {entry.id for entry in await self._config.storage.read_all()}
        missing = tuple(entry for entry in self._prepared_entries if entry.id not in durable_ids)
        if missing:
            append_batch = getattr(self._config.storage, "append_batch", None)
            if append_batch is None:
                # Compatibility storage implementations predate the atomic
                # batch contract.  Production storage always takes this path.
                #
                # 兼容存储实现早于原子批次约定。生产存储始终采用此路径。
                for entry in missing:
                    await self._config.storage.append(entry)
            else:
                await append_batch(missing)
        self._pending_initial_entries = ()
        self._prepared_entries.clear()
        self._config = replace(self._config, defer_authoritative_writes=False)
        try:
            if self._config.index_on_first_persist:
                self._index_current_session()
        except Exception as exc:  # index is a rebuildable cache, not authority
            # 索引是可重建缓存，并非权威数据源。
            self._record_session_index_diagnostic(exc)

    async def _append_session_entry(self, entry: SessionEntry) -> None:
        """Append one durable entry after flushing deferred session metadata.

        刷新延迟会话元数据后追加一条持久化条目。
        """
        await self._ensure_session_initialized()
        await self._config.storage.append(entry)

    async def _append_session_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Append entries as one storage transaction, with legacy fallback.

        将条目作为一个存储事务追加，并提供旧版后备路径。
        """
        await self._ensure_session_initialized()
        append_batch = getattr(self._config.storage, "append_batch", None)
        if append_batch is None:
            for entry in entries:
                await self._config.storage.append(entry)
            return
        await append_batch(tuple(entries))

    async def _close_replaced_provider(self, provider: ModelProvider) -> None:
        """Close a Tau-owned provider once after a committed publication.

        在发布提交后关闭一次由 Tau 拥有的提供者。
        """
        if provider not in self._owned_providers:
            return
        self._owned_providers.remove(provider)
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()

    async def _ensure_session_initialized(self) -> None:
        """Persist initial entries and index metadata before the first durable write.

        在首次持久写入前保存初始条目并建立元数据索引。
        """
        if self._config.defer_authoritative_writes and self._prepared_entries:
            await self._commit_prepared_entries()
            return
        if not self._pending_initial_entries:
            return
        await self._write_pending_initial_entries()
        if self._config.index_on_first_persist:
            try:
                self._index_current_session()
            except Exception as exc:
                self._record_session_index_diagnostic(exc)

    async def _write_pending_initial_entries(self) -> None:
        """Flush any deferred initial session entries to storage.

        将延迟的初始会话条目刷新到存储。
        """
        durable_ids = {entry.id for entry in await self._config.storage.read_all()}
        missing = tuple(
            entry for entry in self._pending_initial_entries if entry.id not in durable_ids
        )
        if missing:
            append_batch = getattr(self._config.storage, "append_batch", None)
            if append_batch is not None:
                await append_batch(missing)
            else:
                for entry in missing:
                    await self._config.storage.append(entry)
        self._pending_initial_entries = ()

    def _ensure_session_file_initialized(self) -> None:
        """Create the backing session file when it does not yet exist.

        在底层会话文件尚不存在时创建该文件。
        """
        if not self._pending_initial_entries:
            return
        for entry in self._pending_initial_entries:
            _append_session_entry_sync(self._config.storage, entry)
        self._pending_initial_entries = ()

    def _record_session_index_diagnostic(self, exc: BaseException) -> None:
        """Record a non-fatal failure while updating the session index.

        记录更新会话索引时发生的非致命失败。
        """
        self._resource_diagnostics = (
            *self._resource_diagnostics,
            ResourceDiagnostic(
                kind="session-index",
                message=f"Session index needs repair: {type(exc).__name__}",
                severity="warning",
            ),
        )

    def _index_current_session(self) -> None:
        """Add the current session metadata to the resume index.

        将当前会话元数据添加到恢复索引。
        """
        if self._config.session_id is None or self._config.session_manager is None:
            return
        existing = self._config.session_manager.get_session(self._config.session_id)
        if existing is not None:
            return
        self._config.session_manager.create_session(
            cwd=self.cwd,
            model=self.model,
            provider_name=self.provider_name,
            inference_provider=self._inference_provider,
            session_id=self._config.session_id,
        )

    # Attempt automatic compaction and convert non-fatal failures to diagnostics.
    #
    # 尝试自动压缩，并将非致命失败转换为诊断信息。
    async def _try_auto_compact(
        self,
        *,
        context: AgentCallDiagnosticContext,
        phase: str,
    ) -> bool:
        try:
            return await self._maybe_auto_compact()
        except Exception as exc:  # noqa: BLE001 - automatic compaction must not lose a turn
            # 自动压缩不能导致当前轮次丢失。
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase=phase,
                exc=exc,
            )
            return False

    # Attempt recovery compaction after a context-overflow failure.
    #
    # 在上下文溢出失败后尝试恢复性压缩。
    async def _try_overflow_compact(
        self,
        *,
        context: AgentCallDiagnosticContext,
    ) -> bool:
        try:
            plan = self._recent_preserving_compaction_plan()
            if plan is None:
                return False
            tokens_before = self.context_token_estimate
            generated = await self._generate_compaction_summary(plan.messages_to_summarize)
            await self._append_compaction(
                generated.text,
                first_kept_entry_id=plan.first_kept_entry_id,
                tokens_before=tokens_before,
                usage=generated.usage,
                provider=generated.provider,
                model=generated.model,
                response_provider=generated.response_provider,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - the original overflow remains visible
            # 保持原始溢出错误可见。
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase="overflow_compact",
                exc=exc,
            )
            return False

    # Generate and persist an automatic session name when eligible.
    #
    # 在符合条件时生成并持久化自动会话名称。
    async def _try_auto_name_session(
        self,
        first_message: str,
        *,
        context: AgentCallDiagnosticContext,
    ) -> None:
        if not self._should_auto_name_session():
            return
        try:
            title = await self._generate_session_name(first_message)
        except Exception as exc:  # noqa: BLE001 - naming must not interrupt the agent turn
            # 命名不能中断代理轮次。
            self._last_diagnostic_log_path = self._diagnostic_logger.log_exception(
                context=context,
                phase="auto_name_session",
                exc=exc,
            )
            title = _fallback_session_name(first_message)
        if title is None:
            title = _fallback_session_name(first_message)
        if title is None:
            return
        persisted = self._persist_session_name(
            title,
            only_if_unnamed=True,
            index_if_missing=False,
        )
        if persisted is not None:
            await self._extension_runtime.emit_event(SessionInfoChangedEvent(name=persisted))

    def _should_auto_name_session(self) -> bool:
        """Return whether the current unnamed session should receive an automatic title.

        返回当前未命名会话是否应获得自动标题。
        """
        if self._config.session_id is None or self._config.session_manager is None:
            return False
        record = self._config.session_manager.get_session(self._config.session_id)
        if record is not None and record.title:
            return False
        return sum(isinstance(message, UserMessage) for message in self._harness.messages) == 1

    async def _generate_session_name(self, first_message: str) -> str | None:
        """Generate and sanitize a concise title from the first user message.

        根据首条用户消息生成并清理简短标题。
        """
        prompt = (
            "Create a concise session name for this first user message. "
            "Use at most four words.\n\n"
            f"User message:\n{first_message}"
        )
        text_parts: list[str] = []
        final_text: str | None = None
        async for event in self._harness.config.provider.stream_response(
            model=self.model,
            system=SESSION_NAME_SYSTEM_PROMPT,
            messages=[UserMessage(content=prompt)],
            tools=[],
        ):
            if isinstance(event, TextDeltaEvent):
                text_parts.append(event.delta)
            elif isinstance(event, AssistantDoneEvent):
                final_text = event.message.text
            elif isinstance(event, AssistantErrorEvent):
                raise RuntimeError(
                    f"Session naming failed: {event.error.error_message or event.reason}"
                )
        return _sanitize_session_name(final_text if final_text is not None else "".join(text_parts))

    def _provider_is_usable(self, provider: ProviderConfig) -> bool:
        """Return whether the provider has usable credentials in this session.

        返回提供者在当前会话中是否具有可用凭据。
        """
        return provider_has_usable_credentials(
            provider,
            credential_reader=self._credential_store,
        )

    def _usable_provider_configs(self) -> tuple[ProviderConfig, ...]:
        """Return configured providers that can be called with current credentials.

        返回可使用当前凭据调用的已配置提供者。
        """
        if self._provider_settings is None:
            return ()
        return tuple(
            provider
            for provider in self._provider_settings.providers
            if self._provider_is_usable(provider)
        )

    async def _maybe_auto_compact(self) -> bool:
        """Run threshold-based compaction when the active context requires it.

        当活动上下文需要时执行基于阈值的压缩。
        """
        threshold = self.auto_compact_token_threshold
        if threshold is None or threshold <= 0:
            return False
        if len(self._state.context_entry_ids) < 2:
            return False
        if self.context_token_estimate <= threshold:
            return False
        plan = self._recent_preserving_compaction_plan()
        if plan is None:
            return False
        tokens_before = self.context_token_estimate
        generated = await self._generate_compaction_summary(plan.messages_to_summarize)
        await self._append_compaction(
            generated.text,
            first_kept_entry_id=plan.first_kept_entry_id,
            tokens_before=tokens_before,
            usage=generated.usage,
            provider=generated.provider,
            model=generated.model,
            response_provider=generated.response_provider,
        )
        return True

    # Ask the active model to summarize entries selected for compaction.
    #
    # 请求活动模型概括选定用于压缩的条目。
    async def _generate_compaction_summary(
        self,
        messages: tuple[AgentMessage, ...],
        *,
        custom_instructions: str | None = None,
    ) -> _GeneratedSummary:
        prompt = build_compaction_summary_prompt(
            messages,
            custom_instructions=custom_instructions,
        )
        text_parts: list[str] = []
        final_text: str | None = None
        response_usages: list[Usage] = []
        response_provider: str | None = None
        summary_messages: list[AgentMessage] = [UserMessage(content=prompt)]
        async for event in self._harness.config.provider.stream_response(
            model=self.model,
            system=SUMMARIZATION_SYSTEM_PROMPT,
            messages=summary_messages,
            tools=[],
        ):
            if isinstance(event, TextDeltaEvent):
                text_parts.append(event.delta)
            elif isinstance(event, AssistantDoneEvent):
                final_text = event.message.text
                response_usages.append(event.message.usage)
                response_provider = event.message.response_provider
            elif isinstance(event, AssistantErrorEvent):
                raise RuntimeError(
                    f"Compaction summarization failed: {event.error.error_message or event.reason}"
                )

        summary = (final_text if final_text is not None else "".join(text_parts)).strip()
        if not summary:
            raise RuntimeError("Compaction summarization returned an empty summary")
        return _GeneratedSummary(
            text=summary,
            usage=sum_usage(response_usages) if response_usages else None,
            provider=self.provider_name,
            model=self.model,
            response_provider=response_provider,
        )

    # Summarize an abandoned branch through the configured model provider.
    #
    # 通过已配置模型提供者概括已放弃分支。
    async def _summarize_branch_messages(
        self,
        messages: tuple[AgentMessage, ...],
        *,
        custom_instructions: str | None = None,
        replace_instructions: bool = False,
    ) -> _GeneratedSummary:
        try:
            result = await summarize_branch_messages_with_model(
                provider=self._harness.config.provider,
                model=self.model,
                messages=messages,
                custom_instructions=custom_instructions,
                replace_instructions=replace_instructions,
            )
        except Exception:
            result = None
        if result is not None:
            summary, usage, response_provider = result
            return _GeneratedSummary(
                text=summary,
                usage=usage,
                provider=self.provider_name,
                model=self.model,
                response_provider=response_provider,
            )
        return _GeneratedSummary(
            text=summarize_messages_for_compaction(messages),
            usage=None,
        )

    def _recent_preserving_compaction_plan(self) -> CompactionPlan | None:
        """Plan compaction while retaining a valid recent context suffix.

        规划压缩，同时保留有效的近期上下文后缀。
        """
        rows = self._active_context_rows()
        if len(rows) < 2:
            return None

        first_kept_index = _first_recent_context_index(
            rows,
            keep_recent_tokens=DEFAULT_COMPACTION_KEEP_RECENT_TOKENS,
        )
        if first_kept_index <= 0:
            return None

        if first_kept_index >= len(rows):
            return None

        replaced = rows[:first_kept_index]
        return CompactionPlan(
            first_kept_entry_id=rows[first_kept_index][0],
            replaced_entry_count=len(replaced),
            messages_to_summarize=tuple(message for _entry_id, message in replaced),
        )

    def _active_context_rows(self) -> tuple[tuple[str, AgentMessage], ...]:
        """Pair active context entry ids with their replayed messages.

        将活动上下文条目标识符与其重放消息配对。
        """
        return tuple(zip(self._state.context_entry_ids, self._state.messages, strict=True))

    # Persist a compaction summary and rebuild the active context state.
    #
    # 持久化压缩摘要并重建活动上下文状态。
    async def _append_compaction(
        self,
        summary: str,
        *,
        first_kept_entry_id: str,
        tokens_before: int | None = None,
        usage: Usage | None = None,
        provider: str | None = None,
        model: str | None = None,
        response_provider: str | None = None,
    ) -> CompactionEntry:
        if first_kept_entry_id not in self._state.context_entry_ids:
            raise ValueError("First kept entry is not in the active context")

        compaction = CompactionEntry(
            parent_id=self._last_parent_id,
            summary=summary,
            first_kept_entry_id=first_kept_entry_id,
            tokens_before=tokens_before,
            usage=usage,
            provider=provider,
            model=model,
            response_provider=response_provider,
        )
        await self._append_session_entry(compaction)
        self._last_parent_id = compaction.id

        await self._refresh_persisted_state(leaf_id=compaction.id)
        self._harness.replace_messages(self._state.messages)
        self._invalidate_context_usage_cache()
        return compaction


# Find the earliest context row that must remain after compaction.
#
# 查找压缩后必须保留的最早上下文行。
def _first_recent_context_index(
    rows: tuple[tuple[str, AgentMessage], ...],
    *,
    keep_recent_tokens: int,
) -> int:
    if keep_recent_tokens <= 0:
        return len(rows)

    accumulated_tokens = 0
    candidate_index: int | None = None
    for index in range(len(rows) - 1, -1, -1):
        _entry_id, message = rows[index]
        accumulated_tokens += estimate_message_tokens(message)
        if accumulated_tokens >= keep_recent_tokens:
            candidate_index = index
            break

    if candidate_index is None:
        return 0

    candidate_message = rows[candidate_index][1]
    if candidate_message.role == "user":
        if candidate_index > 0:
            return candidate_index
        next_user_index = _next_user_message_index(rows, start=1)
        return next_user_index if next_user_index is not None else 0

    next_user_index = _next_user_message_index(rows, start=candidate_index + 1)
    if next_user_index is not None:
        return next_user_index

    for index in range(candidate_index, len(rows)):
        if rows[index][1].role != "toolResult":
            return index
    return len(rows)


# Find the next user message at or after a context index.
#
# 查找指定上下文索引处或之后的下一条用户消息。
def _next_user_message_index(
    rows: tuple[tuple[str, AgentMessage], ...],
    *,
    start: int,
) -> int | None:
    for index in range(start, len(rows)):
        if rows[index][1].role == "user":
            return index
    return None


def is_context_overflow_error(message: AssistantMessage) -> bool:
    """Return True when an assistant error looks like a context overflow.

    当助手错误看起来像上下文溢出时返回 True。
    """
    text = message.error_message or ""
    normalized = text.lower()
    markers = (
        "context length",
        "context window",
        "context limit",
        "maximum context",
        "max context",
        "input is too long",
        "input length",
        "prompt is too long",
        "too many tokens",
        "token limit",
        "exceeds the limit",
        "exceeded the limit",
    )
    return any(marker in normalized for marker in markers)


def is_retryable_huggingface_route_error(message: AssistantMessage) -> bool:
    """Return whether a pre-output Hugging Face HTTP failure is safe to reroute.

    返回输出前发生的 Hugging Face HTTP 失败是否可安全重新路由。
    """
    if message.content:
        return False
    for diagnostic in message.diagnostics or []:
        if diagnostic.type != "provider_error" or diagnostic.details is None:
            continue
        status_code = diagnostic.details.get("status_code")
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code in {408, 409, 425, 429} or status_code >= 500
    return False


def _session_entry_for_message(
    *, parent_id: str | None, message: AgentMessage
) -> MessageEntry | CustomMessageEntry:
    """Build the canonical persisted entry for one completed runtime message.

    为一条已完成的运行时消息构建规范持久化条目。
    """
    if isinstance(message, CustomMessage):
        return CustomMessageEntry(
            parent_id=parent_id,
            timestamp=message.timestamp / 1000,
            custom_type=message.custom_type,
            content=message.content,
            display=message.display,
            details=message.details,
        )
    return MessageEntry(parent_id=parent_id, message=message)


def _detach_missing_parents(entries: list[SessionEntry]) -> list[SessionEntry]:
    """Return entries with dangling parent pointers detached from external history.

    返回已从外部历史分离悬空父指针的条目。
    """
    entry_ids = {entry.id for entry in entries}
    return [
        entry.model_copy(update={"parent_id": None})
        if entry.parent_id is not None and entry.parent_id not in entry_ids
        else entry
        for entry in entries
    ]


# Return the append parent id represented by replayed session state.
#
# 返回重放会话状态所表示的追加父标识符。
def _last_parent_id_from_state(state: SessionState) -> str | None:
    if state.active_leaf_id is not None:
        return state.active_leaf_id
    if state.entries:
        return state.entries[-1].id
    return None


# Find the nearest branchable entry on the active leaf path.
#
# 查找活动叶节点路径上最近的可分支条目。
def _active_branchable_entry_id(
    entries: list[SessionEntry], active_leaf_id: str | None
) -> str | None:
    if active_leaf_id is None:
        return None
    try:
        path = path_to_entry(entries, active_leaf_id)
    except SessionTreeError:
        return active_leaf_id
    return next((entry.id for entry in reversed(path) if _is_branchable_tree_entry(entry)), None)


def _resolved_labels(entries: list[SessionEntry]) -> tuple[dict[str, str], dict[str, float]]:
    """Resolve the latest label change for every target in storage order.

    按存储顺序解析每个目标的最新标签变更。
    """
    labels: dict[str, str] = {}
    timestamps: dict[str, float] = {}
    for entry in entries:
        if not isinstance(entry, LabelEntry):
            continue
        label = entry.label.strip() if entry.label is not None else ""
        if label:
            labels[entry.target_id] = label
            timestamps[entry.target_id] = entry.timestamp
        else:
            labels.pop(entry.target_id, None)
            timestamps.pop(entry.target_id, None)
    return labels, timestamps


# Return whether an entry may be selected as a branch target.
#
# 返回条目是否可选作分支目标。
def _is_branchable_tree_entry(entry: SessionEntry) -> bool:
    if entry.type in {"compaction", "branch_summary"}:
        return True
    if entry.type != "message":
        return False
    return isinstance(entry.message, UserMessage | AssistantMessage)


# Format one tree-picker entry label with branch indentation.
#
# 使用分支缩进格式化一条树选择器标签。
def _tree_choice_label(entry: SessionEntry, *, branch_indent: int = 0) -> str:
    prefix = "  " * branch_indent
    return f"{prefix}{_tree_entry_title(entry)}"


# Compute display indentation for branchable session entries.
#
# 计算可分支会话条目的显示缩进。
def _tree_branch_indents(entries: list[SessionEntry]) -> dict[str, int]:
    return _tree_layout(entries)[1]


# Return session entries in tree-picker traversal order.
#
# 按树选择器遍历顺序返回会话条目。
def _ordered_tree_entries(entries: list[SessionEntry]) -> tuple[SessionEntry, ...]:
    return _tree_layout(entries)[0]


# Build ordered tree entries together with their display indentation.
#
# 构建有序树条目及其显示缩进。
def _tree_layout(
    entries: list[SessionEntry],
) -> tuple[tuple[SessionEntry, ...], dict[str, int]]:
    tree_entries = [entry for entry in entries if entry.type != "leaf"]
    children_by_parent: dict[str | None, list[SessionEntry]] = {}
    entries_by_id: dict[str, SessionEntry] = {}
    for entry in tree_entries:
        children_by_parent.setdefault(entry.parent_id, []).append(entry)
        entries_by_id[entry.id] = entry

    # Resolve each child's longest path to a leaf without recursion. Processing
    # leaves upward also keeps deep sessions safe. Malformed cycles retain a
    # finite fallback length and are handled by the traversal's `seen` set.
    #
    # 以非递归方式解析每个子节点到叶节点的最长路径。自叶向上处理也能保证
    # 深层会话安全。异常循环保留有限的后备长度，并由遍历过程的 `seen` 集合处理。
    branch_lengths = dict.fromkeys(entries_by_id, 1)
    remaining_children = {
        entry_id: len(children_by_parent.get(entry_id, ())) for entry_id in entries_by_id
    }
    pending = [entry_id for entry_id, count in remaining_children.items() if count == 0]
    while pending:
        entry_id = pending.pop()
        parent_id = entries_by_id[entry_id].parent_id
        if parent_id not in remaining_children:
            continue
        branch_lengths[parent_id] = max(branch_lengths[parent_id], branch_lengths[entry_id] + 1)
        remaining_children[parent_id] -= 1
        if remaining_children[parent_id] == 0:
            pending.append(parent_id)

    def ordered_children(parent_id: str | None) -> list[SessionEntry]:
        """Return a parent's children in stable main-branch order.

        按稳定的主分支顺序返回父节点的子节点。
        """
        return sorted(
            children_by_parent.get(parent_id, ()),
            key=lambda child: branch_lengths.get(child.id, 1),
            reverse=True,
        )

    ordered: list[SessionEntry] = []
    indents: dict[str, int] = {}
    seen: set[str] = set()

    def child_stack_items(
        children: list[SessionEntry], parent_indent: int
    ) -> list[tuple[SessionEntry, int]]:
        """Prepare child nodes and indentation for iterative traversal.

        为迭代遍历准备子节点及其缩进。
        """
        if not children:
            return []
        main_child, *alternate_children = children
        display_children = [*alternate_children, main_child]
        return [
            (child, parent_indent if child is main_child else parent_indent + 1)
            for child in reversed(display_children)
        ]

    def append_subtrees(children: list[SessionEntry], parent_indent: int) -> None:
        """Append child subtrees to the traversal stack.

        将子树追加到遍历栈。
        """
        # The longest child is the unindented main branch. Emit shorter siblings
        # immediately after their parent, indented one level, before continuing
        # down the main branch. Stable length sorting preserves storage order
        # when histories have equal lengths.
        #
        # 最长子节点是不缩进的主分支。先在父节点后立即输出缩进一级的较短兄弟
        # 分支，再继续沿主分支向下。稳定的长度排序会在历史长度相同时保留存储顺序。
        stack = child_stack_items(children, parent_indent)
        while stack:
            entry, indent = stack.pop()
            if entry.id in seen:
                continue
            seen.add(entry.id)
            ordered.append(entry)
            indents[entry.id] = indent
            stack.extend(child_stack_items(ordered_children(entry.id), indent))

    append_subtrees(ordered_children(None), 0)
    for entry in tree_entries:
        if entry.id not in seen:
            append_subtrees([entry], 0)
    return tuple(ordered), indents


# Return whether an entry is an assistant message containing only tool calls.
#
# 返回条目是否为仅包含工具调用的助手消息。
def _is_tool_call_tree_entry(entry: SessionEntry) -> bool:
    return (
        entry.type == "message"
        and isinstance(entry.message, AssistantMessage)
        and bool(entry.message.tool_calls)
    )


# Return a concise human-readable title for a tree entry.
#
# 返回树条目的简洁易读标题。
def _tree_entry_title(entry: SessionEntry) -> str:
    match entry.type:
        case "message":
            message = entry.message
            if (
                isinstance(message, AssistantMessage)
                and message.tool_calls
                and not message.text.strip()
            ):
                tool_names = ", ".join(call.name for call in message.tool_calls)
                return f"tool call: {tool_names}"
            return f"{message.role}: {_message_text_preview(message)}"
        case "custom_message":
            return f"custom: {_short_preview(message_text(_custom_message_from_entry(entry)))}"
        case "compaction":
            return f"compaction summary: {_short_preview(entry.summary)}"
        case "branch_summary":
            return f"branch summary: {_short_preview(entry.summary)}"
        case _:
            return entry.type


# Return a compact text preview for an agent message.
#
# 返回代理消息的紧凑文本预览。
def _message_text_preview(message: AgentMessage) -> str:
    return _short_preview(message_text(message))


# Collapse and truncate text for tree-picker display.
#
# 折叠并截断文本以供树选择器显示。
def _short_preview(text: str, *, limit: int = 72) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized or "(empty)"
    return f"{normalized[: limit - 1]}..."


# Return replay messages that follow an entry on the active branch path.
#
# 返回活动分支路径中指定条目之后的重放消息。
def _messages_after_entry_on_active_path(
    entries: list[SessionEntry],
    entry_id: str,
    active_leaf_id: str | None,
) -> tuple[AgentMessage, ...]:
    if active_leaf_id is None:
        return ()
    try:
        active_path = path_to_entry(entries, active_leaf_id)
    except SessionTreeError:
        return ()
    try:
        target_index = next(
            index for index, entry in enumerate(active_path) if entry.id == entry_id
        )
    except StopIteration:
        return ()
    messages: list[AgentMessage] = []
    for entry in active_path[target_index + 1 :]:
        if entry.type == "message":
            messages.append(entry.message)
        elif entry.type == "custom_message":
            messages.append(_custom_message_from_entry(entry))
    return tuple(messages)


# Convert a persisted custom-message entry back into an agent message.
#
# 将持久化自定义消息条目转换回代理消息。
def _custom_message_from_entry(entry: CustomMessageEntry) -> CustomMessage:
    return CustomMessage(
        custom_type=entry.custom_type,
        content=entry.content,
        display=entry.display,
        details=entry.details,
        timestamp=round(entry.timestamp * 1000),
    )


# Return the filesystem path exposed by storage when available.
#
# 返回存储公开的文件系统路径（如果可用）。
def _storage_path(storage: SessionStorage) -> Path | None:
    path = getattr(storage, "path", None)
    return path if isinstance(path, Path) else None


# Resolve the output path for a requested session export.
#
# 解析请求的会话导出输出路径。
def _resolve_export_destination(
    destination: Path | None,
    *,
    cwd: Path,
    session_path: Path | None,
    format: str,
) -> Path:
    if destination is None:
        if session_path is not None:
            return default_session_export_artifact_path(
                session_path,
                destination_dir=cwd,
                format=format,
            )
        return cwd / f"tau-session.{format}"

    resolved = destination if destination.is_absolute() else cwd / destination
    if resolved.suffix:
        return resolved
    name = session_path.stem if session_path is not None else "tau-session"
    return default_session_export_artifact_path(
        Path(name),
        destination_dir=resolved,
        format=format,
    )


# Build the human-readable title used by session exports.
#
# 构建会话导出使用的易读标题。
def _session_export_title(session: CodingSession) -> str:
    manager = session.session_manager
    session_id = session.session_id
    if manager is not None and session_id is not None:
        record = manager.get_session(session_id)
        if record is not None and record.title:
            return record.title
    return f"Tau session {session_id}" if session_id is not None else "Tau Session Export"


@dataclass(frozen=True, slots=True)
class _PreparedProvider:
    """A runtime candidate built after the destination environment is trusted.

    在目标环境获得信任后构建的运行时候选项。
    """

    provider: ClosableModelProvider
    provider_name: str
    model: str
    inference_provider: str | None
    inference_provider_mode: InferenceProviderMode
    runtime_provider_config: ProviderConfig | None
    dynamic_provider: DynamicProvider | None
    runtime_model_catalog: RuntimeModelCatalog | None = None


# Resolve credentials, model metadata, and runtime provider for startup.
#
# 为启动解析凭据、模型元数据和运行时提供者。
async def _prepare_provider_selection(
    config: CodingSessionConfig,
    *,
    state: SessionState,
    provider_registry: DynamicProviderRegistry,
    credential_store: FileCredentialStore | None = None,
) -> _PreparedProvider:
    """Resolve and construct the provider after extension/project staging.

    在扩展和项目暂存后解析并构造提供者。
    """
    requested_provider = config.requested_provider
    requested_model = config.requested_model
    provider_name = requested_provider or state.provider or config.session_provider_name
    explicit = requested_provider is not None or requested_model is not None

    if provider_name is not None:
        effective = provider_registry.effective(provider_name)
        if effective is not None and isinstance(effective.definition, DynamicProvider):
            dynamic = effective.definition
            model = requested_model
            if model is None and state.provider == provider_name:
                model = state.model
            if model is None:
                model = dynamic.default_model
            if model is None and len(dynamic.models) == 1:
                model = dynamic.models[0].id
            if (
                (model is None or not any(item.id == model for item in dynamic.models))
                and dynamic.refresh_models is not None
                and (explicit or state.provider == provider_name)
            ):
                refreshed = await provider_registry.refresh(
                    provider_name,
                    allow_network=True,
                    timeout_seconds=5.0,
                )
                if refreshed.provider is not None:
                    dynamic = refreshed.provider
                if model is None:
                    model = dynamic.default_model
                    if model is None and len(dynamic.models) == 1:
                        model = dynamic.models[0].id
            if model is None:
                raise ProviderConfigError(
                    f"Provider {provider_name} has no selectable models; refresh it explicitly."
                )
            selected = next((item for item in dynamic.models if item.id == model), None)
            if selected is None:
                raise ProviderConfigError(
                    f"Model is not available for provider {provider_name}: {model}"
                )
            try:
                runtime = await create_dynamic_model_provider(
                    dynamic,
                    model=model,
                    credential_store=provider_registry.credentials,
                    environment=provider_registry.environment,
                )
            except (ProviderConfigError, RuntimeError) as exc:
                raise ProviderConfigError(str(exc)) from exc
            return _PreparedProvider(
                provider=runtime,
                provider_name=provider_name,
                model=model,
                inference_provider=None,
                inference_provider_mode="automatic",
                runtime_provider_config=None,
                dynamic_provider=dynamic,
            )

    settings = config.provider_settings
    discovered_catalog: RuntimeModelCatalog | None = None
    if settings is None:
        raise ProviderConfigError(
            f"Provider is not available after trusted extension loading: "
            f"{provider_name or config.model}"
        )
    selected_model = requested_model or (state.model if state.provider == provider_name else None)
    selected_provider = settings.get_provider(provider_name or settings.default_provider)
    if (
        isinstance(selected_provider, OpenAICodexProviderConfig)
        and selected_model is not None
        and selected_model not in selected_provider.models
        and environ.get("TAU_OFFLINE") is None
    ):
        # Live-only explicit/resumed models need discovery before static validation.
        #
        # 仅实时存在的显式或恢复模型需要先发现，再进行静态验证。
        discovery_provider = None
        try:
            discovery_provider = create_model_provider(
                selected_provider,
                credential_store=credential_store,
                model=None,
                thinking_level=None,
            )
            if isinstance(discovery_provider, ModelCatalogProvider):
                catalog = await discovery_provider.discover_models()
                if catalog.models:
                    discovered_catalog = catalog
                    live_provider = _provider_with_runtime_model_catalog(selected_provider, catalog)
                    settings = replace(
                        settings,
                        providers=tuple(
                            live_provider if item.name == selected_provider.name else item
                            for item in settings.providers
                        ),
                    )
        except Exception:  # noqa: BLE001 - retain ordinary static validation on failure
            # 失败时保留常规静态验证流程。
            pass
        finally:
            if discovery_provider is not None:
                await discovery_provider.aclose()
    selection = resolve_provider_selection(
        settings,
        provider_name=provider_name,
        model=selected_model,
    )
    inference_provider = _session_inference_provider(
        config,
        state,
        selection.provider.name,
        selection.model,
    )
    inference_provider_mode = _session_inference_provider_mode(
        config,
        state,
        selection.provider,
        selection.model,
        inference_provider,
    )
    try:
        runtime = create_model_provider(
            selection.provider,
            credential_store=credential_store,
            model=selection.model,
            inference_provider=inference_provider,
            thinking_level=resolve_startup_thinking_level(
                selection.provider,
                selection.model,
            ),
        )
    except RuntimeError as exc:
        raise ProviderConfigError(str(exc)) from exc
    return _PreparedProvider(
        provider=runtime,
        provider_name=selection.provider.name,
        model=selection.model,
        inference_provider=inference_provider,
        inference_provider_mode=inference_provider_mode,
        runtime_provider_config=selection.provider,
        dynamic_provider=None,
        runtime_model_catalog=discovered_catalog,
    )


# Resolve the Hugging Face backing route restored for a session.
#
# 解析为会话恢复的 Hugging Face 后端路由。
def _session_inference_provider(
    config: CodingSessionConfig,
    state: SessionState,
    provider_name: str,
    model: str,
) -> str | None:
    """Preserve HF routing only for the same logical provider/model.

    仅对相同的逻辑提供者和模型保留 Hugging Face 路由。
    """
    if provider_name != "huggingface":
        return None
    if state.provider == provider_name and state.model == model:
        return config.inference_provider
    provider = (
        config.provider_settings.get_provider(provider_name) if config.provider_settings else None
    )
    if isinstance(provider, OpenAICompatibleProviderConfig):
        return provider.inference_providers.get(model)
    return None


# Resolve whether a restored Hugging Face route is automatic or fixed.
#
# 解析恢复的 Hugging Face 路由是自动还是固定模式。
def _session_inference_provider_mode(
    config: CodingSessionConfig,
    state: SessionState,
    provider: ProviderConfig,
    model: str,
    inference_provider: str | None,
) -> InferenceProviderMode:
    """Preserve automatic/fixed HF routing for the same resumed model.

    为相同的恢复模型保留自动或固定 Hugging Face 路由。
    """
    if provider.name != "huggingface":
        return "automatic"
    if state.provider == provider.name and state.model == model:
        return config.inference_provider_mode or (
            "fixed" if inference_provider is not None else "automatic"
        )
    return _configured_inference_provider_mode(provider, model)


# Return configured image support for a provider model when known.
#
# 在已知时返回提供者模型配置的图像支持状态。
def _configured_model_supports_images(config: CodingSessionConfig, model: str) -> bool | None:
    if config.dynamic_provider is not None:
        selected = next((item for item in config.dynamic_provider.models if item.id == model), None)
        if selected is None or selected.input_modalities is None:
            return None
        return "image" in selected.input_modalities
    provider = config.runtime_provider_config
    if provider is None and config.provider_settings is not None:
        try:
            provider = config.provider_settings.get_provider(config.provider_name)
        except ProviderConfigError:
            return None
    return provider_model_supports_images(provider, model) if provider is not None else None


# Resolve the initial model before session-state replay.
#
# 在会话状态重放前解析初始模型。
def _initial_model_for_config(config: CodingSessionConfig) -> str:
    if config.provider_settings is None or config.runtime_provider_config is None:
        return config.model
    provider = _provider_config_for_name(config, config.provider_name)
    if provider is None:
        return config.model
    try:
        validate_provider_model(provider, config.model)
    except ProviderConfigError:
        return provider.default_model
    return config.model


# Resolve the active runtime model from configuration and replayed state.
#
# 根据配置和重放状态解析活动运行时模型。
def _runtime_model_for_state(config: CodingSessionConfig, state: SessionState) -> str:
    state_model = state.model or config.model
    if config.dynamic_provider is not None:
        return config.model
    if config.provider_settings is None or config.runtime_provider_config is None:
        return state_model
    provider = _provider_config_for_name(config, config.provider_name)
    if provider is None:
        return state_model
    try:
        validate_provider_model(provider, state_model)
    except ProviderConfigError:
        return config.model if config.model in provider.models else provider.default_model
    return state_model


# Resolve the startup thinking level for the selected model.
#
# 为选定模型解析启动思考等级。
def _initial_thinking_level_for_config(
    config: CodingSessionConfig,
    *,
    model: str,
) -> ThinkingLevel:
    provider = _provider_config_for_name(config, config.provider_name)
    if config.thinking_level_override is not None:
        if provider is None:
            return config.thinking_level_override
        resolved = resolve_startup_thinking_level(
            provider,
            model,
            cli_override=config.thinking_level_override,
        )
        if resolved is not None:
            return resolved
    if provider is None:
        return config.thinking_level
    return _preferred_thinking_level_for_model(
        provider,
        model=model,
        fallback=config.thinking_level,
    )


# Look up a provider configuration by name without leaking lookup errors.
#
# 按名称查找提供者配置，同时不向外暴露查找错误。
def _provider_config_for_name(
    config: CodingSessionConfig,
    provider_name: str,
) -> ProviderConfig | None:
    if (
        isinstance(config.runtime_provider_config, OpenAICodexProviderConfig)
        and config.runtime_provider_config.name == provider_name
    ):
        return config.runtime_provider_config
    if config.provider_settings is not None:
        try:
            return config.provider_settings.get_provider(provider_name)
        except ProviderConfigError:
            pass
    if config.runtime_provider_config is not None:
        return config.runtime_provider_config
    return None


# Resolve thinking level from replayed state with a validated default.
#
# 根据重放状态和已验证默认值解析思考等级。
def _state_thinking_level(
    state: SessionState,
    default: ThinkingLevel,
) -> ThinkingLevel:
    thinking_level = getattr(state, "thinking_level", None)
    if thinking_level is None:
        return default
    return normalize_thinking_level(thinking_level)


# Construct a concrete model provider from effective configuration.
#
# 根据有效配置构造具体模型提供者。
def _create_runtime_provider(
    provider: ProviderConfig,
    *,
    credential_store: FileCredentialStore,
    model: str,
    thinking_level: ThinkingLevel | None,
    inference_provider: str | None,
    response_headers_observer: Callable[[Mapping[str, str]], None] | None = None,
) -> ClosableModelProvider:
    if inference_provider is None and response_headers_observer is None:
        return create_model_provider(
            provider,
            credential_store=credential_store,
            model=model,
            thinking_level=thinking_level,
        )
    if inference_provider is None:
        return create_model_provider(
            provider,
            credential_store=credential_store,
            model=model,
            thinking_level=thinking_level,
            response_headers_observer=response_headers_observer,
        )
    return create_model_provider(
        provider,
        credential_store=credential_store,
        model=model,
        thinking_level=thinking_level,
        inference_provider=inference_provider,
        response_headers_observer=response_headers_observer,
    )


# Return the configured Hugging Face backing provider for a model.
#
# 返回模型配置的 Hugging Face 后端提供者。
def _configured_inference_provider(
    provider: ProviderConfig | None,
    model: str,
) -> str | None:
    if not isinstance(provider, OpenAICompatibleProviderConfig) or provider.name != "huggingface":
        return None
    return provider.inference_providers.get(model)


# Return automatic or fixed routing mode for configured inference.
#
# 返回已配置推理的自动或固定路由模式。
def _configured_inference_provider_mode(
    provider: ProviderConfig | None,
    model: str,
) -> InferenceProviderMode:
    return "fixed" if _configured_inference_provider(provider, model) is not None else "automatic"


# Return the preferred default thinking level for the active model.
#
# 返回活动模型偏好的默认思考等级。
def _default_thinking_level_for_active_model(session: CodingSession) -> ThinkingLevel:
    provider = session._active_provider_config()
    if provider is None:
        return session._config.thinking_level
    return _preferred_thinking_level_for_model(
        provider,
        model=session.model,
        fallback=session._config.thinking_level,
    )


# Resolve the remembered or configured thinking level for a model.
#
# 解析模型记忆或配置的思考等级。
def _preferred_thinking_level_for_model(
    provider: ProviderConfig,
    *,
    model: str,
    fallback: ThinkingLevel,
) -> ThinkingLevel:
    levels = provider_thinking_levels(provider, model=model)
    preferred = provider.thinking_defaults.get(model)
    if preferred in levels:
        return preferred
    if fallback in levels or not levels:
        return fallback
    default = provider_default_thinking_level(provider, model=model)
    return default or levels[0]


# Coerce a thinking level into the set supported by a model.
#
# 将思考等级调整到模型支持的集合中。
def _coerced_thinking_level(
    provider: ProviderConfig,
    *,
    model: str,
    current: ThinkingLevel,
    preferred: ThinkingLevel | None = None,
) -> ThinkingLevel:
    levels = provider_thinking_levels(provider, model=model)
    if not levels or current in levels:
        return current
    if preferred in levels:
        return preferred
    default = provider_default_thinking_level(provider, model=model)
    return default or levels[0]


# Build the user-facing reason thinking controls are unavailable.
#
# 构建思考控制不可用的用户提示原因。
def _unavailable_thinking_message(session: CodingSession) -> str:
    message = f"Thinking controls are unavailable for {session.provider_name}:{session.model}"
    reason = session.thinking_unavailable_reason
    if reason:
        return f"{message}: {reason}"
    return message


# Sanitize generated text into a valid single-line session name.
#
# 将生成文本清理为有效的单行会话名称。
def _sanitize_session_name(text: str) -> str | None:
    cleaned = " ".join(text.split()).strip()
    cleaned = cleaned.strip("\"'`“”‘’")
    cleaned = cleaned.strip(string.punctuation + " ")
    words = [word.strip(string.punctuation + "\"'`“”‘’") for word in cleaned.split()]
    words = [word for word in words if word]
    if not words:
        return None
    return " ".join(words[:4])


# Derive a fallback session name from the first user message.
#
# 根据首条用户消息派生后备会话名称。
def _fallback_session_name(first_message: str) -> str | None:
    return _sanitize_session_name(first_message)


# Format terminal command output as an agent-context message.
#
# 将终端命令输出格式化为代理上下文消息。
def _terminal_command_context_message(command: str, output: str) -> str:
    return (
        "Terminal command executed by the user.\n\n"
        f"Command:\n```bash\n{command}\n```\n\n"
        f"Output:\n```text\n{output}\n```"
    )


def parse_terminal_command(text: str) -> TerminalCommandRequest | None:
    """Parse input-bar terminal command syntax.

    解析输入栏终端命令语法。
    """
    stripped = text.strip()
    if stripped.startswith("!!"):
        command = stripped[2:].strip()
        if not command:
            return None
        return TerminalCommandRequest(command=command, add_to_context=False)
    if stripped.startswith("!"):
        command = stripped[1:].strip()
        if not command:
            return None
        return TerminalCommandRequest(command=command, add_to_context=True)
    return None


# Compare old and new resource signatures for one reload category.
#
# 比较一个重新加载类别的新旧资源签名。
def _category_summary(
    before: tuple[tuple[object, ...], ...],
    after: tuple[tuple[object, ...], ...],
) -> ReloadCategorySummary:
    return ReloadCategorySummary(
        before=len(before),
        after=len(after),
        changed=before != after,
    )


# Build stable comparison signatures for loaded skills.
#
# 为已加载技能构建稳定的比较签名。
def _skill_signatures(skills: tuple[Skill, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            skill.name,
            str(skill.path),
            skill.description,
            skill.content,
            skill.disable_model_invocation,
        )
        for skill in skills
    )


# Build stable comparison signatures for prompt templates.
#
# 为提示词模板构建稳定的比较签名。
def _prompt_template_signatures(
    prompt_templates: tuple[PromptTemplate, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (template.name, str(template.path), template.description, template.content)
        for template in prompt_templates
    )


# Build stable comparison signatures for project context files.
#
# 为项目上下文文件构建稳定的比较签名。
def _context_file_signatures(
    context_files: tuple[ProjectContextFile, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple((context_file.path, context_file.content) for context_file in context_files)


# Build stable comparison signatures for resource diagnostics.
#
# 为资源诊断信息构建稳定的比较签名。
def _diagnostic_signatures(
    diagnostics: tuple[ResourceDiagnostic, ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            diagnostic.kind,
            diagnostic.message,
            str(diagnostic.path) if diagnostic.path is not None else None,
            diagnostic.name,
            diagnostic.severity,
        )
        for diagnostic in diagnostics
    )


# Build stable comparison signatures for loaded extensions.
#
# 为已加载扩展构建稳定的比较签名。
def _extension_signatures(runtime: ExtensionRuntime) -> tuple[tuple[object, ...], ...]:
    return tuple((name,) for name in runtime.extension_names)


# Build comparison signatures for files contributing to the system prompt.
#
# 为参与系统提示词的文件构建比较签名。
def _system_prompt_resource_signatures(
    *,
    skills: tuple[Skill, ...],
    context_files: tuple[ProjectContextFile, ...],
    custom_system_prompt: str | None,
    custom_system_prompt_path: Path | None,
    append_system_prompt: str | None,
    append_system_prompt_paths: tuple[Path, ...],
) -> tuple[object, ...]:
    prompt_skills = tuple(
        (skill.name, str(skill.path), skill.description, skill.disable_model_invocation)
        for skill in sorted(skills, key=lambda item: item.name)
    )
    return (
        prompt_skills,
        _context_file_signatures(context_files),
        custom_system_prompt,
        str(custom_system_prompt_path) if custom_system_prompt_path is not None else None,
        append_system_prompt,
        tuple(str(path) for path in append_system_prompt_paths),
    )


# Return a provider configuration, mapping unknown names to None.
#
# 返回提供者配置，并将未知名称映射为 None。
def _provider_config_or_none(
    settings: ProviderSettings | None,
    provider_name: str | None,
) -> ProviderConfig | None:
    if settings is None or provider_name is None:
        return None
    try:
        return settings.get_provider(provider_name)
    except ProviderConfigError:
        return None


# Resolve the authenticated Codex account id for cache scoping.
#
# 解析已认证 Codex 账户标识符以限定缓存范围。
def _codex_account_id(
    provider: OpenAICodexProviderConfig,
    credential_store: FileCredentialStore,
) -> str | None:
    if provider.credential_name:
        credential = credential_store.get_oauth(provider.credential_name)
        if credential is not None and credential.account_id:
            return credential.account_id
    return account_id_from_access_token(environ.get(provider.api_key_env, ""))


# Overlay all runtime model catalogs onto durable provider settings.
#
# 将全部运行时模型目录叠加到持久化提供者设置。
def _provider_settings_with_runtime_catalogs(
    settings: ProviderSettings | None,
    catalogs: Mapping[str, RuntimeModelCatalog],
) -> ProviderSettings | None:
    if settings is None or not catalogs:
        return settings
    providers = tuple(
        _provider_with_runtime_model_catalog(provider, catalogs.get(provider.name))
        for provider in settings.providers
    )
    return replace(settings, providers=providers)


# Apply one runtime model catalog to a provider configuration.
#
# 将一个运行时模型目录应用到提供者配置。
def _provider_with_runtime_model_catalog(
    provider: ProviderConfig,
    catalog: RuntimeModelCatalog | None,
) -> ProviderConfig:
    """Overlay one account-specific catalog without changing durable settings.

    叠加一个账户专属目录，而不更改持久化设置。
    """
    if catalog is None or not isinstance(provider, OpenAICodexProviderConfig):
        return provider

    models = tuple(model.id for model in catalog.models)
    metadata = {
        model.id: _runtime_provider_model_metadata(provider, model) for model in catalog.models
    }
    context_windows = {
        model.id: (
            model.limits.context_window
            if model.limits is not None
            else provider.context_windows[model.id]
        )
        for model in catalog.models
        if model.limits is not None or model.id in provider.context_windows
    }
    return replace(
        provider,
        models=models,
        default_model=(provider.default_model if provider.default_model in models else models[0]),
        context_windows=context_windows,
        model_metadata=metadata,
        thinking_models=tuple(model.id for model in catalog.models if model.thinking_levels),
        thinking_defaults=_runtime_model_thinking_defaults(provider, catalog),
    )


# Extract per-model thinking defaults from a runtime catalog.
#
# 从运行时目录提取每模型思考默认值。
def _runtime_model_thinking_defaults(
    provider: OpenAICodexProviderConfig,
    catalog: RuntimeModelCatalog,
) -> dict[str, ThinkingLevel]:
    defaults: dict[str, ThinkingLevel] = {}
    for model in catalog.models:
        default = provider.thinking_defaults.get(model.id) or model.default_thinking_level
        if default is not None:
            defaults[model.id] = default
    return defaults


# Convert runtime catalog entries into provider model metadata.
#
# 将运行时目录条目转换为提供者模型元数据。
def _runtime_provider_model_metadata(
    provider: OpenAICodexProviderConfig,
    model: RuntimeModel,
) -> ProviderModelMetadata:
    existing = provider.model_metadata.get(model.id, ProviderModelMetadata())
    supported = set(model.thinking_levels)
    thinking_level_map = {
        level: ("none" if level == "off" else level) if level in supported else None
        for level in THINKING_LEVELS
    }
    return replace(
        existing,
        name=model.name or existing.name,
        reasoning=True if model.thinking_levels else existing.reasoning,
        input=model.input_modalities,
        context_window=(
            model.limits.context_window if model.limits is not None else existing.context_window
        ),
        max_tokens=(
            model.limits.max_output_tokens
            if model.limits is not None and model.limits.max_output_tokens is not None
            else existing.max_tokens
        ),
        thinking_level_map=thinking_level_map,
    )


# Discover skills, prompts, context, and system-prompt resources for a session.
#
# 为会话发现技能、提示词、上下文和系统提示词资源。
def _load_session_resources(
    resource_paths: TauResourcePaths,
    explicit_context_files: tuple[ProjectContextFile, ...],
    *,
    skills_enabled: bool = True,
    system_prompt_enabled: bool = True,
    custom_system_prompt_explicit: bool = False,
) -> SessionResources:
    loaded_skills: list[Skill]
    skill_diagnostics: list[ResourceDiagnostic]
    if skills_enabled:
        loaded_skills, skill_diagnostics = load_skills_with_diagnostics(resource_paths)
    else:
        loaded_skills, skill_diagnostics = [], []
    loaded_prompt_templates, prompt_diagnostics = load_prompt_templates_with_diagnostics(
        resource_paths
    )
    discovered_context, context_diagnostics = discover_project_context_with_diagnostics(
        resource_paths
    )
    system_prompts = discover_system_prompt_resources(
        resource_paths,
        custom_prompt_explicit=custom_system_prompt_explicit,
        enabled=system_prompt_enabled,
    )
    return SessionResources(
        skills=tuple(loaded_skills),
        prompt_templates=tuple(loaded_prompt_templates),
        context_files=_merge_context_files(explicit_context_files, discovered_context),
        custom_system_prompt=system_prompts.custom_prompt,
        custom_system_prompt_path=system_prompts.custom_prompt_path,
        append_system_prompt=system_prompts.append_prompt,
        append_system_prompts=system_prompts.append_prompts,
        append_system_prompt_paths=system_prompts.append_prompt_paths,
        diagnostics=tuple(
            [
                *skill_diagnostics,
                *prompt_diagnostics,
                *context_diagnostics,
                *system_prompts.diagnostics,
            ]
        ),
    )


# Pair appended system prompts with their source paths and ordering.
#
# 将追加的系统提示词与其来源路径和顺序配对。
def _append_prompt_sections(
    prompts: tuple[str, ...],
    paths: tuple[Path, ...],
    explicit_prompt: str | None,
) -> tuple[PromptSection, ...]:
    """Pair append content with its file or CLI origin in composition order.

    按组合顺序将追加内容与其文件或命令行来源配对。
    """
    sections = [
        PromptSection(title=None, body=prompt, source=str(path))
        for prompt, path in zip(prompts, paths, strict=True)
    ]
    if explicit_prompt is not None:
        sections.append(
            PromptSection(
                title=None,
                body=explicit_prompt,
                source="CLI --append-system-prompt",
            )
        )
    return tuple(sections)


# Describe the source of an explicit or file-backed custom system prompt.
#
# 描述显式或文件提供的自定义系统提示词来源。
def _custom_prompt_source(*, explicit: bool, path: Path | None) -> str:
    if explicit:
        return "CLI --system-prompt"
    return str(path) if path is not None else "runtime configuration"


# Merge configured and discovered context files without duplicate paths.
#
# 合并已配置和已发现的上下文文件，并按路径去重。
def _merge_context_files(
    explicit: tuple[ProjectContextFile, ...],
    discovered: tuple[ProjectContextFile, ...],
) -> tuple[ProjectContextFile, ...]:
    merged: list[ProjectContextFile] = []
    seen: set[str] = set()
    for context_file in (*explicit, *discovered):
        if context_file.path in seen:
            continue
        seen.add(context_file.path)
        merged.append(context_file)
    return tuple(merged)


# Build a durable repair plan for malformed tool-call history.
#
# 为格式异常的工具调用历史构建持久化修复计划。
def _tool_history_repair_plan(
    messages: tuple[AgentMessage, ...],
    *,
    context_entry_ids: tuple[str, ...],
    entries: tuple[SessionEntry, ...],
) -> tuple[str | None, tuple[AgentMessage, ...], ToolHistoryRepair] | None:
    repair = repair_tool_history(messages)
    if not repair.changed:
        return None

    common_prefix_length = 0
    for old_message, repaired_message in zip(messages, repair.messages, strict=False):
        if old_message != repaired_message:
            break
        common_prefix_length += 1

    if common_prefix_length > 0:
        parent_id: str | None = context_entry_ids[common_prefix_length - 1]
    elif context_entry_ids:
        entries_by_id = {entry.id: entry for entry in entries}
        first_entry = entries_by_id.get(context_entry_ids[0])
        parent_id = first_entry.parent_id if first_entry is not None else None
    else:
        parent_id = None

    return parent_id, repair.messages[common_prefix_length:], repair


def default_session_path(cwd: Path) -> Path:
    """Return Tau's default user-home session path for a project cwd.

    返回项目工作目录对应的 Tau 默认用户主目录会话路径。
    """
    return TauPaths().default_session_path(cwd)


def jsonl_session_storage(path: str | Path) -> JsonlSessionStorage:
    """Convenience factory for local JSONL coding-session storage.

    用于本地 JSONL 编码会话存储的便捷工厂函数。
    """
    return JsonlSessionStorage(path)


def _append_session_entry_sync(storage: SessionStorage, entry: SessionEntry) -> None:
    """Append an entry synchronously for slash commands that cannot await storage.

    为无法等待存储的斜杠命令同步追加条目。
    """
    if isinstance(storage, JsonlSessionStorage):
        storage.path.parent.mkdir(parents=True, exist_ok=True)
        with storage.path.open("a", encoding="utf-8") as file:
            file.write(entry_to_json_line(entry))
        return
    raise RuntimeError("Session storage does not support synchronous initialization")
