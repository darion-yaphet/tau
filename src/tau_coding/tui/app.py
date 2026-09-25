"""Minimal Textual app for Tau coding sessions.

用于 Tau 编码会话的精简 Textual 应用。
"""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import tempfile
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, auto
from inspect import isawaitable
from io import StringIO
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal, Protocol, TypeVar, cast

from rich.console import Console, Group
from rich.style import Style
from rich.text import Text
from textual import constants as textual_constants
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingsMap
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.events import Key, Resize
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    Static,
    TextArea,
)
from textual.worker import Worker

from tau_agent.events import (
    AgentEndEvent,
    AgentStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
    ToolExecutionUpdateEvent,
)
from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    TextContent,
    ThinkingContent,
    UserMessage,
)
from tau_agent.provider import CancellationToken
from tau_agent.provider_events import (
    AssistantErrorEvent,
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ThinkingEndEvent,
)
from tau_agent.tools import AgentTool
from tau_agent.types import JSONValue
from tau_coding.catalog_loader import save_user_catalog_entries
from tau_coding.commands import (
    LOGIN_PROVIDER_ALIASES,
    CommandRegistry,
    create_default_command_registry,
    format_reload_summary,
)
from tau_coding.credentials import FileCredentialStore, OAuthCredential
from tau_coding.events import (
    AgentSettledEvent,
    AutoRetryStartEvent,
    CodingSessionEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    QueueUpdateEvent,
)
from tau_coding.extensions.api import (
    KeyInterceptor,
    MainViewFactory,
    MainViewHandle,
    Placement,
    SidebarContent,
    SlotWidgetContent,
    SlotWidgetFactory,
)
from tau_coding.oauth import login_openai_codex
from tau_coding.oauth_registry import get_oauth_provider, oauth_provider_ids
from tau_coding.oauth_types import (
    OAuthAuthInfo,
    OAuthDeviceCodeInfo,
    OAuthLoginCallbacks,
    OAuthPrompt,
    OAuthSelectPrompt,
)
from tau_coding.project_trust import ProjectTrustRequest, TrustChoice, TrustOverride
from tau_coding.prompt_templates import PromptTemplate
from tau_coding.provider_catalog import (
    BUILTIN_PROVIDER_CATALOG,
    ProviderCatalogEntry,
    builtin_provider_entry,
)
from tau_coding.provider_config import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER_NAME,
    OpenAICompatibleProviderConfig,
    ProviderConfig,
    ProviderConfigError,
    ProviderSelection,
    load_provider_settings,
    provider_config_from_catalog_entry,
    provider_has_usable_credentials,
    resolve_provider_selection,
    resolve_startup_thinking_level,
    save_provider_settings,
    upsert_openai_compatible_provider,
    upsert_saved_provider,
)
from tau_coding.provider_runtime import ClosableModelProvider, create_model_provider
from tau_coding.resources import ResourceDiagnostic, TauResourcePaths
from tau_coding.session import (
    TREE_RUNNING_MESSAGE,
    CodingSession,
    CodingSessionConfig,
    ModelChoice,
    SessionTreeBranchResult,
    SessionTreeChoice,
    is_context_overflow_error,
    jsonl_session_storage,
    parse_terminal_command,
)
from tau_coding.session_manager import CodingSessionRecord, SessionManager
from tau_coding.session_preparation import prepare_coding_session
from tau_coding.shell_config import load_shell_settings
from tau_coding.skills import Skill
from tau_coding.system_prompt import SystemPromptInspection
from tau_coding.thinking import ThinkingLevel
from tau_coding.tui.adapter import TuiEventAdapter
from tau_coding.tui.autocomplete import (
    CompletionItem,
    CompletionKind,
    CompletionOption,
    CompletionState,
    build_completion_state,
)
from tau_coding.tui.config import (
    TAU_DARK_THEME,
    TuiKeybindings,
    TuiSettings,
    TuiTheme,
    TuiThemeName,
    load_tui_settings,
    save_tui_settings,
)
from tau_coding.tui.file_drop import normalize_dropped_paths
from tau_coding.tui.local_backends import (
    LocalBackendPickerScreen,
    LocalBackendScreen,
    LocalChoiceConfirmScreen,
    LocalConfirmScreen,
    LocalSearchResultsScreen,
)
from tau_coding.tui.project_trust import ProjectTrustScreen, prompt_project_trust
from tau_coding.tui.state import TuiState, format_terminal_command_result_block
from tau_coding.tui.terminal_notification import TerminalNotificationController
from tau_coding.tui.terminal_title import TerminalTitleController
from tau_coding.tui.themes import (
    available_tui_theme_names,
    load_custom_tui_themes,
    set_custom_tui_themes,
    textual_theme_for_tui_theme,
    theme_css_variables,
)
from tau_coding.tui.widgets import (
    CompactSessionInfo,
    SessionSidebar,
    SidebarFileItem,
    TranscriptView,
    _custom_markup_to_text,
    _sidebar_separator,
    render_completion_suggestions,
)

_textual_theme_for_tau_theme = textual_theme_for_tui_theme
_theme_css_variables = theme_css_variables

type BindingEntry = Binding | tuple[str, str] | tuple[str, str, str]
SIDEBAR_MIN_WIDTH = 96
SIDEBAR_MIN_HEIGHT = 38
ACTIVITY_TICK_SECONDS = 0.15
ACTIVITY_COLOR_FADE_STEPS = 24
ACTIVITY_INDICATOR_HEIGHT = 3
COMPLETION_MAX_VISIBLE_LINES = 16
COMPLETION_INITIAL_TERMINAL_FRACTION = 3
COMPLETION_MIN_TRANSCRIPT_LINES = 4
COMPLETION_WIDGET_CHROME_LINES = 3
NO_STORED_CREDENTIALS_MESSAGE = (
    "No stored credentials to remove. /logout only removes credentials saved by /login; "
    "environment variables and providers.json config are unchanged."
)


def _configure_herdr_textual_mouse() -> None:
    """Keep Textual on cell mouse coordinates inside affected Herdr versions.

    在受影响的 Herdr 版本中让 Textual 继续使用单元格鼠标坐标。
    """
    if os.environ.get("HERDR_ENV") != "1" or "TEXTUAL_SMOOTH_SCROLL" in os.environ:
        return
    os.environ["TEXTUAL_SMOOTH_SCROLL"] = "0"
    # Textual reads this environment variable while importing constants, before
    # Tau reaches the TUI runner. Update the loaded value for this process too.
    #
    # Textual 在导入常量时就读取此环境变量，早于 Tau 到达 TUI 运行器。因此还要
    # 更新当前进程中已经加载的值。
    textual_constants.SMOOTH_SCROLL = False  # type: ignore[misc]


class LoginRequiredProvider:
    """Placeholder provider used so the TUI can open before login.

    用于让 TUI 在登录前即可打开的占位提供者。
    """

    def __init__(self, message: str) -> None:
        """Initialize the placeholder with the login error to surface.

        使用需要显示的登录错误初始化占位提供者。
        """
        self.message = message

    async def aclose(self) -> None:
        """Close provider resources.

        关闭提供者资源。
        """

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Surface a login-needed provider error.

        向上报告需要登录的提供者错误。
        """
        del system, messages, tools, signal, session_id

        async def iterator() -> AsyncIterator[AssistantMessageEvent]:
            """Yield the stored login error as one terminal assistant event.

            将已存储的登录错误作为一个终止助手事件产出。
            """
            error = AssistantMessage(
                model=model,
                stop_reason="error",
                error_message=self.message,
            )
            yield AssistantErrorEvent(reason="error", error=error)

        return iterator()


_DialogResult = TypeVar("_DialogResult")


@dataclass(frozen=True, slots=True)
class _SidebarContribution:
    """One extension-owned sidebar section retained for theme rebuilds.

    为主题重建保留的一个扩展所属侧边栏区段。
    """

    title: str
    content: SidebarContent


class _TuiExtensionUiBridge:
    """Route extension UI requests to the running Textual app.

    将扩展界面请求路由到正在运行的 Textual 应用。
    """

    _SEVERITIES: ClassVar[dict[str, Literal["information", "warning", "error"]]] = {
        "info": "information",
        "warning": "warning",
        "error": "error",
    }

    def __init__(self, app: TauTuiApp) -> None:
        """Bind extension UI operations to the running Tau TUI app.

        将扩展界面操作绑定到正在运行的 Tau TUI 应用。
        """
        self._app = app

    @property
    def has_ui(self) -> bool:
        """Return True: an interactive TUI is attached.

        返回 True，表示已附加交互式 TUI。
        """
        return True

    def notify(self, message: str, level: str = "info") -> None:
        """Show an extension notification through the app's dedupe path.

        通过应用的去重路径显示扩展通知。
        """
        self._app._notify(message, severity=self._SEVERITIES.get(level, "information"))

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Show a modal picker; return the choice, or None on cancel/timeout.

        显示模态选择器；返回所选项，取消或超时时返回 None。
        """
        theme = self._app.tui_settings.resolved_theme
        screen: ModalScreen[str | None] = ExtensionSelectScreen(title, options, theme=theme)
        return await self._run_dialog(screen, default=None, timeout=timeout)

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Show a modal confirmation; True only if confirmed.

        显示模态确认框；仅在确认时返回 True。
        """
        theme = self._app.tui_settings.resolved_theme
        screen: ModalScreen[bool] = ExtensionConfirmScreen(title, message, theme=theme)
        return await self._run_dialog(screen, default=False, timeout=timeout)

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Show a modal text prompt; return the text, or None on cancel/timeout.

        显示模态文本提示框；返回输入文本，取消或超时时返回 None。
        """
        theme = self._app.tui_settings.resolved_theme
        screen: ModalScreen[str | None] = ExtensionInputScreen(title, placeholder, theme=theme)
        return await self._run_dialog(screen, default=None, timeout=timeout)

    # -- component seam -- pass-through to the app ----------------------------
    #
    # 组件接口：直接转发到应用。

    @property
    def supports_components(self) -> bool:
        """Return True: a Textual TUI can host extension widgets.

        返回 True，表示 Textual TUI 可以承载扩展小组件。
        """
        return True

    @property
    def theme(self) -> TuiTheme:
        """Return the live TUI theme handed to widget factories.

        返回传给小组件工厂的实时 TUI 主题。
        """
        return self._app.tui_settings.resolved_theme

    def get_prompt_text(self) -> str:
        """Return the current prompt-editor text (Pi's getEditorText).

        返回当前提示词编辑器文本，对应 Pi 的 getEditorText。

        Interceptors do not need this — the host passes the prompt text as
        their second argument; it exists for reads outside the key path.

        拦截器不需要调用它，因为宿主会把提示词文本作为第二个参数传入；该方法
        用于按键路径之外的读取。
        """
        return self._app._current_prompt_text()

    def request_render(self) -> None:
        """Re-render mounted extension widgets (analog of Pi's requestRender).

        重新渲染已挂载的扩展小组件，类似 Pi 的 requestRender。
        """
        self._app._refresh_extension_components()

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:
        """Mount or remove an extension slot widget by key (factory or lines).

        按键挂载或移除扩展槽位小组件，可使用工厂或文本行。
        """
        self._app._set_extension_slot_widget(key, content, placement)

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:
        """Open a full main-area extension view (display-toggled, not modal).

        打开完整的主区域扩展视图，通过显示状态切换，而非模态方式。
        """
        return self._app._open_extension_main_view(factory)

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:
        """Register a pre-dispatch key hook; return an unsubscribe callable.

        注册分派前按键钩子，并返回取消订阅的可调用对象。

        Ports Pi's ``onTerminalInput``. The handler is consulted in
        ``TauTuiApp.on_event`` before Textual's app-level priority bindings and
        before the focused widget receives the key, so it can own navigation
        keys (``up``/``down``/``tab``/…) that tau otherwise binds with
        ``priority=True``. Because it fires for EVERY main-screen key regardless
        of which widget holds focus, the handler MUST self-gate (e.g. on the
        prompt text and its own state) and return ``True`` only for keys it
        actually consumes. It is never consulted while a modal screen (dialog,
        picker, command palette) is on top.

        此方法移植 Pi 的 ``onTerminalInput``。处理器会在 ``TauTuiApp.on_event``
        中、Textual 的应用级优先绑定以及聚焦小组件接收按键之前被调用，因此可接管
        Tau 原本以高优先级绑定的导航键。由于无论哪个小组件聚焦，它都会对每个主
        屏幕按键触发，处理器必须自行判断是否适用，并且仅对实际消费的按键返回
        ``True``。模态屏幕位于顶层时不会调用它。
        """
        return self._app._register_extension_key_interceptor(handler)

    @property
    def supports_sidebar(self) -> bool:
        """Return whether the configured TUI sidebar can host sections.

        返回已配置的 TUI 侧边栏是否可以承载区段。
        """
        return self._app.tui_settings.sidebar_position != "off"

    def set_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:
        """Add or replace one host-framed extension sidebar section.

        添加或替换一个由宿主提供边框的扩展侧边栏区段。
        """
        self._app._set_extension_sidebar_section(
            extension_name,
            key,
            title=title,
            content=content,
        )

    def remove_sidebar_section(self, extension_name: str, key: str) -> None:
        """Remove one extension-owned sidebar section.

        移除一个扩展所属的侧边栏区段。
        """
        self._app._remove_extension_sidebar_section(extension_name, key)

    def clear_components(self) -> None:
        """Tear down all extension-owned UI (runtime-driven: /reload, rebind).

        拆除所有扩展所属界面，由运行时在重新加载或重新绑定时驱动。
        """
        self._app._clear_extension_components()

    async def _run_dialog(
        self,
        screen: ModalScreen[_DialogResult],
        *,
        default: _DialogResult,
        timeout: float | None,
    ) -> _DialogResult:
        """Push a modal and await its dismissal via a callback-resolved future.

        推入模态界面，并通过回调解析的 future 等待其关闭。

        Uses ``push_screen(screen, callback)`` + an ``asyncio.Future`` rather
        than ``push_screen_wait`` (which requires a Textual worker context);
        this pattern works from any coroutine on the app's event loop,
        including a task spawned by a sync ``/command`` handler. On ``timeout``
        (seconds) the dialog auto-dismisses and the no-op ``default`` returns.

        此处使用 ``push_screen(screen, callback)`` 加 ``asyncio.Future``，而不是
        要求 Textual worker 上下文的 ``push_screen_wait``。该模式可从应用事件
        循环上的任何协程使用，包括同步斜杠命令处理器启动的任务。达到以秒为单位
        的 ``timeout`` 后，对话框会自动关闭并返回空操作 ``default``。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_DialogResult] = loop.create_future()

        def _resolve(result: _DialogResult | None) -> None:
            """Resolve the dialog future once with its result or safe default.

            使用对话框结果或安全默认值仅解析一次 future。
            """
            # Textual passes None when a screen is dismissed with no value;
            # map that (and any explicit cancel) to the no-op default.
            #
            # Textual 在屏幕无返回值关闭时传入 None；将该情况以及任何显式取消
            # 映射为空操作默认值。
            if not future.done():
                future.set_result(default if result is None else result)

        self._app.push_screen(screen, _resolve)
        if timeout is None:
            return await future
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            # `Screen.dismiss` only works while the dialog is the top screen.
            # Known limitation: if another screen was pushed on top before the
            # timeout fired, the stale dialog stays on the stack (its future
            # result is discarded by `_resolve` racing `future.done()`) until
            # the covering screen closes and the user dismisses it manually.
            #
            # `Screen.dismiss` 只能在对话框位于最上层时生效。已知限制：如果超时前
            # 有其他屏幕被推到其上方，旧对话框会继续留在栈中（其结果会在 `_resolve`
            # 与 `future.done()` 竞争时被丢弃），直到覆盖它的屏幕关闭且用户手动将其关闭。
            if screen.is_current:
                with suppress(Exception):
                    screen.dismiss(default)
            return default


class _MainViewHandle:
    """Host-side handle to an open extension main view.

    已打开扩展主视图的宿主侧句柄。

    ``close(result)`` is idempotent and routes back to the app, which unmounts
    the widget and restores the main transcript; it also resolves ``wait()``
    with ``result`` (Pi's ``done(result)``). Every other teardown path the host
    owns — session rebind, quarantine, being superseded by a later
    ``open_main_view`` — resolves ``wait()`` with ``None`` via
    :meth:`_resolve`, so an awaiting extension task never hangs.

    ``close(result)`` 是幂等的，并会返回应用，由应用卸载小组件、恢复主会话记录，
    同时用 ``result`` 解析 ``wait()``。宿主管理的其他拆除路径会通过
    :meth:`_resolve` 用 ``None`` 解析 ``wait()``，因此等待中的扩展任务不会挂起。
    """

    def __init__(self, app: TauTuiApp, result: asyncio.Future[object | None]) -> None:
        """Track one open extension main view and its completion future.

        跟踪一个已打开的扩展主视图及其完成 future。
        """
        self._app = app
        self._open = True
        self.widget: Widget | None = None
        # Created on the app's event loop at open time; resolved exactly once by
        # the first teardown (close/clear/quarantine/supersede) to wake wait().
        #
        # 在应用事件循环中打开时创建；首次拆除时仅解析一次以唤醒 wait()。
        self._result = result

    def close(self, result: object | None = None) -> None:
        """Close the view, resolving ``wait()`` with ``result`` (safe to repeat).

        关闭视图，并用 ``result`` 解析 ``wait()``；可安全重复调用。
        """
        if not self._open:
            return
        self._open = False
        self._resolve(result)
        self._app._close_extension_main_view(self)

    def _resolve(self, result: object | None) -> None:
        """Resolve the pending ``wait()`` future once; later calls are no-ops.

        仅解析一次待处理的 ``wait()`` future；后续调用不执行操作。
        """
        if not self._result.done():
            self._result.set_result(result)

    async def wait(self) -> object | None:
        """Await teardown and return the ``close`` result (``None`` if cleared).

        等待拆除并返回 ``close`` 结果；被清除时返回 ``None``。
        """
        return await self._result

    @property
    def is_open(self) -> bool:
        """Return whether the view is still open.

        返回视图是否仍处于打开状态。
        """
        return self._open


class _DeadMainViewHandle:
    """A no-op main-view handle returned when a view could not be opened.

    无法打开视图时返回的空操作主视图句柄。
    """

    def close(self, result: object | None = None) -> None:
        """Do nothing: there is no view to close (``result`` is ignored).

        不执行操作：没有可关闭的视图，``result`` 会被忽略。
        """

    async def wait(self) -> object | None:
        """Return None immediately: a dead handle never opens a view.

        立即返回 None：失效句柄永远不会打开视图。
        """
        return None

    @property
    def is_open(self) -> bool:
        """Return False: a dead handle is never open.

        返回 False：失效句柄永远不会处于打开状态。
        """
        return False


class CompletionActionTarget(Protocol):
    """App actions used by the prompt input completion bindings.

    提示词输入补全绑定使用的应用操作。
    """

    # Accept the currently selected completion.

    # 接受当前选中的补全项。
    def action_accept_completion(self) -> None: ...

    # Cancel the current completion or running interaction.

    # 取消当前补全或正在进行的交互。
    def action_cancel(self) -> None: ...

    # Move to the next completion candidate.

    # 移动到下一个补全候选项。
    def action_completion_next(self) -> None: ...

    # Move to the previous completion candidate.

    # 移动到上一个补全候选项。
    def action_completion_previous(self) -> None: ...

    # Open the command palette.

    # 打开命令面板。
    def action_open_command_palette(self) -> None: ...

    # Open the resumable-session picker.

    # 打开可恢复会话选择器。
    def action_open_session_picker(self) -> None: ...

    # Cycle to the next thinking level.

    # 循环切换到下一个思考等级。
    def action_cycle_thinking(self) -> None: ...

    # Cycle forward through scoped models.

    # 向前循环切换限定模型。
    def action_cycle_model(self) -> None: ...

    # Cycle backward through scoped models.

    # 向后循环切换限定模型。
    def action_cycle_model_reverse(self) -> None: ...

    # Toggle tool-result visibility.

    # 切换工具结果可见性。
    def action_toggle_tool_results(self) -> None: ...

    # Toggle thinking-token visibility.

    # 切换思考令牌可见性。
    def action_toggle_thinking(self) -> None: ...

    # Move a queued message back into the editor when available.

    # 在可用时将排队消息移回编辑器。
    def action_edit_queued_message(self) -> bool: ...

    # Submit the current prompt as a new turn.

    # 将当前提示词作为新轮次提交。
    async def action_submit_prompt(self) -> None: ...

    # Submit the current prompt as a follow-up message.

    # 将当前提示词作为后续消息提交。
    async def action_submit_follow_up(self) -> None: ...


class SessionCompletionRecord(Protocol):
    """Session metadata needed to render resume picker completions.

    渲染恢复选择器补全项所需的会话元数据。
    """

    id: str
    title: str | None
    model: str
    cwd: Path
    updated_at: float


PASTE_DISPLAY_THRESHOLD = 2_000


class PromptInput(TextArea):
    """Multiline prompt input with completion key bindings.

    带补全按键绑定的多行提示词输入框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = []
    shell_mode_style: str = ""

    def __init__(
        self,
        *,
        tui_keybindings: TuiKeybindings | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize a themed modal option picker with its choices.

        使用选项和主题初始化模态选项选择器。
        """
        """Initialize prompt editing, completion routing, and paste tracking.

        初始化提示词编辑、补全路由和粘贴内容跟踪。
        """
        kwargs.setdefault("highlight_cursor_line", False)
        super().__init__(**kwargs)
        self.tui_keybindings = tui_keybindings or TuiKeybindings()
        self._base_bindings = self._bindings.copy()
        self._footer_mode: Literal["normal", "completion", "file_completion", "running"] = "normal"
        self._pending_pastes: list[tuple[str, str]] = []
        self._paste_placeholder_counter = 0
        self._apply_prompt_bindings()

    def set_footer_mode(
        self,
        mode: Literal["normal", "completion", "file_completion", "running"],
    ) -> None:
        """Switch the prompt bindings shown by Textual's built-in footer.

        切换 Textual 内置页脚显示的提示词按键绑定。
        """
        if mode == self._footer_mode:
            return
        self._footer_mode = mode
        self._apply_prompt_bindings()
        self.refresh_bindings()

    def _apply_prompt_bindings(self) -> None:
        """Install the key bindings for the current footer mode.

        安装当前页脚模式对应的按键绑定。
        """
        self._bindings = BindingsMap.merge(
            [
                self._base_bindings,
                BindingsMap(_prompt_bindings(self.tui_keybindings, mode=self._footer_mode)),
            ]
        )

    @property
    def value(self) -> str:
        """Compatibility alias for tests and code that previously used Input.value.

        为先前使用 Input.value 的测试和代码提供兼容别名。
        """
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        """Replace prompt text through the Input-compatible alias.

        通过兼容 Input 的别名替换提示词文本。
        """
        self.text = text

    @property
    def cursor_position(self) -> int:
        """Return a flat cursor offset for Input compatibility.

        返回扁平光标偏移量，以兼容 Input。
        """
        row, column = self.cursor_location
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + column

    @cursor_position.setter
    def cursor_position(self, offset: int) -> None:
        """Move the cursor from a flat Input-compatible offset.

        根据兼容 Input 的扁平偏移量移动光标。
        """
        text = self.text
        bounded = max(0, min(offset, len(text)))
        before = text[:bounded]
        self.move_cursor((before.count("\n"), len(before.rsplit("\n", 1)[-1])))

    def action_accept_completion(self) -> None:
        """Accept the selected app-level completion.

        接受选定的应用级补全项。
        """
        self._completion_target().action_accept_completion()

    def action_completion_next(self) -> None:
        """Select the next app-level completion or move down in the prompt.

        选择下一个应用级补全项，或在提示词中向下移动。
        """
        if self._has_completion_options():
            self._completion_target().action_completion_next()
        else:
            self.action_cursor_down()

    def action_completion_previous(self) -> None:
        """Select the previous app-level completion or move up in the prompt.

        选择上一个应用级补全项，或在提示词中向上移动。
        """
        if self._has_completion_options():
            self._completion_target().action_completion_previous()
        elif self._completion_target().action_edit_queued_message():
            return
        else:
            self.action_cursor_up()

    def action_cancel(self) -> None:
        """Run the app-level cancel action.

        执行应用级取消操作。
        """
        self._completion_target().action_cancel()

    def action_open_command_palette(self) -> None:
        """Open the app-level command palette.

        打开应用级命令面板。
        """
        self._completion_target().action_open_command_palette()

    def action_open_session_picker(self) -> None:
        """Open the app-level session picker.

        打开应用级会话选择器。
        """
        self._completion_target().action_open_session_picker()

    def action_cycle_thinking(self) -> None:
        """Cycle the app-level thinking mode.

        循环切换应用级思考模式。
        """
        self._completion_target().action_cycle_thinking()

    def action_cycle_model(self) -> None:
        """Cycle the app-level scoped model forward.

        向前循环切换应用级限定模型。
        """
        self._completion_target().action_cycle_model()

    def action_cycle_model_reverse(self) -> None:
        """Cycle the app-level scoped model backward.

        向后循环切换应用级限定模型。
        """
        self._completion_target().action_cycle_model_reverse()

    def action_toggle_tool_results(self) -> None:
        """Toggle app-level tool result display.

        切换应用级工具结果显示。
        """
        self._completion_target().action_toggle_tool_results()

    def action_toggle_thinking(self) -> None:
        """Toggle app-level thinking-token display.

        切换应用级思考令牌显示。
        """
        self._completion_target().action_toggle_thinking()

    def action_clear_prompt(self) -> None:
        """Clear the current prompt.

        清空当前提示词。
        """
        if self.selected_text:
            return
        if self.text:
            self.text = ""
            self.move_cursor((0, 0))
            self._clear_pending_paste()

    def render_line(self, y: int) -> Strip:
        """Render safely while a narrow terminal leaves no content width.

        当狭窄终端没有留下内容宽度时进行安全渲染。

        Textual's placeholder wrapping currently raises when the content width
        is zero. This can happen briefly while a narrow terminal pane is
        switching from the sidebar layout to compact mode.

        Textual 的占位符换行目前会在内容宽度为零时抛出异常；狭窄终端窗格从
        侧边栏布局切换到紧凑模式时可能短暂出现这种情况。
        """
        if self.content_size.width <= 0:
            return Strip.blank(0, self.visual_style.rich_style)
        return super().render_line(y)

    def get_line(self, line_index: int) -> Text:
        """Retrieve one prompt line, coloring terminal commands like a running tool.

        获取一行提示词，并将终端命令着色为正在运行的工具。
        """
        line = super().get_line(line_index)
        if not self.shell_mode_style:
            return line
        span = _terminal_command_prefix_span(self.text)
        if span is None:
            return line
        start, _ = span
        line.stylize(self.shell_mode_style, start if line_index == 0 else 0)
        return line

    async def action_submit_follow_up(self) -> None:
        """Submit the prompt as an app-level follow-up.

        将提示词作为应用级后续消息提交。
        """
        await self._completion_target().action_submit_follow_up()

    async def action_submit_prompt(self) -> None:
        """Submit the prompt through the app-level action.

        通过应用级操作提交提示词。
        """
        await self._completion_target().action_submit_prompt()

    def action_insert_newline(self) -> None:
        """Insert a newline in the prompt.

        在提示词中插入换行符。
        """
        self.insert("\n")

    async def action_quit(self) -> None:
        """Quit the app through the app-level action.

        通过应用级操作退出应用。
        """
        await self.app.action_quit()

    def action_scroll_down(self) -> None:
        """Use down arrow for completion selection while focused.

        聚焦时使用向下箭头选择补全项。
        """
        self.action_completion_next()

    def action_scroll_up(self) -> None:
        """Use up arrow for completion selection while focused.

        聚焦时使用向上箭头选择补全项。
        """
        self.action_completion_previous()

    def on_paste(self, event: events.Paste) -> None:
        """Handle file drops and collapse very large pastes to a placeholder.

        处理文件拖放，并将超大粘贴内容折叠为占位符。

        Terminals deliver OS drag-and-drop as typed text, which Textual reports
        as a paste; when the pasted text is only existing file paths, insert the
        normalized paths instead of the raw (possibly escaped) drop text.

        终端会把操作系统拖放作为键入文本传递，Textual 将其报告为粘贴。如果
        粘贴文本只包含现有文件路径，则插入规范化路径，而不是原始且可能已转义的
        拖放文本。
        """
        if self.handle_pasted_text(event.text):
            event.stop()
            event.prevent_default()

    def handle_pasted_text(self, text: str) -> bool:
        """Apply Tau's paste rules to *text*.

        将 Tau 的粘贴规则应用到 *text*。

        Returns ``True`` when the text was inserted here (file drop or large
        paste placeholder) and ``False`` when it should be inserted verbatim by
        the caller (or by Textual's default paste handling).

        当文本已在这里插入时返回 ``True``，包括文件拖放或大段粘贴占位符；当
        调用方或 Textual 默认粘贴处理应按原样插入时返回 ``False``。
        """
        dropped_paths = normalize_dropped_paths(text)
        if dropped_paths is not None:
            self._insert_dropped_paths(dropped_paths)
            return True
        if len(text) <= PASTE_DISPLAY_THRESHOLD:
            return False
        self._show_large_paste_placeholder(text)
        return True

    def insert_pasted_text(self, text: str) -> None:
        """Insert pasted text that Textual could not deliver to this widget.

        插入 Textual 无法传递给此小组件的粘贴文本。

        Used for drops that arrive while the terminal is unfocused, where no
        default paste handler runs, so verbatim insertion is done here.

        用于终端未聚焦时到达的拖放；此时不会运行默认粘贴处理器，因此在这里
        按原样插入。
        """
        if not self.handle_pasted_text(text):
            self.insert(text)

    def _insert_dropped_paths(self, insertion: str) -> None:
        """Insert dropped paths at the cursor, separated from surrounding text.

        在光标处插入拖放路径，并与周围文本分隔。
        """
        position = self.cursor_position
        before = self.text[:position]
        after = self.text[position:]
        if before and not before[-1].isspace():
            insertion = f" {insertion}"
        if not after or not after[0].isspace():
            insertion = f"{insertion} "
        self.insert(insertion)

    def _show_large_paste_placeholder(self, content: str) -> None:
        """Store large pasted text and render a compact placeholder.

        存储大段粘贴文本并渲染紧凑占位符。
        """
        self._paste_placeholder_counter += 1
        placeholder = self._large_paste_placeholder(content, self._paste_placeholder_counter)
        self._pending_pastes.append((placeholder, content))
        self.insert(placeholder)

    def _large_paste_placeholder(self, content: str, paste_number: int) -> str:
        """Build the display text for a large paste.

        构建大段粘贴内容的显示文本。
        """
        char_count = len(content)
        line_count = content.count("\n") + 1
        kb = char_count / 1024
        parts: list[str] = [f"{char_count:,} characters"]
        if line_count > 1:
            parts.append(f"{line_count} lines")
        if kb >= 1:
            parts.append(f"{kb:.1f} KB")
        return f"[Pasted content #{paste_number}: {', '.join(parts)}]"

    def _clear_pending_paste(self) -> None:
        """Forget any stored large paste content.

        清除已存储的大段粘贴内容。
        """
        self._pending_pastes.clear()

    def sync_pending_paste(self) -> None:
        """Invalidate stored paste content when its placeholder is edited away.

        当占位符被编辑掉时使已存储的粘贴内容失效。
        """
        self._pending_pastes = [
            (placeholder, content)
            for placeholder, content in self._pending_pastes
            if placeholder in self.text
        ]

    def text_for_submission(self) -> str:
        """Return the prompt text, expanding intact large-paste placeholders.

        返回提示词文本，并展开仍然完整的大段粘贴占位符。
        """
        self.sync_pending_paste()
        text = self.text
        for placeholder, content in self._pending_pastes:
            text = text.replace(placeholder, content, 1)
        return text

    async def on_key(self, event: Key) -> None:
        """Route completion and submission keys before default input handling.

        在默认输入处理前路由补全和提交按键。

        Extension key interceptors are consulted upstream in
        :meth:`TauTuiApp.on_event` (pre-dispatch, before app-level priority
        bindings), so there is no interceptor splice here.

        扩展按键拦截器会在上游的 :meth:`TauTuiApp.on_event` 中调用，位于分派前
        且早于应用级优先绑定，因此这里没有拦截器接入点。
        """
        keybindings = self.tui_keybindings
        if event.key == keybindings.queue_follow_up:
            event.stop()
            event.prevent_default()
            await self._completion_target().action_submit_follow_up()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            await self._completion_target().action_submit_prompt()
        elif event.key == keybindings.insert_newline:
            event.stop()
            event.prevent_default()
            self.insert("\n")
        elif event.key == keybindings.accept_completion:
            event.stop()
            self._completion_target().action_accept_completion()
        elif event.key == keybindings.cancel:
            event.stop()
            self._completion_target().action_cancel()
        elif event.key == keybindings.command_palette:
            event.stop()
            self._completion_target().action_open_command_palette()
        elif event.key == keybindings.session_picker:
            event.stop()
            self._completion_target().action_open_session_picker()
        elif _is_thinking_cycle_key(event.key, keybindings.thinking_cycle):
            event.stop()
            self._completion_target().action_cycle_thinking()
        elif event.key == keybindings.model_cycle:
            event.stop()
            self._completion_target().action_cycle_model()
        elif event.key == keybindings.model_cycle_reverse:
            event.stop()
            self._completion_target().action_cycle_model_reverse()
        elif event.key == keybindings.toggle_tool_results:
            event.stop()
            self._completion_target().action_toggle_tool_results()
        elif event.key == keybindings.toggle_thinking:
            event.stop()
            self._completion_target().action_toggle_thinking()
        elif event.key == keybindings.copy_message:
            if self.selected_text:
                return
            event.stop()
            event.prevent_default()
            if self.text:
                self.text = ""
                self.move_cursor((0, 0))
        elif event.key == keybindings.completion_next:
            event.stop()
            if self._has_completion_options():
                self._completion_target().action_completion_next()
            else:
                self.action_cursor_down()
        elif event.key == keybindings.completion_previous:
            event.stop()
            self.action_completion_previous()
        elif event.key == keybindings.quit:
            event.stop()
            await self.action_quit()

    def _has_completion_options(self) -> bool:
        """Return whether the app currently exposes completion candidates.

        返回应用当前是否公开补全候选项。
        """
        completion_state = getattr(self.app, "_completion_state", None)
        return bool(getattr(completion_state, "items", ()))

    def _completion_target(self) -> CompletionActionTarget:
        """Return the app-level object that owns completion actions.

        返回拥有补全操作的应用级对象。
        """
        return cast(CompletionActionTarget, self.app)


class ExtensionSelectScreen(ModalScreen[str | None]):
    """Modal option picker backing `context.ui.select`.

    Binding/key wiring mirrors `SessionPickerScreen`. Note: the app binds
    Up/Down globally with priority (completion navigation), so this screen
    must also be listed in the `action_completion_next/previous` and
    `action_accept_completion` screen allowlists for arrow keys to reach
    the option list.
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    # 初始化带主题的扩展选项选择器，并保存标题和候选项。
    def __init__(
        self,
        title: str,
        options: Sequence[str],
        *,
        theme: TuiTheme,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.options = tuple(options)
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the option picker.

        组合选项选择器。
        """
        with Vertical(id="extension-select"):
            yield Static(self.title_text, id="extension-select-title", markup=False)
            yield ListView(
                *[ListItem(Label(option, markup=False)) for option in self.options],
                id="extension-select-list",
            )
            yield Static("Enter selects - Escape cancels", id="extension-select-help")

    def on_mount(self) -> None:
        """Focus the option list for keyboard navigation.

        聚焦选项列表以供键盘导航。
        """
        option_list = self.query_one("#extension-select-list", ListView)
        option_list.index = 0
        option_list.focus()

    def on_key(self, event: Key) -> None:
        """Route arrow and enter keys to the option list.

        将方向键和回车键路由到选项列表。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the chosen option.

        使用选中的选项关闭界面。
        """
        self.dismiss(self.options[event.index])

    def action_cursor_up(self) -> None:
        """Move to the previous option.

        移动到上一个选项。
        """
        self.query_one("#extension-select-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move to the next option.

        移动到下一个选项。
        """
        self.query_one("#extension-select-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Select the highlighted option.

        选择高亮选项。
        """
        self.query_one("#extension-select-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:
        """Close without choosing an option.

        不选择任何选项并关闭。
        """
        self.dismiss(None)


class ExtensionConfirmScreen(ModalScreen[bool]):
    """Modal yes/no confirmation backing `context.ui.confirm`.

    Binding/key wiring mirrors `SessionPickerScreen`; see
    `ExtensionSelectScreen` for the app-level Up/Down allowlist requirement.
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    def __init__(self, title: str, message: str, *, theme: TuiTheme) -> None:
        """Initialize a themed yes-or-no confirmation dialog.

        初始化带主题的是非确认对话框。
        """
        super().__init__()
        self.title_text = title
        self.message = message
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the confirmation dialog.

        组合确认对话框。
        """
        with Vertical(id="extension-confirm"):
            yield Static(self.title_text, id="extension-confirm-title", markup=False)
            yield Static(self.message, id="extension-confirm-message", markup=False)
            yield ListView(
                ListItem(Label("Yes", markup=False)),
                ListItem(Label("No", markup=False)),
                id="extension-confirm-list",
            )
            yield Static("Enter selects - Escape cancels", id="extension-confirm-help")

    def on_mount(self) -> None:
        """Focus the choice list.

        聚焦选项列表。
        """
        choice_list = self.query_one("#extension-confirm-list", ListView)
        choice_list.index = 0
        choice_list.focus()

    def on_key(self, event: Key) -> None:
        """Route arrow and enter keys to the choice list.

        将方向键和回车键路由到选项列表。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the confirmation result (Yes is index 0).

        使用确认结果关闭界面，其中“是”的索引为 0。
        """
        self.dismiss(event.index == 0)

    def action_cursor_up(self) -> None:
        """Move to the previous choice.

        移动到上一个选项。
        """
        self.query_one("#extension-confirm-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move to the next choice.

        移动到下一个选项。
        """
        self.query_one("#extension-confirm-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Select the highlighted choice.

        选择高亮选项。
        """
        self.query_one("#extension-confirm-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:
        """Close, declining the confirmation.

        关闭界面并拒绝确认。
        """
        self.dismiss(False)


class ExtensionInputScreen(ModalScreen[str | None]):
    """Modal single-line text prompt backing `context.ui.input`.

    为 `context.ui.input` 提供支持的模态单行文本提示框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        *,
        theme: TuiTheme,
        value: str = "",
    ) -> None:
        """Initialize a themed single-line text prompt.

        初始化带主题的单行文本提示框。
        """
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.theme = theme
        self.value = value

    def compose(self) -> ComposeResult:
        """Compose the text prompt.

        组合文本提示框。
        """
        with Vertical(id="extension-input"):
            yield Static(self.title_text, id="extension-input-title", markup=False)
            yield Input(
                value=self.value,
                placeholder=self.placeholder,
                id="extension-input-field",
            )
            yield Static("Enter submits - Escape cancels", id="extension-input-help")

    def on_mount(self) -> None:
        """Focus the text field.

        聚焦文本字段。
        """
        self.query_one("#extension-input-field", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit the text field when the user presses Enter.

        用户按下回车时提交文本字段。
        """
        """Dismiss with the entered text.

        使用输入文本关闭界面。
        """
        if event.input.id != "extension-input-field":
            return
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        """Close without submitting text.

        不提交文本并关闭。
        """
        self.dismiss(None)


class ToolsReferenceSearchInput(Input):
    """Search input that keeps tool-reference navigation local.

    将工具参考导航限制在本地的搜索输入框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "open_selected", "Open", show=False, priority=True),
    ]

    def _reference(self) -> ToolsReferenceScreen:
        """Return the owning tool-reference screen.

        返回所属的工具参考屏幕。
        """
        return cast(ToolsReferenceScreen, self.screen)

    def on_key(self, event: Key) -> None:
        """Route navigation without changing the search text.

        在不更改搜索文本的情况下路由导航操作。
        """
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self._reference().action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self._reference().action_cursor_down()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self._reference().action_cancel()
        elif event.key == "enter":
            event.stop()
            event.prevent_default()
            self._reference().action_open_selected()

    def action_open_selected(self) -> None:
        """Open the tool currently highlighted in the owning reference screen.

        打开所属工具参考屏幕中当前高亮的工具。
        """
        self._reference().action_open_selected()


class ToolsReferenceScreen(ModalScreen[None]):
    """Searchable tool table with navigable description details.

    带可导航说明详情的可搜索工具表格。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Close"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "open_selected", "Open", show=False),
    ]

    def __init__(
        self,
        tools: Sequence[AgentTool],
        *,
        extension_sources: Mapping[str, str],
        theme: TuiTheme,
    ) -> None:
        """Initialize the searchable tool reference from visible agent tools.

        根据可见代理工具初始化可搜索工具参考界面。
        """
        super().__init__()
        self.extension_sources = dict(extension_sources)
        self.tools = self._order_tools(tools)
        self.visible_tools = self.tools
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the tool reference.

        组合工具参考界面。
        """
        with Vertical(id="tools-reference"):
            yield Static("Available tools", id="tools-reference-title")
            yield ToolsReferenceSearchInput(placeholder="Search tools", id="tools-reference-search")
            yield Static(
                self._table_row("Tool", "Origin", "Description"),
                id="tools-reference-header",
            )
            yield ListView(id="tools-reference-list")
            yield Static("Enter opens description - Escape closes", id="tools-reference-help")

    def on_mount(self) -> None:
        """Populate the list and focus search on open.

        打开时填充列表并聚焦搜索框。
        """
        self._refresh_tools("")
        self.query_one("#tools-reference-search", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Refresh visible tools when the search query changes.

        搜索查询变化时刷新可见工具。
        """
        if event.input.id == "tools-reference-search":
            event.stop()
            self._refresh_tools(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Open the tool represented by the selected list row.

        打开选中列表行所表示的工具。
        """
        """Open the selected tool's full description.

        打开选定工具的完整说明。
        """
        event.stop()
        self._open_tool(event.index)

    def action_cursor_up(self) -> None:
        """Move tool selection to the previous row.

        将工具选择移动到上一行。
        """
        self.query_one("#tools-reference-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move tool selection to the next row.

        将工具选择移动到下一行。
        """
        self.query_one("#tools-reference-list", ListView).action_cursor_down()

    def action_open_selected(self) -> None:
        """Open details for the highlighted tool.

        打开高亮工具的详情。
        """
        tool_list = self.query_one("#tools-reference-list", ListView)
        if tool_list.index is not None:
            self._open_tool(tool_list.index)

    def action_cancel(self) -> None:
        """Close the tool-reference screen.

        关闭工具参考屏幕。
        """
        self.dismiss(None)

    def _open_tool(self, index: int) -> None:
        """Render the full description for a visible tool index.

        渲染可见工具索引对应的完整说明。
        """
        if index >= len(self.visible_tools):
            return
        tool = self.visible_tools[index]
        self.app.push_screen(
            CommandOutputScreen(
                f"{tool.name} — {self._source_label(tool)}",
                tool.description or "No description",
                theme=self.theme,
            )
        )

    def _refresh_tools(self, query: str) -> None:
        """Filter and rebuild tool rows for a search query.

        根据搜索查询筛选并重建工具行。
        """
        needle = query.casefold().strip()
        self.visible_tools = tuple(
            tool
            for tool in self.tools
            if not needle
            or needle in tool.name.casefold()
            or needle in tool.label.casefold()
            or needle in tool.description.casefold()
            or needle in self._source_label(tool).casefold()
        )
        tool_list = self.query_one("#tools-reference-list", ListView)
        tool_list.clear()
        if not self.visible_tools:
            message = "No tools available." if not self.tools else "No tools match your search."
            tool_list.append(ListItem(Label(message, markup=False), disabled=True))
            return
        tool_list.extend(
            [
                ListItem(
                    Label(
                        self._table_row(
                            tool.name,
                            self._source_label(tool),
                            f"{len(tool.description)} chars",
                        ),
                        markup=False,
                    )
                )
                for tool in self.visible_tools
            ]
        )
        tool_list.index = 0

    def _order_tools(self, tools: Sequence[AgentTool]) -> tuple[AgentTool, ...]:
        """Order tools consistently by source and display name.

        按来源和显示名称稳定排序工具。
        """
        tools_by_name = {tool.name: tool for tool in tools}
        builtins = sorted(
            (tool for tool in tools if tool.name not in self.extension_sources),
            key=lambda tool: tool.name.casefold(),
        )
        extension_tools: list[AgentTool] = []
        seen_extensions: set[str] = set()
        for extension in self.extension_sources.values():
            if extension in seen_extensions:
                continue
            seen_extensions.add(extension)
            extension_tools.extend(
                tools_by_name[tool_name]
                for tool_name, source in self.extension_sources.items()
                if source == extension and tool_name in tools_by_name
            )
        return tuple([*builtins, *extension_tools])

    def _table_row(self, name: str, source: str, description: str) -> str:
        """Format one compact row for the tool table.

        为工具表格格式化一条紧凑行。
        """
        name_width = max((len(tool.name) for tool in self.tools), default=len("Tool"))
        source_width = max(
            (len(self._source_label(tool)) for tool in self.tools),
            default=len("Origin"),
        )
        return (
            f"{name:<{max(name_width, len('Tool'))}}  "
            f"{source:<{max(source_width, len('Origin'))}}  {description}"
        )

    def _source_label(self, tool: AgentTool) -> str:
        """Return the user-facing source label for a tool.

        返回工具面向用户的来源标签。
        """
        extension = self.extension_sources.get(tool.name)
        return extension if extension is not None else "Built in"


class SessionPickerSearchInput(Input):
    """Search input that keeps session-picker navigation local to the picker.

    将会话选择器导航限制在选择器内部的搜索输入框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    # 返回拥有此搜索输入框的会话选择器屏幕。
    def _picker(self) -> SessionPickerScreen:
        return cast(SessionPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:
        """Route picker control keys before the input edits its text.

        在输入框编辑文本前路由选择器控制键。
        """
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key in {"left", "right"} and isinstance(self.screen, SessionPickerScreen):
            event.stop()
            event.prevent_default()
            if event.key == "left":
                self.screen.action_focus_projects()
            else:
                self.screen.action_focus_sessions()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def action_cursor_up(self) -> None:
        """Move the session picker selection up.

        向上移动会话选择器的选中项。
        """
        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move the session picker selection down.

        向下移动会话选择器的选中项。
        """
        self._picker().action_cursor_down()

    def action_cancel(self) -> None:
        """Close the session picker.

        关闭会话选择器。
        """
        self._picker().action_cancel()


@dataclass(frozen=True, slots=True)
class PromptTemplatePickerResult:
    """Action selected from the prompt-template picker.

    从提示词模板选择器选中的操作。
    """

    action: Literal["insert", "edit"]
    template: PromptTemplate


class PromptTemplatePickerScreen(ModalScreen[PromptTemplatePickerResult | None]):
    """Searchable picker for loaded prompt templates.

    用于已加载提示词模板的可搜索选择器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("ctrl+e", "edit_cursor", "Edit", show=False, priority=True),
    ]

    # 初始化按名称排序的提示词模板集合及当前可见集合。
    def __init__(self, templates: Sequence[PromptTemplate]) -> None:
        super().__init__()
        self.templates = tuple(sorted(templates, key=lambda item: item.name.lower()))
        self.visible_templates = self.templates

    # 组合模板搜索框、结果列表和操作提示。
    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-template-picker"):
            yield Static("Prompt templates", id="prompt-template-picker-title")
            yield SessionPickerSearchInput(
                placeholder="Search prompt templates", id="prompt-template-picker-search"
            )
            yield ListView(id="prompt-template-picker-list")
            yield Static("", id="prompt-template-picker-help")

    # 聚焦搜索框并首次填充模板列表。
    def on_mount(self) -> None:
        self.query_one("#prompt-template-picker-search", Input).focus()
        self._refresh_list()

    # 根据搜索输入过滤模板名称和描述。
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt-template-picker-search":
            return
        event.stop()
        query = event.value.casefold()
        self.visible_templates = tuple(
            template
            for template in self.templates
            if query in template.name.casefold() or query in (template.description or "").casefold()
        )
        self._refresh_list()

    # 在搜索框提交时插入当前高亮模板。
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt-template-picker-search":
            event.stop()
            self.action_select_cursor()

    # 将列表选择事件转交给当前模板插入操作。
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self.action_select_cursor()

    # 将模板选择移动到上一项。
    def action_cursor_up(self) -> None:
        self.query_one("#prompt-template-picker-list", ListView).action_cursor_up()

    # 将模板选择移动到下一项。
    def action_cursor_down(self) -> None:
        self.query_one("#prompt-template-picker-list", ListView).action_cursor_down()

    # 以插入操作关闭选择器并返回当前模板。
    def action_select_cursor(self) -> None:
        template = self._selected_template()
        if template is not None:
            self.dismiss(PromptTemplatePickerResult(action="insert", template=template))

    def action_edit_cursor(self) -> None:
        """Open the selected template in Tau's prompt editor.

        在 Tau 的提示词编辑器中打开选定模板。
        """
        template = self._selected_template()
        if template is not None:
            self.dismiss(PromptTemplatePickerResult(action="edit", template=template))

    # 关闭模板选择器且不返回任何操作。
    def action_cancel(self) -> None:
        self.dismiss(None)

    # 返回当前高亮的可见模板；没有有效选择时返回 None。
    def _selected_template(self) -> PromptTemplate | None:
        picker_list = self.query_one("#prompt-template-picker-list", ListView)
        index = picker_list.index
        if index is None or index >= len(self.visible_templates):
            return None
        return self.visible_templates[index]

    # 按当前过滤结果重建模板列表并更新帮助文本。
    def _refresh_list(self) -> None:
        picker_list = self.query_one("#prompt-template-picker-list", ListView)
        picker_list.clear()
        picker_list.extend(
            ListItem(
                Label(
                    f"/{template.name} — {template.description or 'No description'}",
                    markup=False,
                )
            )
            for template in self.visible_templates
        )
        picker_list.index = 0 if self.visible_templates else None
        if self.visible_templates:
            help_text = "Enter inserts - Ctrl+E edits - Escape closes"
        elif self.templates:
            help_text = "No matching prompt templates - Escape closes"
        else:
            help_text = "No prompt templates loaded - Escape closes"
        self.query_one("#prompt-template-picker-help", Static).update(help_text)


class PromptTemplateEditorScreen(ModalScreen[str | None]):
    """Edit one prompt-template Markdown file inside the TUI.

    在 TUI 中编辑一个提示词模板 Markdown 文件。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    # 初始化待编辑模板及其 Markdown 源文本。
    def __init__(self, template: PromptTemplate, source: str) -> None:
        super().__init__()
        self.template = template
        self.source = source

    # 组合模板标题、路径、文本编辑器和快捷键提示。
    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-template-editor"):
            yield Static(f"Edit /{self.template.name}", id="prompt-template-editor-title")
            yield Static(str(self.template.path), id="prompt-template-editor-path")
            yield TextArea(self.source, id="prompt-template-editor-input")
            yield Static(
                "Ctrl+S saves - Escape returns without saving",
                id="prompt-template-editor-help",
            )

    # 屏幕挂载后聚焦模板文本编辑器。
    def on_mount(self) -> None:
        self.query_one("#prompt-template-editor-input", TextArea).focus()

    # 返回编辑后的源文本并关闭编辑器。
    def action_save(self) -> None:
        source = self.query_one("#prompt-template-editor-input", TextArea).text
        self.dismiss(source)

    # 放弃编辑结果并关闭编辑器。
    def action_cancel(self) -> None:
        self.dismiss(None)


def _write_staged_utf8(handle: BinaryIO, source: str) -> None:
    """Write complete UTF-8 editor contents to an open staging file.

    将完整的 UTF-8 编辑器内容写入已打开的暂存文件。
    """
    remaining = memoryview(source.encode("utf-8"))
    while remaining:
        written = handle.write(remaining)
        if written is None or written <= 0:
            raise OSError("staged write did not make progress")
        remaining = remaining[written:]


@dataclass(frozen=True, slots=True)
class _SidebarFileSnapshot:
    """Resolved target and exact bytes observed when a sidebar file was loaded.

    加载侧边栏文件时观察到的已解析目标和精确字节。
    """

    target: Path
    content: bytes


def _read_sidebar_file(path: Path) -> tuple[str, _SidebarFileSnapshot]:
    """Read a sidebar file without normalizing its encoded contents.

    读取侧边栏文件，同时不规范化其编码内容。
    """
    target = path.resolve(strict=True)
    content = target.read_bytes()
    return content.decode("utf-8"), _SidebarFileSnapshot(target=target, content=content)


def _atomic_write_sidebar_file(
    path: Path,
    source: str,
    expected: _SidebarFileSnapshot,
) -> _SidebarFileSnapshot:
    """Atomically replace an unchanged, writable file and preserve its mode/symlink.

    原子替换未变化且可写的文件，并保留其模式和符号链接。
    """
    target = path.resolve(strict=True)
    replacement = source.encode("utf-8")
    if target != expected.target:
        raise OSError(f"File target changed on disk; reopen before saving: {path}")
    # Ask the OS to enforce ownership/ACL rules without truncating the target.
    #
    # 请求操作系统执行所有权和访问控制规则，同时不截断目标文件。
    authorization = os.open(
        target,
        os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        target_stat = os.fstat(authorization)
        # Privileged processes may open 0444 files, but the editor treats an
        # explicitly read-only resource as not authorized for replacement.
        #
        # 特权进程可能可以打开 0444 文件，但编辑器将显式只读资源视为无权替换。
        if target_stat.st_mode & 0o222 == 0:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), target)
        target_mode = stat.S_IMODE(target_stat.st_mode)
    finally:
        os.close(authorization)

    descriptor = -1
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(raw_temporary)
        os.chmod(temporary, target_mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            _write_staged_utf8(handle, source)
            handle.flush()
            os.fsync(handle.fileno())
        current_target = path.resolve(strict=True)
        if current_target != expected.target or current_target.read_bytes() != expected.content:
            raise OSError(f"File changed on disk; reopen before saving: {path}")
        os.replace(temporary, target)
        temporary = None
        return _SidebarFileSnapshot(target=target, content=replacement)
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()


class SidebarFileEditor(Vertical):
    """Main-area editor for a file selected from the session sidebar.

    用于编辑会话侧边栏所选文件的主区域编辑器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "close", "Close", show=False, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
    ]

    # 初始化侧栏文件编辑器及用于并发写入校验的文件快照。
    def __init__(
        self,
        *,
        handle: MainViewHandle,
        path: Path,
        label: str,
        kind: str,
        source: str,
        snapshot: _SidebarFileSnapshot,
    ) -> None:
        super().__init__(id="sidebar-file-editor")
        self.handle = handle
        self.path = path
        self.label = label
        self.kind = kind
        self.source = source
        self._saved_source = source
        self._snapshot = snapshot
        self._saving = False

    # 组合文件标题、路径、正文编辑器、帮助信息和保存状态。
    def compose(self) -> ComposeResult:
        yield Static(f"Edit {self.kind}: {self.label}", id="sidebar-file-editor-title")
        yield Static(str(self.path), id="sidebar-file-editor-path")
        yield TextArea(self.source, id="sidebar-file-editor-input")
        yield Static(
            "Ctrl+S saves - Escape closes",
            id="sidebar-file-editor-help",
        )
        yield Static("", id="sidebar-file-editor-status")

    # 组件挂载后聚焦文件正文编辑器。
    def on_mount(self) -> None:
        self.query_one("#sidebar-file-editor-input", TextArea).focus()

    @property
    def is_dirty(self) -> bool:
        """Return whether the mounted editor differs from its last saved source.

        返回已挂载编辑器是否不同于最近保存的源内容。
        """
        try:
            source = self.query_one("#sidebar-file-editor-input", TextArea).text
        except NoMatches:
            return False
        return source != self._saved_source

    def action_save(self) -> None:
        """Write the current editor contents without closing the editor.

        写入当前编辑器内容但不关闭编辑器。
        """
        if self._saving:
            return
        self._saving = True
        self.query_one("#sidebar-file-editor-status", Static).update("Saving…")
        self.app.run_worker(self._save(), exclusive=False)

    # 在后台线程原子保存文件，并同步快照、脏状态和用户通知。
    async def _save(self) -> None:
        source = self.query_one("#sidebar-file-editor-input", TextArea).text
        try:
            snapshot = await asyncio.to_thread(
                _atomic_write_sidebar_file,
                self.path,
                source,
                self._snapshot,
            )
        except Exception as exc:  # noqa: BLE001 - filesystem worker boundary

            # BLE001：此处是文件系统工作器的异常隔离边界。
            message = f"Could not save {self.path}: {exc}"
            self.query_one("#sidebar-file-editor-status", Static).update(message)
            cast(TauTuiApp, self.app)._notify(message, severity="error")
        else:
            self._snapshot = snapshot
            self._saved_source = source
            message = f"Saved {self.path}"
            self.query_one("#sidebar-file-editor-status", Static).update(message)
            cast(TauTuiApp, self.app)._notify(message)
        finally:
            self._saving = False

    def action_close(self) -> None:
        """Close the editor and restore the transcript.

        关闭编辑器并恢复会话记录视图。
        """
        self.handle.close()


class SessionPickerScreen(ModalScreen[str | None]):
    """Project-and-session navigator for indexed sessions.

    用于已索引会话的项目和会话导航器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("left", "focus_projects", "Projects", show=False),
        Binding("right", "focus_sessions", "Sessions", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    CSS = """
    #session-picker {
        width: 110;
        max-width: 94%;
        height: auto;
        max-height: 85%;
    }

    #session-picker-columns {
        height: auto;
        border: tall $tau-border;
        background: $tau-transcript-background;
    }

    .session-picker-column {
        height: auto;
        background: $tau-transcript-background;
    }

    #session-picker-project-column {
        width: 34;
        border-right: tall $tau-border;
    }

    #session-picker-session-column {
        width: 1fr;
    }

    .session-picker-column-title {
        height: 1;
        padding: 0 1;
        color: $tau-muted-text;
        text-style: bold;
    }

    .-active-column > .session-picker-column-title {
        color: $tau-accent;
    }

    #session-picker-project-list,
    #session-picker-list {
        height: auto;
        max-height: 16;
        border: none;
        background: $tau-transcript-background;
    }

    #session-picker-list {
        padding: 0 1;
    }
    """

    # 初始化会话记录、项目分组、搜索条件及当前活动列。
    def __init__(
        self,
        records: Sequence[SessionCompletionRecord],
        *,
        local_cwd: Path,
        theme: TuiTheme,
        loading_other_projects: bool = False,
        current_project_loaded: bool = True,
    ) -> None:
        super().__init__()
        self.records = tuple(records)
        self.local_cwd = local_cwd.resolve()
        self.theme = theme
        self.search_value = ""
        self.active_column: Literal["projects", "sessions"] = "sessions"
        self.records_by_project = self._group_records_by_project()
        self.project_cwds = tuple(self.records_by_project)
        self.selected_project_index = 0
        self.visible_records: tuple[SessionCompletionRecord, ...] = ()
        self.loading_other_projects = loading_other_projects
        self.current_project_loaded = current_project_loaded

    def compose(self) -> ComposeResult:
        """Compose project and session columns under one search field.

        在一个搜索字段下组合项目列和会话列。
        """
        with Vertical(id="session-picker"):
            yield Static("Sessions", id="session-picker-title")
            yield SessionPickerSearchInput(
                placeholder="Search sessions in selected project",
                id="session-picker-search",
            )
            with Horizontal(id="session-picker-columns"):
                with Vertical(
                    id="session-picker-project-column",
                    classes="session-picker-column",
                ):
                    yield Static("Projects", classes="session-picker-column-title")
                    yield OptionList(id="session-picker-project-list", markup=False, compact=True)
                with Vertical(
                    id="session-picker-session-column",
                    classes="session-picker-column -active-column",
                ):
                    yield Static(
                        "",
                        id="session-picker-session-title",
                        classes="session-picker-column-title",
                    )
                    yield OptionList(id="session-picker-list", markup=False, compact=True)
            yield Static("", id="session-picker-help")

    def on_mount(self) -> None:
        """Start in the current project's recent-session column.

        从当前项目的最近会话列开始。
        """
        self.query_one("#session-picker-search", Input).focus()
        self._refresh_project_list()
        self._refresh_session_list()
        self._update_help()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter sessions in the selected project.

        筛选选定项目中的会话。
        """
        if event.input.id != "session-picker-search":
            return
        event.stop()
        self.search_value = event.value
        self._refresh_session_list()

    # 在搜索框提交时恢复当前高亮的可见会话。
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "session-picker-search":
            return
        event.stop()
        self.action_select_cursor()

    def on_key(self, event: Key) -> None:
        """Route navigation while keeping typing focus in the search field.

        路由导航操作，同时让输入焦点保持在搜索字段中。
        """
        actions = {
            "up": self.action_cursor_up,
            "down": self.action_cursor_down,
            "left": self.action_focus_projects,
            "right": self.action_focus_sessions,
            "enter": self.action_select_cursor,
        }
        action = actions.get(event.key)
        if action is not None:
            event.stop()
            action()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Show sessions for the highlighted project immediately.

        立即显示高亮项目的会话。
        """
        if event.option_list.id != "session-picker-project-list":
            return
        index = event.option_index
        if index == self.selected_project_index:
            return
        self.selected_project_index = index
        self._refresh_session_list()

    # 处理项目或会话列表的选中事件。
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == "session-picker-project-list":
            self.selected_project_index = event.option_index
            self._refresh_session_list()
            self.action_focus_sessions()
            return
        self._select_visible_record()

    # 在当前活动列中向上移动选择。
    def action_cursor_up(self) -> None:
        self._active_list().action_cursor_up()

    # 在当前活动列中向下移动选择。
    def action_cursor_down(self) -> None:
        self._active_list().action_cursor_down()

    # 将键盘导航焦点切换到项目列。
    def action_focus_projects(self) -> None:
        self._set_active_column("projects")

    # 将键盘导航焦点切换到会话列。
    def action_focus_sessions(self) -> None:
        self._set_active_column("sessions")

    # 确认当前活动列的高亮项目或会话。
    def action_select_cursor(self) -> None:
        if self.active_column == "projects":
            self.action_focus_sessions()
        else:
            self._select_visible_record()

    # 关闭会话选择器且不恢复任何会话。
    def action_cancel(self) -> None:
        self.dismiss(None)

    def update_records(
        self,
        records: Sequence[SessionCompletionRecord],
        *,
        loading_other_projects: bool = False,
    ) -> None:
        """Replace records after background loading while preserving navigation.

        后台加载完成后替换记录，同时保留导航状态。
        """
        selected_cwd = self.project_cwds[self.selected_project_index]
        session_list = self.query_one("#session-picker-list", OptionList)
        selected_session_id = None
        if session_list.highlighted is not None and session_list.highlighted < len(
            self.visible_records
        ):
            selected_session_id = self.visible_records[session_list.highlighted].id

        self.records = tuple(records)
        self.records_by_project = self._group_records_by_project()
        self.project_cwds = tuple(self.records_by_project)
        try:
            self.selected_project_index = self.project_cwds.index(selected_cwd)
        except ValueError:
            self.selected_project_index = 0
        self.loading_other_projects = loading_other_projects
        self.current_project_loaded = True
        self._refresh_project_list()
        self._refresh_session_list()

        if selected_session_id is not None:
            for index, record in enumerate(self.visible_records):
                if record.id == selected_session_id:
                    session_list.highlighted = index
                    break

    def finish_loading(self) -> None:
        """Remove the loading state when a background refresh fails.

        后台刷新失败时移除加载状态。
        """
        self.loading_other_projects = False
        self._update_help()

    # 返回当前接受键盘导航的项目或会话列表。
    def _active_list(self) -> OptionList:
        selector = (
            "#session-picker-project-list"
            if self.active_column == "projects"
            else "#session-picker-list"
        )
        return self.query_one(selector, OptionList)

    # 切换活动列并同步两列的视觉焦点状态。
    def _set_active_column(self, column: Literal["projects", "sessions"]) -> None:
        self.active_column = column
        projects = self.query_one("#session-picker-project-column", Vertical)
        sessions = self.query_one("#session-picker-session-column", Vertical)
        projects.set_class(column == "projects", "-active-column")
        sessions.set_class(column == "sessions", "-active-column")
        self._update_help()

    # 使用当前高亮索引关闭选择器并返回会话 ID。
    def _select_visible_record(self) -> None:
        index = self.query_one("#session-picker-list", OptionList).highlighted
        if index is not None and index < len(self.visible_records):
            self.dismiss(self.visible_records[index].id)

    def _group_records_by_project(
        self,
    ) -> dict[Path, tuple[SessionCompletionRecord, ...]]:
        """Group records once so picker refreshes stay linear in history size.

        仅对记录分组一次，使选择器刷新耗时与历史规模保持线性关系。
        """
        grouped: dict[Path, list[SessionCompletionRecord]] = {self.local_cwd: []}
        for record in self.records:
            grouped.setdefault(Path(record.cwd).resolve(), []).append(record)
        return {cwd: tuple(records) for cwd, records in grouped.items()}

    # 重建项目列表，并尽可能保持现有项目选择。
    def _refresh_project_list(self) -> None:
        project_list = self.query_one("#session-picker-project-list", OptionList)
        items: list[str] = []
        for cwd in self.project_cwds:
            marker = "● " if cwd == self.local_cwd else "  "
            folder_name = cwd.name or str(cwd)
            items.append(f"{marker}{folder_name}")
        project_list.set_options(items)
        project_list.highlighted = self.selected_project_index

    # 按当前项目和搜索条件重建可见会话列表。
    def _refresh_session_list(self) -> None:
        selected_cwd = self.project_cwds[self.selected_project_index]
        self.query_one("#session-picker-session-title", Static).update(
            f"Recent sessions — {selected_cwd}"
        )
        project_records = self.records_by_project[selected_cwd]
        self.visible_records = _filter_session_records(project_records, self.search_value)
        session_list = self.query_one("#session-picker-list", OptionList)
        session_list.set_options(_session_picker_label(record) for record in self.visible_records)
        session_list.highlighted = 0 if self.visible_records else None
        self._update_help()

    # 根据加载、过滤和活动列状态更新底部操作提示。
    def _update_help(self) -> None:
        if self.loading_other_projects and not self.current_project_loaded:
            text = "Loading sessions… - Escape closes"
        elif self.loading_other_projects:
            text = "Loading other projects… - Current sessions are ready - Escape closes"
        elif not self.visible_records and self.active_column == "sessions":
            text = "No matching sessions - Left selects a project - Escape closes"
        elif self.active_column == "projects":
            text = "Up/Down selects project - Right opens sessions - Escape closes"
        else:
            text = "Left selects project - Up/Down navigates - Enter resumes - Escape closes"
        self.query_one("#session-picker-help", Static).update(text)


class SkillPickerSearchInput(Input):
    """Search input that keeps skill-picker navigation local.

    将技能选择器导航限制在本地的搜索输入框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    # 返回拥有此搜索输入框的技能选择器。
    def _picker(self) -> SkillPickerScreen:
        return cast(SkillPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:
        """Route picker control keys before the input edits its text.

        在输入框编辑文本前路由选择器控制键。
        """
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()
        elif event.key == "f1":
            event.stop()
            event.prevent_default()
            self.action_show_description()
        elif event.key == "ctrl+enter":
            event.stop()
            event.prevent_default()
            self.action_show_in_transcript()

    # 将向上导航转发给技能选择器。
    def action_cursor_up(self) -> None:
        self._picker().action_cursor_up()

    # 将向下导航转发给技能选择器。
    def action_cursor_down(self) -> None:
        self._picker().action_cursor_down()

    # 关闭所属技能选择器。
    def action_cancel(self) -> None:
        self._picker().action_cancel()

    # 请求显示当前技能的完整说明。
    def action_show_description(self) -> None:
        self._picker().action_show_description()

    # 请求把当前技能内容显示到对话记录。
    def action_show_in_transcript(self) -> None:
        self._picker().action_show_in_transcript()


@dataclass(frozen=True, slots=True)
class SkillPickerResult:
    """A skill selection and the requested inspection action.

    技能选择结果及请求的检查操作。
    """

    skill: Skill
    action: Literal["insert", "transcript"]


class SkillPickerScreen(ModalScreen[SkillPickerResult | None]):
    """Searchable modal containing every loaded skill.

    包含全部已加载技能的可搜索模态界面。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_cursor", "Insert", show=False, priority=True),
        Binding("f1", "show_description", "Description", show=False, priority=True),
        Binding("ctrl+enter", "show_in_transcript", "Transcript", show=False, priority=True),
    ]

    # 初始化按名称排序的技能集合、主题和可见索引映射。
    def __init__(self, skills: Sequence[Skill], *, theme: TuiTheme) -> None:
        super().__init__()
        self.skills = tuple(sorted(skills, key=lambda skill: skill.name.casefold()))
        self.visible_skills = self.skills
        self.theme = theme

    # 组合技能搜索框、结果列表和操作提示。
    def compose(self) -> ComposeResult:
        with Vertical(id="skill-picker"):
            yield Static("Skills", id="skill-picker-title")
            yield SkillPickerSearchInput(placeholder="Search skills", id="skill-picker-search")
            yield ListView(id="skill-picker-list")
            yield Static("", id="skill-picker-help")

    # 聚焦搜索框并首次填充技能列表。
    def on_mount(self) -> None:
        self.query_one("#skill-picker-search", Input).focus()
        self._refresh_skill_list("")

    # 根据搜索文本刷新可见技能。
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "skill-picker-search":
            event.stop()
            self._refresh_skill_list(event.value)

    # 在搜索框提交时选择当前高亮技能。
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "skill-picker-search":
            event.stop()
            self._select_visible_skill()

    # 将列表选择事件转交给当前技能选择操作。
    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        self._select_visible_skill()

    # 将技能选择移动到上一行。
    def action_cursor_up(self) -> None:
        skill_list = self.query_one("#skill-picker-list", ListView)
        if skill_list.index is not None:
            skill_list.index = max(0, skill_list.index - 1)

    # 将技能选择移动到下一行。
    def action_cursor_down(self) -> None:
        skill_list = self.query_one("#skill-picker-list", ListView)
        if skill_list.index is not None:
            skill_list.index = min(len(self.visible_skills) - 1, skill_list.index + 1)

    # 以默认选择操作关闭选择器并返回技能。
    def action_select_cursor(self) -> None:
        skill = self._selected_skill()
        if skill is not None:
            self.dismiss(SkillPickerResult(skill, "insert"))

    # 打开当前技能的说明操作。
    def action_show_description(self) -> None:
        skill = self._selected_skill()
        if skill is not None:
            self.app.push_screen(
                CommandOutputScreen(
                    f"Skill description: {skill.name}",
                    skill.description or "No description",
                    theme=self.theme,
                )
            )

    # 请求将当前技能显示到对话记录。
    def action_show_in_transcript(self) -> None:
        skill = self._selected_skill()
        if skill is not None:
            self.dismiss(SkillPickerResult(skill, "transcript"))

    # 关闭技能选择器且不返回操作。
    def action_cancel(self) -> None:
        self.dismiss(None)

    # 返回当前高亮技能；没有有效选择时返回 None。
    def _selected_skill(self) -> Skill | None:
        index = self.query_one("#skill-picker-list", ListView).index
        if index is None or not self.visible_skills:
            return None
        return self.visible_skills[index]

    # 使用默认动作关闭选择器并返回当前技能。
    def _select_visible_skill(self) -> None:
        self.action_select_cursor()

    # 按名称和说明过滤技能，并重建带主题的列表行。
    def _refresh_skill_list(self, search: str) -> None:
        query = search.casefold().strip()
        self.visible_skills = tuple(
            skill
            for skill in self.skills
            if not query
            or query in skill.name.casefold()
            or query in (skill.description or "").casefold()
        )
        skill_list = self.query_one("#skill-picker-list", ListView)
        skill_list.clear()
        skill_list.extend(
            ListItem(
                Horizontal(
                    Label(skill.name, classes="skill-picker-name", markup=False),
                    Label(
                        skill.description or "No description",
                        classes="skill-picker-description",
                        markup=False,
                    ),
                    classes="skill-picker-row",
                )
            )
            for skill in self.visible_skills
        )
        skill_list.index = 0 if self.visible_skills else None
        if not self.skills:
            help_text = "No skills loaded - Escape closes"
        elif not self.visible_skills:
            help_text = "No matching skills - Escape closes"
        else:
            help_text = "Enter inserts - F1 describes - Ctrl+Enter shows full skill"
        self.query_one("#skill-picker-help", Static).update(help_text)


@dataclass(frozen=True, slots=True)
class TreePickerResult:
    """Tree-picker branch selection.

    树形选择器的分支选择结果。
    """

    entry_id: str
    summarize: bool = False
    custom_instructions: str | None = None


class _TreePickerListItem(ListItem):
    """Tree entry that keeps inline label colors readable when highlighted.

    高亮时仍保持内联标签颜色清晰可读的树条目。
    """

    def __init__(
        self,
        choice: SessionTreeChoice,
        *,
        theme: TuiTheme,
        show_label_timestamp: bool = False,
    ) -> None:
        """Initialize one themed tree row from a branchable session choice.

        根据可分支会话选项初始化一条带主题的树行。
        """
        self.choice = choice
        self.theme = theme
        self.show_label_timestamp = show_label_timestamp
        super().__init__(
            Label(
                _tree_picker_label(
                    choice,
                    theme=theme,
                    show_label_timestamp=show_label_timestamp,
                ),
                markup=False,
            )
        )

    def watch_highlighted(self, value: bool) -> None:
        """Recolor inline label spans when the list highlight changes.

        列表高亮状态变化时重新着色内联标签区段。
        """
        super().watch_highlighted(value)
        self.query_one(Label).update(
            _tree_picker_label(
                self.choice,
                theme=self.theme,
                highlighted=value,
                show_label_timestamp=self.show_label_timestamp,
            )
        )


class TreePickerScreen(ModalScreen[TreePickerResult | None]):
    """Modal picker for branching from a previous session entry.

    用于从先前会话条目创建分支的模态选择器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Branch", show=False),
        Binding("s", "select_with_summary", "Summarize", show=False),
        Binding("c", "select_with_custom_summary", "Custom summary", show=False),
        Binding("ctrl+t", "toggle_tool_calls", "Tool calls", show=False),
        Binding("l", "edit_label", "Label", show=False),
        Binding("ctrl+f", "toggle_labeled_only", "Labeled", show=False),
        Binding("ctrl+l", "toggle_label_timestamps", "Label time", show=False),
    ]

    def __init__(
        self,
        choices: Sequence[SessionTreeChoice],
        *,
        theme: TuiTheme,
        on_label_change: Callable[[str, str | None], Awaitable[float]] | None = None,
    ) -> None:
        """Initialize the branch picker with session choices and filter state.

        使用会话选项和筛选状态初始化分支选择器。
        """
        super().__init__()
        self.choices = tuple(choices)
        self.theme = theme
        self.on_label_change = on_label_change
        self.show_tool_calls = True
        self.labeled_only = False
        self.show_label_timestamps = False

    def compose(self) -> ComposeResult:
        """Compose the tree picker.

        组合树形选择器。
        """
        with Vertical(id="tree-picker"):
            yield Static("Session Tree", id="tree-picker-title")
            yield ListView(
                *self._list_items(),
                id="tree-picker-list",
            )
            yield Static(
                self._help_text(),
                id="tree-picker-help",
            )

    def on_mount(self) -> None:
        """Focus the tree list for keyboard navigation.

        聚焦树列表以供键盘导航。
        """
        tree_list = self.query_one("#tree-picker-list", ListView)
        tree_list.index = _active_tree_choice_index(self.choices)
        tree_list.focus()

    def on_key(self, event: Key) -> None:
        """Route tree picker keys to the list.

        将树形选择器按键路由到列表。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()
        elif event.key == "s":
            event.stop()
            self.action_select_with_summary()
        elif event.key == "c":
            event.stop()
            self.action_select_with_custom_summary()
        elif event.key == "ctrl+t":
            event.stop()
            self.action_toggle_tool_calls()
        elif event.key == "l":
            event.stop()
            self.action_edit_label()
        elif event.key == "ctrl+f":
            event.stop()
            self.action_toggle_labeled_only()
        elif event.key == "ctrl+l":
            event.stop()
            self.action_toggle_label_timestamps()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the selected entry id.

        使用选定条目标识符关闭选择器。
        """
        self.dismiss(TreePickerResult(entry_id=self._visible_choices()[event.index].entry_id))

    def action_cursor_up(self) -> None:
        """Move to the previous tree entry.

        移动到上一个树条目。
        """
        self.query_one("#tree-picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move to the next tree entry.

        移动到下一个树条目。
        """
        self.query_one("#tree-picker-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Branch from the highlighted entry without a summary.

        从高亮条目创建分支且不生成摘要。
        """
        self.query_one("#tree-picker-list", ListView).action_select_cursor()

    def action_select_with_summary(self) -> None:
        """Branch from the highlighted entry with a branch summary.

        从高亮条目创建分支并生成分支摘要。
        """
        tree_list = self.query_one("#tree-picker-list", ListView)
        index = tree_list.index
        if index is None:
            return
        self.dismiss(
            TreePickerResult(entry_id=self._visible_choices()[index].entry_id, summarize=True)
        )

    def action_select_with_custom_summary(self) -> None:
        """Branch from the highlighted entry with custom summary instructions.

        使用自定义摘要指令从高亮条目创建分支。
        """
        tree_list = self.query_one("#tree-picker-list", ListView)
        index = tree_list.index
        if index is None:
            return
        self.app.push_screen(
            BranchSummaryInstructionsScreen(theme=self.theme),
            callback=lambda instructions: self._dismiss_with_custom_summary(index, instructions),
        )

    def _dismiss_with_custom_summary(self, index: int, instructions: str | None) -> None:
        """Dismiss with a custom-summary selection when instructions exist.

        存在指令时使用自定义摘要选择结果关闭选择器。
        """
        if instructions is None:
            return
        visible_choices = self._visible_choices()
        if index >= len(visible_choices):
            return
        self.dismiss(
            TreePickerResult(
                entry_id=visible_choices[index].entry_id,
                summarize=True,
                custom_instructions=instructions,
            )
        )

    def action_toggle_tool_calls(self) -> None:
        """Toggle tool-call entries in the tree picker.

        切换树形选择器中的工具调用条目。
        """
        selected_entry_id = self._selected_entry_id()
        self.show_tool_calls = not self.show_tool_calls
        self.run_worker(self._refresh_choices(selected_entry_id=selected_entry_id))

    def action_toggle_labeled_only(self) -> None:
        """Toggle the Pi-style labeled-entry filter.

        切换 Pi 风格的已标记条目筛选器。
        """
        selected_entry_id = self._selected_entry_id()
        self.labeled_only = not self.labeled_only
        self.run_worker(self._refresh_choices(selected_entry_id=selected_entry_id))

    def action_toggle_label_timestamps(self) -> None:
        """Toggle display of the latest label-change timestamp.

        切换最新标签变更时间戳的显示。
        """
        selected_entry_id = self._selected_entry_id()
        self.show_label_timestamps = not self.show_label_timestamps
        self.run_worker(self._refresh_choices(selected_entry_id=selected_entry_id))

    def action_edit_label(self) -> None:
        """Open a prefilled editor; submitting an empty value clears the label.

        打开预填充编辑器；提交空值会清除标签。
        """
        selected = self._selected_choice()
        if selected is None:
            return
        self.app.push_screen(
            ExtensionInputScreen(
                "Label this session entry",
                "Empty clears the label",
                theme=self.theme,
                value=selected.bookmark_label or "",
            ),
            callback=lambda value: self._handle_label_input(selected.entry_id, value),
        )

    def _handle_label_input(self, entry_id: str, value: str | None) -> None:
        """Normalize submitted label text and schedule its persistence.

        规范化提交的标签文本并安排持久化。
        """
        if value is None:
            return
        self.run_worker(self._apply_label(entry_id, value))

    async def _apply_label(self, entry_id: str, value: str) -> None:
        """Persist a label update and refresh the visible tree choices.

        持久化标签更新并刷新可见树选项。
        """
        normalized = value.strip() or None
        try:
            if self.on_label_change is None:
                raise RuntimeError("Session labels are not available.")
            timestamp = await self.on_label_change(entry_id, normalized)
        except Exception as exc:  # noqa: BLE001 - keep the tree open and surface persistence errors

            # BLE001：保留树视图打开，并向用户显示持久化错误。
            self.app.notify(f"Error: {exc}", severity="error")
            return
        self.choices = tuple(
            replace(
                choice,
                bookmark_label=normalized,
                label_timestamp=timestamp if normalized is not None else None,
            )
            if choice.entry_id == entry_id
            else choice
            for choice in self.choices
        )
        await self._refresh_choices(selected_entry_id=entry_id)

    async def _refresh_choices(self, *, selected_entry_id: str | None = None) -> None:
        """Reload tree choices while preserving selection when possible.

        重新加载树选项，并尽可能保留选中项。
        """
        selected_entry_id = selected_entry_id or self._selected_entry_id()
        tree_list = self.query_one("#tree-picker-list", ListView)
        await tree_list.clear()
        await tree_list.extend(self._list_items())
        visible_choices = self._visible_choices()
        tree_list.index = (
            _tree_choice_index(visible_choices, selected_entry_id) if visible_choices else None
        )
        self.query_one("#tree-picker-help", Static).update(self._help_text())

    def _selected_entry_id(self) -> str | None:
        """Return the entry id represented by the highlighted row.

        返回高亮行所表示的条目标识符。
        """
        tree_list = self.query_one("#tree-picker-list", ListView)
        index = tree_list.index
        visible_choices = self._visible_choices()
        if index is None or index >= len(visible_choices):
            return None
        return visible_choices[index].entry_id

    def _selected_choice(self) -> SessionTreeChoice | None:
        """Return the complete choice represented by the highlighted row.

        返回高亮行所表示的完整选项。
        """
        entry_id = self._selected_entry_id()
        return next((choice for choice in self.choices if choice.entry_id == entry_id), None)

    def _visible_choices(self) -> tuple[SessionTreeChoice, ...]:
        """Apply tool-call and label filters to available choices.

        将工具调用和标签筛选器应用到可用选项。
        """
        return tuple(
            choice
            for choice in self.choices
            if (self.show_tool_calls or not choice.is_tool_call)
            and (not self.labeled_only or choice.bookmark_label is not None)
        )

    def _list_items(self) -> list[ListItem]:
        """Build themed list items for the currently visible choices.

        为当前可见选项构建带主题的列表项。
        """
        return [
            _TreePickerListItem(
                choice,
                theme=self.theme,
                show_label_timestamp=self.show_label_timestamps,
            )
            for choice in self._visible_choices()
        ]

    def _help_text(self) -> str:
        """Build footer help text for the active tree filters.

        为当前树筛选状态构建页脚帮助文本。
        """
        tool_call_state = "shown" if self.show_tool_calls else "hidden"
        labeled_state = "only" if self.labeled_only else "all"
        time_state = "shown" if self.show_label_timestamps else "hidden"
        return (
            "Enter branch · L label/clear · S summary · C custom · "
            f"Ctrl+T tool calls {tool_call_state} · Ctrl+F labels {labeled_state} · "
            f"Ctrl+L times {time_state} · Esc close"
        )

    def action_cancel(self) -> None:
        """Close the picker without selecting an entry.

        不选择条目并关闭选择器。
        """
        self.dismiss(None)


class BranchSummaryInstructionsScreen(ModalScreen[str | None]):
    """Prompt for custom branch-summary instructions.

    用于输入自定义分支摘要指令的提示框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [Binding("escape", "cancel", "Cancel")]

    # 初始化自定义分支摘要指令输入屏幕及主题。
    def __init__(self, *, theme: TuiTheme) -> None:
        super().__init__()
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the custom-instructions prompt.

        组合自定义指令提示框。
        """
        with Vertical(id="branch-summary-instructions"):
            yield Static(
                "Custom summarization instructions",
                id="branch-summary-instructions-title",
            )
            yield TextArea(id="branch-summary-instructions-input")
            yield Static(
                "Ctrl+Enter submits - Escape returns to tree",
                id="branch-summary-instructions-help",
            )

    def on_mount(self) -> None:
        """Focus the instruction editor.

        聚焦指令编辑器。
        """
        self.query_one("#branch-summary-instructions-input", TextArea).focus()

    def on_key(self, event: Key) -> None:
        """Submit on Ctrl+Enter and cancel on Escape.

        按 Ctrl+Enter 提交，按 Escape 取消。
        """
        if event.key == "ctrl+enter":
            event.stop()
            self.action_submit()
        elif event.key == "escape":
            event.stop()
            self.action_cancel()

    def action_submit(self) -> None:
        """Submit custom instructions.

        提交自定义指令。
        """
        value = self.query_one("#branch-summary-instructions-input", TextArea).text.strip()
        self.dismiss(value or None)

    def action_cancel(self) -> None:
        """Cancel custom instructions.

        取消自定义指令。
        """
        self.dismiss(None)


class CommandOutputScroll(VerticalScroll):
    """Scrollable command output area with deterministic arrow-key scrolling.

    使用确定性方向键滚动的可滚动命令输出区域。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("up", "scroll_up", "Scroll up", show=False, priority=True),
        Binding("down", "scroll_down", "Scroll down", show=False, priority=True),
    ]

    def action_scroll_up(self) -> None:
        """Scroll command output up.

        向上滚动命令输出。
        """
        self.scroll_y = max(0, self.scroll_y - 1)

    def action_scroll_down(self) -> None:
        """Scroll command output down.

        向下滚动命令输出。
        """
        self.scroll_y = min(self.max_scroll_y, self.scroll_y + 1)


class CommandOutputScreen(ModalScreen[None]):
    """Dismissible modal for slash-command output.

    可关闭的斜杠命令输出模态界面。
    """

    auto_copy_selection: bool = False

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "close", "Close"),
        Binding("enter", "close", "Close"),
        Binding("up", "scroll_up", "Scroll up", show=False, priority=True),
        Binding("down", "scroll_down", "Scroll down", show=False, priority=True),
    ]

    # 初始化命令输出标题、正文、主题及自动复制设置。
    def __init__(
        self,
        title: str,
        message: str,
        *,
        theme: TuiTheme,
        auto_copy_selection: bool = False,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.message = message
        self.theme = theme
        self.auto_copy_selection = auto_copy_selection

    def compose(self) -> ComposeResult:
        """Compose command output.

        组合命令输出界面。
        """
        with Vertical(id="command-output"):
            yield Static(self.title_text, id="command-output-title")
            with CommandOutputScroll(id="command-output-scroll"):
                yield Static(self.message, id="command-output-body", markup=False)
            yield Static(self._help_text(), id="command-output-help")

    def on_mount(self) -> None:
        """Focus the scroll area so arrow keys navigate long output.

        聚焦滚动区域，使方向键可以浏览较长输出。
        """
        self.query_one("#command-output-scroll", VerticalScroll).focus()

    def on_key(self, event: Key) -> None:
        """Route arrow keys to the command output scroll area.

        将方向键路由到命令输出滚动区域。
        """
        if event.key == "up":
            event.stop()
            self.action_scroll_up()
        elif event.key == "down":
            event.stop()
            self.action_scroll_down()

    def action_close(self) -> None:
        """Close the command output modal.

        关闭命令输出模态界面。
        """
        self.dismiss(None)

    # 根据自动复制设置生成关闭与选择帮助文本。
    def _help_text(self) -> str:
        if self.auto_copy_selection:
            return "Select text to copy - Enter or Escape closes"
        return "Enter or Escape closes"

    def action_scroll_up(self) -> None:
        """Scroll command output up.

        向上滚动命令输出。
        """
        self.query_one("#command-output-scroll", CommandOutputScroll).action_scroll_up()

    def action_scroll_down(self) -> None:
        """Scroll command output down.

        向下滚动命令输出。
        """
        self.query_one("#command-output-scroll", CommandOutputScroll).action_scroll_down()


class LoginProviderSearchInput(Input):
    """Search input that keeps provider-picker navigation local.

    将提供者选择器导航限制在本地的搜索输入框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    # 返回拥有此搜索输入框的登录提供商选择器。
    def _picker(self) -> LoginProviderPickerScreen:
        return cast(LoginProviderPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:
        """Route picker control keys before the input edits its text.

        在输入框编辑文本前路由选择器控制键。
        """
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def action_cursor_up(self) -> None:
        """Move the provider picker selection up.

        向上移动提供者选择器的选中项。
        """
        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move the provider picker selection down.

        向下移动提供者选择器的选中项。
        """
        self._picker().action_cursor_down()

    def action_cancel(self) -> None:
        """Close the provider picker.

        关闭提供者选择器。
        """
        self._picker().action_cancel()


class _LoginFlowAction(Enum):
    """Navigation actions returned by nested login screens.

    嵌套登录屏幕返回的导航操作。
    """

    BACK = auto()


class LoginProviderPickerScreen(ModalScreen[str | _LoginFlowAction | None]):
    """Searchable provider picker for the TUI login flow.

    用于 TUI 登录流程的可搜索提供者选择器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+d", "close", "Close", priority=True),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    # 初始化提供商候选项、导航方式、主题和标题。
    def __init__(
        self,
        providers: Sequence[ProviderCatalogEntry],
        *,
        theme: TuiTheme,
        title: str = "Login",
        back_on_cancel: bool = False,
    ) -> None:
        super().__init__()
        self.providers = tuple(providers)
        self.back_on_cancel = back_on_cancel
        self.visible_providers = self.providers
        self.theme = theme
        self.title_text = title

    def compose(self) -> ComposeResult:
        """Compose the provider picker.

        组合提供者选择器。
        """
        with Vertical(id="login-provider-picker"):
            yield Static(self.title_text, id="login-provider-title")
            yield LoginProviderSearchInput(
                placeholder="Search providers",
                id="login-provider-search",
            )
            yield ListView(
                *[
                    ListItem(Label(_login_provider_label(provider), markup=False))
                    for provider in self.providers
                ],
                id="login-provider-list",
            )
            yield Static("Enter selects - Escape closes", id="login-provider-help")

    async def on_mount(self) -> None:
        """Focus the provider search field.

        聚焦提供者搜索字段。
        """
        self.query_one("#login-provider-search", Input).focus()
        await self._refresh_provider_list()

    async def on_input_changed(self, event: Input.Changed) -> None:
        """Filter providers as the search value changes.

        搜索值变化时筛选提供者。
        """
        if event.input.id != "login-provider-search":
            return
        event.stop()
        self.visible_providers = _filter_login_providers(self.providers, event.value)
        await self._refresh_provider_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Select the highlighted provider from the search field.

        从搜索字段选择高亮提供者。
        """
        if event.input.id != "login-provider-search":
            return
        event.stop()
        self._select_visible_provider()

    def on_key(self, event: Key) -> None:
        """Route provider picker keys to the list.

        将提供者选择器按键路由到列表。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the selected provider name.

        使用选定的提供者名称关闭选择器。
        """
        event.stop()
        self._select_visible_provider()

    def action_cursor_up(self) -> None:
        """Move to the previous provider.

        移动到上一个提供者。
        """
        self.query_one("#login-provider-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move to the next provider.

        移动到下一个提供者。
        """
        self.query_one("#login-provider-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Select the highlighted provider.

        选择高亮提供者。
        """
        self._select_visible_provider()

    def action_cancel(self) -> None:
        """Go back in a login flow, or close a standalone provider picker.

        在登录流程中返回，或关闭独立的提供者选择器。
        """
        self.dismiss(_LoginFlowAction.BACK if self.back_on_cancel else None)

    def action_close(self) -> None:
        """Close the entire login flow.

        关闭整个登录流程。
        """
        self.dismiss(None)

    # 选择当前高亮的可见提供商，并在列表尚未高亮时回退到首项。
    def _select_visible_provider(self) -> None:
        if not self.visible_providers:
            return
        provider_list = self.query_one("#login-provider-list", ListView)
        # Fall back to the first match: submitting from the search field can
        # land here before the refreshed list has applied its highlight.
        #
        # 回退到第一个匹配项：从搜索字段提交时，刷新后的列表可能尚未应用高亮。
        index = provider_list.index
        self.dismiss(self.visible_providers[0 if index is None else index].name)

    # 异步重建过滤后的提供商列表并更新帮助文本。
    async def _refresh_provider_list(self) -> None:
        provider_list = self.query_one("#login-provider-list", ListView)
        # Await the mounts: assigning the index while the list is still empty
        # validates it back to None, leaving the first provider unreachable
        # with the down key (issue #494).
        #
        # 等待挂载完成：列表仍为空时设置索引会被验证回 None，导致无法使用向下键
        # 访问第一个提供者（问题 #494）。
        await provider_list.clear()
        await provider_list.extend(
            [
                ListItem(Label(_login_provider_label(provider), markup=False))
                for provider in self.visible_providers
            ]
        )
        provider_list.index = 0 if self.visible_providers else None
        help_text = (
            "Enter selects - Escape closes"
            if self.visible_providers
            else "No matching providers - Escape closes"
        )
        self.query_one("#login-provider-help", Static).update(help_text)


@dataclass(frozen=True, slots=True)
class CustomProviderLoginResult:
    """Provider details collected by the custom-provider login flow.

    自定义提供者登录流程收集的提供者详情。
    """

    provider_name: str
    display_name: str
    base_url: str
    api_key_env: str
    models: tuple[str, ...]
    default_model: str
    api_key: str


class LoginMethodPickerScreen(ModalScreen[str | None]):
    """Login method picker for the TUI login flow.

    TUI 登录流程的登录方式选择器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+d", "cancel", "Close", priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_cursor", "Select", show=False, priority=True),
    ]

    # 初始化登录方式选择器主题。
    def __init__(self, *, theme: TuiTheme) -> None:
        super().__init__()
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the login method picker.

        组合登录方式选择器。
        """
        with Vertical(id="login-method-picker"):
            yield Static("Login", id="login-method-title")
            yield Static("Choose how to authenticate.", id="login-method-intro")
            yield LoginMethodListView(
                ListItem(
                    Label("Subscription — OAuth account", markup=False),
                    id="login-method-subscription",
                ),
                ListItem(
                    Label("API key — built-in provider", markup=False),
                    id="login-method-api-key",
                ),
                ListItem(
                    Label("Custom provider — OpenAI-compatible", markup=False),
                    id="login-method-custom",
                ),
                id="login-method-list",
            )
            yield Static("Enter selects - Escape/Ctrl+D closes", id="login-method-help")

    def on_mount(self) -> None:
        """Focus the default subscription method.

        聚焦默认订阅登录方式。
        """
        method_list = self.query_one("#login-method-list", ListView)
        method_list.index = 0
        method_list.focus()

    def on_key(self, event: Key) -> None:
        """Route arrow keys between login method buttons.

        在登录方式按钮之间路由方向键。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Dismiss with the selected login method.

        使用选定登录方式关闭选择器。
        """
        if event.button.id == "login-method-subscription":
            self.dismiss("subscription")
        elif event.button.id == "login-method-api-key":
            self.dismiss("api-key")
        elif event.button.id == "login-method-custom":
            self.dismiss("custom")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the selected login method.

        使用选定的登录方式关闭选择器。
        """
        if event.item.id == "login-method-subscription":
            self.dismiss("subscription")
        elif event.item.id == "login-method-api-key":
            self.dismiss("api-key")
        elif event.item.id == "login-method-custom":
            self.dismiss("custom")

    def action_cancel(self) -> None:
        """Close without selecting a login method.

        不选择登录方式并关闭。
        """
        self.dismiss(None)

    def action_cursor_up(self) -> None:
        """Focus the previous login method.

        聚焦上一个登录方式。
        """
        self._move_method_cursor(offset=-1)

    def action_cursor_down(self) -> None:
        """Focus the next login method.

        聚焦下一个登录方式。
        """
        self._move_method_cursor(offset=1)

    def action_select_cursor(self) -> None:
        """Select the currently focused login method.

        选择当前聚焦的登录方式。
        """
        self.query_one("#login-method-list", ListView).action_select_cursor()

    # 按偏移量循环移动登录方式选中项。
    def _move_method_cursor(self, *, offset: int) -> None:
        method_list = self.query_one("#login-method-list", ListView)
        item_count = len(method_list.children)
        if item_count == 0:
            method_list.index = None
            return
        current_index = method_list.index if method_list.index is not None else 0
        method_list.index = (current_index + offset) % item_count


class LoginMethodListView(ListView):
    """List view with wrapping arrow navigation for the login method picker.

    为登录方式选择器提供循环方向键导航的列表视图。
    """

    def action_cursor_up(self) -> None:
        """Move to the previous login method.

        移动到上一个登录方式。
        """
        self._move_cursor(offset=-1)

    def action_cursor_down(self) -> None:
        """Move to the next login method.

        移动到下一个登录方式。
        """
        self._move_cursor(offset=1)

    # 按偏移量循环移动列表光标。
    def _move_cursor(self, *, offset: int) -> None:
        item_count = len(self.children)
        if item_count == 0:
            self.index = None
            return
        current_index = self.index if self.index is not None else 0
        self.index = (current_index + offset) % item_count


class ThemePickerScreen(ModalScreen[TuiThemeName | None]):
    """Theme picker for the available TUI themes.

    用于可用 TUI 主题的选择器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("enter", "select_cursor", "Select", show=False, priority=True),
    ]

    # 初始化当前主题、渲染主题及全部可选主题名称。
    def __init__(
        self,
        *,
        current_theme: TuiThemeName,
        theme: TuiTheme,
        theme_names: tuple[TuiThemeName, ...],
    ) -> None:
        super().__init__()
        self.current_theme = current_theme
        self.theme = theme
        self.theme_names = theme_names

    def compose(self) -> ComposeResult:
        """Compose the theme picker.

        组合主题选择器。
        """
        with Vertical(id="theme-picker"):
            yield Static("Theme", id="theme-picker-title")
            yield ListView(
                *[
                    ListItem(
                        Label(
                            _theme_picker_label(theme_name, current_theme=self.current_theme),
                            markup=False,
                        )
                    )
                    for theme_name in self.theme_names
                ],
                id="theme-picker-list",
            )
            yield Static("Enter selects - Escape closes", id="theme-picker-help")

    def on_mount(self) -> None:
        """Select the current theme.

        选择当前主题。
        """
        theme_list = self.query_one("#theme-picker-list", ListView)
        try:
            theme_list.index = self.theme_names.index(self.current_theme)
        except ValueError:
            theme_list.index = 0
        theme_list.focus()

    def on_key(self, event: Key) -> None:
        """Route theme picker keys to the list.

        将主题选择器按键路由到列表。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_select_cursor()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Dismiss with the selected theme name.

        使用选定主题名称关闭选择器。
        """
        self.dismiss(self.theme_names[event.index])

    def action_cursor_up(self) -> None:
        """Move to the previous theme.

        移动到上一个主题。
        """
        self.query_one("#theme-picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move to the next theme.

        移动到下一个主题。
        """
        self.query_one("#theme-picker-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Select the highlighted theme.

        选择高亮主题。
        """
        self.query_one("#theme-picker-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:
        """Close without selecting a theme.

        不选择主题并关闭。
        """
        self.dismiss(None)


class ModelPickerSearchInput(Input):
    """Search input that keeps model-picker control keys local to the picker.

    将模型选择器控制键限制在选择器内部的搜索输入框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel", show=False, priority=True),
        Binding("tab", "toggle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+i", "toggle_mode", "Mode", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("down", "cursor_down", "Down", show=False, priority=True),
    ]

    # 返回拥有此搜索输入框的模型选择器。
    def _picker(self) -> ModelPickerScreen:
        return cast(ModelPickerScreen, self.screen)

    def on_key(self, event: Key) -> None:
        """Route picker control keys before the input edits its text.

        在输入框编辑文本前路由选择器控制键。
        """
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.action_cursor_down()
        elif event.key in {"tab", "ctrl+i"}:
            event.stop()
            event.prevent_default()
            self.action_toggle_mode()
        elif event.key == "escape":
            event.stop()
            event.prevent_default()
            self.action_cancel()

    def action_cursor_up(self) -> None:
        """Move the model picker selection up.

        向上移动模型选择器的选中项。
        """
        self._picker().action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move the model picker selection down.

        向下移动模型选择器的选中项。
        """
        self._picker().action_cursor_down()

    def action_toggle_mode(self) -> None:
        """Toggle between all and scoped picker modes.

        在全部模型和限定模型选择模式之间切换。
        """
        self._picker().action_toggle_mode()

    def action_cancel(self) -> None:
        """Close the model picker.

        关闭模型选择器。
        """
        self._picker().action_cancel()


class ModelPickerScreen(ModalScreen[ModelChoice | None]):
    """Model picker for the active TUI provider.

    用于当前 TUI 提供者的模型选择器。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("tab", "toggle_mode", "Mode", show=False, priority=True),
        Binding("ctrl+i", "toggle_mode", "Mode", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "accept_model", "Select", show=False),
    ]

    # 初始化可用模型、范围模型、当前模型及选择器模式。
    def __init__(
        self,
        choices: Sequence[ModelChoice],
        *,
        scoped_choices: Sequence[ModelChoice],
        current_model: str,
        provider_name: str,
        theme: TuiTheme,
        on_toggle_scoped: Callable[[ModelChoice], Sequence[ModelChoice]] | None = None,
        picker_kind: Literal["model", "scoped"] = "model",
    ) -> None:
        super().__init__()
        available = tuple(dict.fromkeys(choices))
        self.scoped_choices = tuple(dict.fromkeys(scoped_choices))
        self.unavailable_choices = frozenset(self.scoped_choices) - frozenset(available)
        self.choices = tuple(dict.fromkeys((*available, *self.scoped_choices)))
        self.visible_choices = self.choices
        self.current_model = current_model
        self.provider_name = provider_name
        self.theme = theme
        self.on_toggle_scoped = on_toggle_scoped
        self.picker_kind = picker_kind
        self.mode: Literal["all", "scoped"] = "all"
        self.search_value = ""

    def compose(self) -> ComposeResult:
        """Compose the model picker.

        组合模型选择器。
        """
        with Vertical(id="model-picker"):
            title = (
                f"Model: {self.provider_name}" if self.picker_kind == "model" else "Scoped models"
            )
            yield Static(title, id="model-picker-title")
            yield Static("", id="model-picker-tabs")
            yield ModelPickerSearchInput(placeholder="Search models", id="model-picker-search")
            yield ListView(
                *[
                    ListItem(
                        Label(
                            _model_picker_label(
                                choice,
                                current_model=self.current_model,
                                current_provider=self.provider_name,
                                scoped=choice in self.scoped_choices,
                                unavailable=choice in self.unavailable_choices,
                            ),
                            markup=False,
                        )
                    )
                    for choice in self.choices
                ],
                id="model-picker-list",
            )
            yield Static("", id="model-picker-help")

    def on_mount(self) -> None:
        """Focus the search field.

        聚焦搜索字段。
        """
        search = self.query_one("#model-picker-search", Input)
        search.focus()
        self._refresh_model_list()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter model choices as the search value changes.

        搜索值变化时筛选模型选项。
        """
        if event.input.id != "model-picker-search":
            return
        event.stop()
        self.search_value = event.value
        self._refresh_model_list()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Select the highlighted model from the search field.

        从搜索字段选择高亮模型。
        """
        if event.input.id != "model-picker-search":
            return
        event.stop()
        self._select_visible_choice()

    def _reset_model_list_index(self) -> None:
        """Move selection to the current model or first visible row.

        将选中项移到当前模型或第一条可见行。
        """
        model_list = self.query_one("#model-picker-list", ListView)
        if not self.visible_choices:
            model_list.index = None
            return
        try:
            model_list.index = self.visible_choices.index(
                ModelChoice(provider_name=self.provider_name, model=self.current_model)
            )
        except ValueError:
            model_list.index = 0

    def on_key(self, event: Key) -> None:
        """Route model picker keys to the list.

        将模型选择器按键路由到列表。
        """
        if event.key == "up":
            event.stop()
            self.action_cursor_up()
        elif event.key == "down":
            event.stop()
            self.action_cursor_down()
        elif event.key == "enter":
            event.stop()
            self.action_accept_model()
        elif event.key in {"tab", "ctrl+i"}:
            event.stop()
            self.action_toggle_mode()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Handle the selected row.

        处理选中的模型行。
        """
        event.stop()
        self._select_visible_choice()

    def action_cursor_up(self) -> None:
        """Move to the previous model.

        移动到上一个模型。
        """
        self.query_one("#model-picker-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move to the next model.

        移动到下一个模型。
        """
        self.query_one("#model-picker-list", ListView).action_cursor_down()

    def action_accept_model(self) -> None:
        """Select the highlighted model.

        选择高亮模型。
        """
        self._select_visible_choice()

    def action_toggle_mode(self) -> None:
        """Toggle between all models and scoped models.

        在全部模型和限定模型之间切换。
        """
        self.mode = "scoped" if self.mode == "all" else "all"
        self._refresh_model_list()

    def action_toggle_scoped(self) -> None:
        """Add or remove the highlighted model from scoped models.

        在限定模型中添加或移除高亮模型。
        """
        if self.on_toggle_scoped is None or not self.visible_choices:
            return
        model_list = self.query_one("#model-picker-list", ListView)
        index = model_list.index
        if index is None:
            return
        choice = self.visible_choices[index]
        self.scoped_choices = tuple(dict.fromkeys(self.on_toggle_scoped(choice)))
        self._refresh_model_list()

    def action_cancel(self) -> None:
        """Close without selecting a model.

        不选择模型并关闭。
        """
        self.dismiss(None)

    def update_choices(
        self,
        choices: Sequence[ModelChoice],
        scoped_choices: Sequence[ModelChoice],
    ) -> None:
        """Publish a refreshed catalog without replacing the open picker.

        发布刷新的模型目录，同时不替换已打开的选择器。
        """
        available = tuple(dict.fromkeys(choices))
        self.scoped_choices = tuple(dict.fromkeys(scoped_choices))
        self.unavailable_choices = frozenset(self.scoped_choices) - frozenset(available)
        self.choices = tuple(dict.fromkeys((*available, *self.scoped_choices)))
        self._refresh_model_list()

    # 返回当前高亮模型，或在范围管理模式中切换其范围状态。
    def _select_visible_choice(self) -> None:
        if not self.visible_choices:
            return
        model_list = self.query_one("#model-picker-list", ListView)
        index = model_list.index
        if index is None:
            return
        choice = self.visible_choices[index]
        if self.picker_kind == "scoped":
            self.action_toggle_scoped()
            return
        if choice in self.unavailable_choices:
            return
        self.dismiss(choice)

    # 按当前标签和搜索条件重建模型列表、标签及帮助文本。
    def _refresh_model_list(self) -> None:
        base_choices = self.scoped_choices if self.mode == "scoped" else self.choices
        self.visible_choices = _filter_model_choices(base_choices, self.search_value)
        model_list = self.query_one("#model-picker-list", ListView)
        model_list.clear()
        model_list.extend(
            [
                ListItem(
                    Label(
                        _model_picker_label(
                            choice,
                            current_model=self.current_model,
                            current_provider=self.provider_name,
                            scoped=choice in self.scoped_choices,
                            unavailable=choice in self.unavailable_choices,
                        ),
                        markup=False,
                    )
                )
                for choice in self.visible_choices
            ]
        )
        self._reset_model_list_index()
        scope_count = len(self.scoped_choices)
        tabs = self.query_one("#model-picker-tabs", Static)
        if self.picker_kind == "scoped":
            if self.mode == "all":
                tabs.update("Tabs: ● All models  ○ Scoped models")
                help_text = (
                    "all models: no matching models - Tab switches to scoped models"
                    if not self.visible_choices
                    else (
                        "All models - Enter toggles scoped model - Tab switches tabs - "
                        f"{scope_count} scoped - active model is unchanged"
                    )
                )
            else:
                tabs.update("Tabs: ○ All models  ● Scoped models")
                help_text = (
                    "scoped models: no scoped models - Tab switches to all models"
                    if not self.visible_choices
                    else "Scoped models - Enter removes scoped model - Tab switches tabs"
                )
        elif self.mode == "all":
            tabs.update("Tabs: ● All models  ○ Scoped models")
            help_text = (
                "all models: no matching models - Tab switches to scoped models"
                if not self.visible_choices
                else (
                    "All models - Enter selects active model - Tab switches tabs - "
                    f"{scope_count} scoped"
                )
            )
        else:
            tabs.update("Tabs: ○ All models  ● Scoped models")
            help_text = (
                "scoped models: no matching models - Tab switches to all models"
                if not self.visible_choices
                else "Scoped models - Enter selects active model - Tab switches tabs"
            )
        self.query_one("#model-picker-help", Static).update(help_text)


class CustomProviderLoginScreen(ModalScreen[CustomProviderLoginResult | _LoginFlowAction | None]):
    """Prompt for adding an OpenAI-compatible custom provider.

    用于添加兼容 OpenAI 的自定义提供者的提示框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+d", "close", "Close", priority=True),
    ]

    _INPUT_ORDER: ClassVar[tuple[str, ...]] = (
        "custom-provider-name",
        "custom-provider-display-name",
        "custom-provider-base-url",
        "custom-provider-api-key-env",
        "custom-provider-models",
        "custom-provider-default-model",
        "custom-provider-api-key",
    )

    # 初始化自定义提供商登录表单主题。
    def __init__(self, *, theme: TuiTheme) -> None:
        super().__init__()
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the custom provider prompt.

        组合自定义提供者提示框。
        """
        with Vertical(id="login-screen"):
            yield Static("Add custom provider", id="login-title")
            yield Static(
                "Short provider name is used in commands/config.",
                id="custom-provider-help",
            )
            yield Input(placeholder="Provider name/id, e.g. nebius", id="custom-provider-name")
            yield Input(
                placeholder="Display name shown in UI, e.g. Nebius AI Studio",
                id="custom-provider-display-name",
            )
            yield Input(
                placeholder="OpenAI-compatible base URL, e.g. https://api.studio.nebius.ai/v1",
                id="custom-provider-base-url",
            )
            yield Input(
                placeholder="API key environment variable fallback, e.g. NEBIUS_API_KEY",
                id="custom-provider-api-key-env",
            )
            yield Input(
                placeholder="Model ids, comma-separated, e.g. model-a, model-b",
                id="custom-provider-models",
            )
            yield Input(
                placeholder="Default model id, must be listed above",
                id="custom-provider-default-model",
            )
            yield Input(
                placeholder="Paste API key to save for this provider",
                password=True,
                id="custom-provider-api-key",
            )
            yield Static(
                "Enter advances/saves - Escape goes back - Ctrl+D closes",
                id="login-footer",
            )

    def on_mount(self) -> None:
        """Focus the first provider-detail field.

        聚焦第一个提供者详情字段。
        """
        self.query_one("#custom-provider-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Advance through fields, then dismiss with provider details.

        依次推进各字段，然后使用提供者详情关闭界面。
        """
        input_id = event.input.id
        if input_id not in self._INPUT_ORDER:
            return
        event.stop()
        if input_id != self._INPUT_ORDER[-1]:
            self._focus_next(input_id)
            return
        result = self._collect_result()
        if result is not None:
            self.dismiss(result)

    # 将焦点移动到字段顺序中的下一个输入框。
    def _focus_next(self, input_id: str) -> None:
        index = self._INPUT_ORDER.index(input_id)
        self.query_one(f"#{self._INPUT_ORDER[index + 1]}", Input).focus()

    # 校验所有字段并构造自定义提供商登录结果。
    def _collect_result(self) -> CustomProviderLoginResult | None:
        provider_name = self._field("custom-provider-name", "Provider name")
        if provider_name is None:
            return None
        base_url = self._field("custom-provider-base-url", "Base URL")
        if base_url is None:
            return None
        api_key_env = self._field("custom-provider-api-key-env", "API key environment variable")
        if api_key_env is None:
            return None
        models_text = self._field("custom-provider-models", "Model ids")
        if models_text is None:
            return None
        models = tuple(
            dict.fromkeys(item.strip() for item in models_text.split(",") if item.strip())
        )
        if not models:
            self.query_one("#custom-provider-help", Static).update(
                "At least one model id is required."
            )
            self.query_one("#custom-provider-models", Input).focus()
            return None
        default_model = self._field("custom-provider-default-model", "Default model")
        if default_model is None:
            return None
        if default_model not in models:
            self.query_one("#custom-provider-help", Static).update(
                "Default model must be included in the model list."
            )
            self.query_one("#custom-provider-default-model", Input).focus()
            return None
        api_key = self._field("custom-provider-api-key", "API key")
        if api_key is None:
            return None
        display_name = self.query_one("#custom-provider-display-name", Input).value.strip()
        return CustomProviderLoginResult(
            provider_name=provider_name,
            display_name=display_name or provider_name,
            base_url=base_url,
            api_key_env=api_key_env,
            models=models,
            default_model=default_model,
            api_key=api_key,
        )

    # 读取必填字段；为空时显示错误并把焦点移回该字段。
    def _field(self, input_id: str, label: str) -> str | None:
        value = self.query_one(f"#{input_id}", Input).value.strip()
        if value:
            return value
        self.query_one("#custom-provider-help", Static).update(f"{label} is required.")
        self.query_one(f"#{input_id}", Input).focus()
        return None

    def action_back(self) -> None:
        """Return to the login method picker.

        返回登录方式选择器。
        """
        self.dismiss(_LoginFlowAction.BACK)

    def action_close(self) -> None:
        """Close the entire login flow.

        关闭整个登录流程。
        """
        self.dismiss(None)


class LoginScreen(ModalScreen[str | _LoginFlowAction | None]):
    """Password prompt for saving a provider API key.

    用于保存提供者 API 密钥的密码提示框。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+d", "close", "Close", priority=True),
    ]

    # 初始化指定提供商的 API 密钥登录表单。
    def __init__(self, provider: ProviderCatalogEntry, *, theme: TuiTheme) -> None:
        super().__init__()
        self.provider = provider
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the provider login prompt.

        组合提供者登录提示框。
        """
        with Vertical(id="login-screen"):
            yield Static(f"Login: {self.provider.display_name}", id="login-title")
            yield Static("Paste this provider's API key.", id="login-help")
            yield Input(placeholder="Paste API key", password=True, id="login-api-key")
            yield Static("Enter saves - Escape goes back - Ctrl+D closes", id="login-footer")

    def on_mount(self) -> None:
        """Focus the API key field.

        聚焦 API 密钥字段。
        """
        self.query_one("#login-api-key", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Dismiss with the submitted API key.

        使用提交的 API 密钥关闭界面。
        """
        if event.input.id != "login-api-key":
            return
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_back(self) -> None:
        """Return to the login method picker without saving.

        不保存并返回登录方式选择器。
        """
        self.dismiss(_LoginFlowAction.BACK)

    def action_close(self) -> None:
        """Close the entire login flow.

        关闭整个登录流程。
        """
        self.dismiss(None)


class OAuthLoginScreen(ModalScreen[OAuthCredential | _LoginFlowAction | None]):
    """OAuth login flow for providers backed by subscription auth.

    由订阅认证支持的提供者 OAuth 登录流程。
    """

    BINDINGS: ClassVar[list[BindingEntry]] = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+d", "close", "Close", priority=True),
    ]

    # 初始化提供商 OAuth 流程、可选登录实现和手动验证码状态。
    def __init__(
        self,
        provider: ProviderCatalogEntry,
        *,
        theme: TuiTheme,
        login: Callable[[OAuthLoginCallbacks], Awaitable[OAuthCredential]] | None = None,
    ) -> None:
        super().__init__()
        self.provider = provider
        self.theme = theme
        self._login = login
        self._manual_code_future: asyncio.Future[str] | None = None
        self._manual_code_value: str | None = None
        self._prompt_allows_empty = False

    def compose(self) -> ComposeResult:
        """Compose the OAuth login prompt.

        组合 OAuth 登录提示框。
        """
        with Vertical(id="login-screen"):
            yield Static(f"Login: {self.provider.display_name}", id="login-title")
            yield Static("Follow the provider instructions to complete login.", id="login-help")
            yield Static("", id="login-oauth-url")
            yield Input(
                placeholder="Paste redirect URL or authorization code",
                id="login-oauth-code",
            )
            yield Static("Enter submits - Escape goes back - Ctrl+D closes", id="login-footer")

    def on_mount(self) -> None:
        """Focus the manual-code field and start OAuth.

        聚焦手动验证码字段并启动 OAuth。
        """
        self.query_one("#login-oauth-code", Input).focus()
        self.run_worker(self._run_login(), exclusive=True)

    async def _run_login(self) -> None:
        """Run the provider OAuth flow and dismiss with saved credentials.

        运行提供者 OAuth 流程，并在保存凭据后关闭界面。
        """
        try:
            oauth_provider = get_oauth_provider(self.provider.name)
            login = self._login or (oauth_provider.login if oauth_provider is not None else None)
            if login is None:
                raise RuntimeError(f"No OAuth implementation for {self.provider.name}")
            credential = await login(
                OAuthLoginCallbacks(
                    on_auth=self._show_auth,
                    on_device_code=self._show_device_code,
                    on_prompt=self._prompt_for_code,
                    on_select=self._select_option,
                    on_progress=self._show_progress,
                    on_manual_code_input=self._manual_code_input,
                )
            )
        except Exception as exc:  # noqa: BLE001 - surface OAuth failures in the TUI

            # BLE001：在 TUI 中显示 OAuth 登录失败。
            self.query_one("#login-help", Static).update(f"OAuth failed: {exc}")
            return
        self.dismiss(credential)

    def _show_auth(self, info: OAuthAuthInfo) -> None:
        """Display browser authorization instructions from the OAuth flow.

        显示 OAuth 流程返回的浏览器授权指引。
        """
        self._show_url(info.url)
        # Copy only the browser-flow URL. It is hundreds of characters long and
        # wraps across the dialog, so hand-selecting it is what corrupts it;
        # taking over the clipboard is worth it there and nowhere else.
        #
        # 只复制浏览器流程 URL。它长达数百个字符并会在对话框中换行，手动选择
        # 容易破坏内容；只有此处值得主动写入剪贴板，其他位置不应这样做。
        with suppress(Exception):
            self.app.copy_to_clipboard(info.url)
        self.notify("Authorization URL copied to clipboard.")
        if info.instructions:
            self.query_one("#login-help", Static).update(info.instructions)

    def _show_url(self, url: str) -> None:
        """Display and open the current OAuth authorization URL.

        显示并打开当前 OAuth 授权网址。
        """
        """Display a URL as one clickable unit.

        将 URL 显示为一个完整的可点击单元。

        Authorization URLs are far wider than the dialog, so they render across
        several wrapped lines. Selecting those lines by hand tends to corrupt
        the URL — a query parameter split across a wrap picks up the line break
        or trailing padding and the provider rejects the request. An OSC 8
        hyperlink keeps a click on any wrapped line opening the intact URL.

        授权 URL 远宽于对话框，因此会跨多行显示。手动选择这些行容易破坏 URL：
        跨换行的查询参数可能带入换行符或尾部填充，使提供商拒绝请求。OSC 8
        超链接可确保点击任意换行都打开完整 URL。
        """
        self.query_one("#login-oauth-url", Static).update(Text(url, style=Style(link=url)))

    def _show_device_code(self, info: OAuthDeviceCodeInfo) -> None:
        """Display device-code authorization details for the user.

        向用户显示设备码授权详情。
        """
        # No clipboard copy here: the verification URI is short and clickable,
        # and the thing the user carries to the browser is the code below it.
        #
        # 此处不复制到剪贴板：验证 URI 较短且可点击，用户需要带到浏览器的是
        # 下方的验证码。
        self._show_url(info.verification_uri)
        self.query_one("#login-help", Static).update(
            f"Open the URL and enter code: {info.user_code}"
        )

    def _show_progress(self, message: str) -> None:
        """Update the visible OAuth progress message.

        更新可见的 OAuth 进度消息。
        """
        self.query_one("#login-help", Static).update(message)

    async def _prompt_for_code(self, prompt: OAuthPrompt) -> str:
        """Collect a required OAuth code through the modal input bridge.

        通过模态输入桥接收集必需的 OAuth 验证码。
        """
        self.query_one("#login-help", Static).update(prompt.message)
        self._prompt_allows_empty = prompt.allow_empty
        try:
            return await self._manual_code_input()
        finally:
            self._prompt_allows_empty = False

    async def _select_option(self, prompt: OAuthSelectPrompt) -> str | None:
        """Collect one OAuth option through the modal selection bridge.

        通过模态选择桥接收集一个 OAuth 选项。
        """
        self.query_one("#login-help", Static).update(prompt.message)
        return prompt.options[0].id if prompt.options else None

    async def _manual_code_input(self) -> str:
        """Wait for manual OAuth code submission or cancellation.

        等待手动提交或取消 OAuth 验证码。
        """
        if self._manual_code_value is not None:
            return self._manual_code_value
        loop = asyncio.get_running_loop()
        self._manual_code_future = loop.create_future()
        try:
            return await self._manual_code_future
        finally:
            self._manual_code_future = None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Resolve the manual OAuth code fallback.

        解析手动 OAuth 验证码后备流程。
        """
        if event.input.id != "login-oauth-code":
            return
        event.stop()
        value = event.value.strip()
        if not value and not self._prompt_allows_empty:
            return
        self._manual_code_value = value
        if self._manual_code_future is not None and not self._manual_code_future.done():
            self._manual_code_future.set_result(value)

    def action_back(self) -> None:
        """Return to the login method picker without saving credentials.

        不保存凭据并返回登录方式选择器。
        """
        self._cancel_manual_code_input()
        self.dismiss(_LoginFlowAction.BACK)

    def action_close(self) -> None:
        """Close the entire login flow without saving credentials.

        不保存凭据并关闭整个登录流程。
        """
        self._cancel_manual_code_input()
        self.dismiss(None)

    def _cancel_manual_code_input(self) -> None:
        """Cancel and resolve any pending manual OAuth code request.

        取消并解析任何待处理的手动 OAuth 验证码请求。
        """
        if self._manual_code_future is not None and not self._manual_code_future.done():
            self._manual_code_future.cancel()


#: Keys an extension key interceptor is never consulted for. These flow
#: straight to normal dispatch so a buggy interceptor (one that returns True
#: too broadly) cannot swallow the session's hard interrupt/exit reflexes and
#: brick the TUI. Deliberately minimal — only the always-available escape
#: hatches: ``ctrl+d`` (the ``quit`` action, exits the app) and ``ctrl+c``
#: (Tau binds it to ``clear_prompt``, but it is the terminal-standard
#: SIGINT/interrupt reflex users hit to bail). NOT reserved: escape/enter/
#: arrows/tab/left/right — those are load-bearing for the tau-subagents
#: extension and must stay interceptable. This is Tau's counterpart to Pi's
#: ``RESERVED_KEYBINDINGS_FOR_EXTENSION_CONFLICTS`` (runner.ts:69), applied
#: here to the pre-dispatch interceptor rather than a registerShortcut API.
#:
#: 扩展按键拦截器永远不会接收到这些按键。它们会直接进入正常分发，避免行为
#: 异常且返回 True 范围过宽的拦截器吞掉会话的硬中断或退出操作，从而锁死 TUI。
#: 此集合刻意保持最小，只包含始终可用的逃生键：``ctrl+d``（退出应用的
#: ``quit`` 操作）和 ``ctrl+c``（Tau 将其绑定到 ``clear_prompt``，同时它也是
#: 用户退出操作时常用的终端标准 SIGINT/中断键）。escape、enter、方向键、tab、
#: left 和 right 不在保留范围，因为 tau-subagents 扩展依赖拦截这些键。这对应
#: Pi 的 ``RESERVED_KEYBINDINGS_FOR_EXTENSION_CONFLICTS``（runner.ts:69），但应用
#: 于预分发拦截器，而不是 registerShortcut API。
RESERVED_EXTENSION_INTERCEPTOR_KEYS: frozenset[str] = frozenset({"ctrl+c", "ctrl+d"})


class TauTuiApp(App[None]):
    """Interactive Textual frontend for a ``CodingSession``.

    ``CodingSession`` 的交互式 Textual 前端。
    """

    TITLE = "Tau"
    CSS = """
    Screen {
        layout: vertical;
        background: $tau-screen-background;
        color: $tau-screen-text;
    }

    Toast {
        background: $tau-chrome-background;
        color: $tau-chrome-text;
    }

    Toast .toast--title {
        color: $tau-accent;
    }

    #workspace {
        height: 1fr;
    }

    #sidebar {
        width: 40;
        min-width: 36;
        height: 1fr;
        padding: 1 1 1 2;
        background: $tau-prompt-background;
        border: none;
    }

    #sidebar-scroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }

    #sidebar-content {
        height: auto;
    }

    #sidebar .sidebar-separator {
        height: auto;
    }

    #sidebar .sidebar-resource-section {
        width: 1fr;
        height: auto;
        padding: 0;
        background: transparent;
        border: none;
    }

    #sidebar .sidebar-resource-section:focus-within {
        background-tint: transparent;
    }

    #sidebar .sidebar-resource-section CollapsibleTitle {
        width: 1fr;
        padding: 0 0 0 1;
        color: $tau-prompt-text;
        text-style: none;
        background: transparent;
    }

    #sidebar .sidebar-resource-section CollapsibleTitle:hover,
    #sidebar .sidebar-resource-section CollapsibleTitle:focus {
        color: $tau-prompt-text;
        text-style: none;
        background: transparent;
    }

    #sidebar .sidebar-resource-section Contents {
        padding: 1 0 0 1;
    }

    #sidebar .sidebar-section-title {
        height: 1;
        padding: 0 0 0 1;
        color: $tau-prompt-text;
        text-style: bold;
    }

    #sidebar .sidebar-file-list {
        width: 1fr;
        height: auto;
    }

    #sidebar .sidebar-resource-origin,
    #sidebar .sidebar-file-empty,
    #sidebar .sidebar-file-overflow {
        width: 1fr;
        height: auto;
        color: $tau-muted-text;
    }

    #sidebar .sidebar-file-item {
        width: 1fr;
        height: auto;
        color: $tau-muted-text;
        background: transparent;
    }

    #sidebar .sidebar-file-item:hover,
    #sidebar .sidebar-file-item:focus {
        color: $tau-highlight-text;
        background: $tau-highlight-background;
        text-style: underline;
    }

    #sidebar-extension-sections,
    #sidebar .extension-sidebar-section,
    #sidebar .extension-sidebar-body {
        width: 1fr;
        height: auto;
    }

    #sidebar .extension-sidebar-title {
        height: auto;
        padding: 0 0 0 1;
    }

    #sidebar .extension-sidebar-body {
        padding: 1 0 0 1;
    }

    #sidebar-brand {
        height: auto;
        color: $tau-prompt-text;
    }

    TauTuiApp.-hide-sidebar #sidebar {
        display: none;
    }

    TauTuiApp.-hide-sidebar #main-pane {
        padding-left: 1;
    }

    TauTuiApp.-sidebar-right #sidebar {
        dock: right;
    }

    #main-pane {
        width: 1fr;
        padding: 1 1 0 1;
    }

    #transcript {
        height: 1fr;
        border: none;
        background: $tau-transcript-background;
        padding: 0 0 0 2;
        overflow-x: auto;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 1;
    }

    /* Component seam: generic extension mount points.

       组件接口：通用扩展挂载点。 */
    #main-slot {
        display: none;
        height: 1fr;
        border: none;
        background: $tau-transcript-background;
        padding: 0 0 0 2;
        overflow-x: auto;
        scrollbar-size-vertical: 0;
        scrollbar-size-horizontal: 1;
    }

    #sidebar-file-editor {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }

    #sidebar-file-editor-title {
        height: 1;
        color: $tau-accent;
        text-style: bold;
    }

    #sidebar-file-editor-path,
    #sidebar-file-editor-help,
    #sidebar-file-editor-status {
        height: 1;
        color: $tau-muted-text;
    }

    #sidebar-file-editor-path {
        margin-bottom: 1;
    }

    #sidebar-file-editor-input {
        height: 1fr;
        background: $tau-prompt-background;
        color: $tau-prompt-text;
        border: tall $tau-prompt-border;
    }

    #above-prompt-slot {
        height: auto;
        max-height: 8;
        margin: 0 1 0 1;
        padding: 0;
        background: $tau-screen-background;
    }

    #below-prompt-slot {
        height: auto;
        max-height: 8;
        margin: 0 1 0 1;
        padding: 0;
        background: $tau-screen-background;
    }

    #queued-messages {
        height: auto;
        max-height: 8;
        margin: 0 1 1 1;
        padding: 0 1;
        background: $tau-screen-background;
        color: $tau-muted-text;
    }

    #prompt-row {
        height: auto;
        margin: 0 1 1 1;
    }

    #prompt-prefix {
        width: 2;
        height: 3;
        padding: 0 0 0 0;
        margin: 0;
        content-align: center middle;
        color: $tau-accent;
        text-style: bold;
    }

    #prompt {
        width: 1fr;
        height: auto;
        background: $tau-prompt-background;
        color: $tau-prompt-text;
        border: none;
        border-left: tall transparent;
        margin: 0;
        padding: 1 1;
        max-height: 8;
    }

    #prompt:focus {
        border-left: tall $tau-prompt-border;
    }

    #prompt.-shell-mode {
        border-left: tall $tau-tool-running;
    }

    #compact-session-info {
        height: auto;
        max-height: 3;
        margin: 0 1 1 1;
        padding: 0 1;
        color: $tau-muted-text;
    }

    #autocomplete {
        height: auto;
        max-height: 18;
        margin: 0 1 1 1;
        padding: 0 1;
        background: $tau-autocomplete-background;
        color: $tau-screen-text;
        border: tall $tau-border;
        overflow-y: auto;
    }

    SessionPickerScreen,
    PromptTemplatePickerScreen,
    PromptTemplateEditorScreen,
    SkillPickerScreen,
    TreePickerScreen,
    ToolsReferenceScreen,
    CommandOutputScreen {
        align: center middle;
    }

    #session-picker,
    #prompt-template-picker,
    #prompt-template-editor,
    #skill-picker,
    #tree-picker,
    #tools-reference {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $tau-chrome-background;
        border: tall $tau-border;
    }

    #session-picker-title,
    #prompt-template-picker-title,
    #prompt-template-editor-title,
    #skill-picker-title,
    #tree-picker-title,
    #tools-reference-title {
        height: 1;
        color: $tau-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #session-picker-search,
    #prompt-template-picker-search,
    #skill-picker-search,
    #tools-reference-search {
        height: 3;
        margin-bottom: 1;
        background: $tau-prompt-background;
        color: $tau-prompt-text;
        border: tall $tau-prompt-border;
    }

    #tools-reference-header {
        height: 1;
        color: $tau-muted-text;
        text-style: bold;
    }

    #prompt-template-editor {
        height: 80%;
    }

    #prompt-template-editor-path {
        height: 1;
        margin-bottom: 1;
        color: $tau-muted-text;
    }

    #prompt-template-editor-input {
        height: 1fr;
        background: $tau-prompt-background;
        color: $tau-prompt-text;
        border: tall $tau-prompt-border;
    }

    #session-picker-list,
    #prompt-template-picker-list,
    #skill-picker-list,
    #tree-picker-list,
    #tools-reference-list {
        height: auto;
        max-height: 16;
        background: $tau-transcript-background;
        border: tall $tau-border;
    }

    ListView,
    OptionList {
        scrollbar-background: $tau-transcript-background;
        scrollbar-color: $tau-border;
        scrollbar-color-hover: $tau-highlight-background;
        scrollbar-background-hover: $tau-transcript-background;
        scrollbar-color-active: $tau-accent;
        scrollbar-background-active: $tau-transcript-background;
        scrollbar-size-vertical: 2;
    }

    ListView > ListItem.-highlight {
        background: $tau-highlight-background;
        color: $tau-highlight-text;
    }

    ListView > ListItem.-highlight Label {
        background: $tau-highlight-background;
        color: $tau-highlight-text;
    }

    OptionList > .option-list--option-highlighted {
        background: $tau-highlight-background;
        color: $tau-highlight-text;
    }

    #skill-picker-list .skill-picker-row {
        height: 1;
    }

    #skill-picker-list .skill-picker-name {
        width: 35%;
        text-style: bold;
    }

    #skill-picker-list .skill-picker-description {
        width: 65%;
        color: $tau-muted-text;
    }

    #skill-picker-list ListItem.-highlight .skill-picker-description {
        color: $tau-highlight-text;
    }

    #session-picker-help,
    #prompt-template-picker-help,
    #prompt-template-editor-help,
    #skill-picker-help,
    #tree-picker-help,
    #tools-reference-help {
        height: 1;
        margin-top: 1;
        color: $tau-muted-text;
    }

    #tree-picker-help {
        height: auto;
    }

    ExtensionSelectScreen,
    ExtensionConfirmScreen,
    ExtensionInputScreen,
    LocalBackendPickerScreen,
    LocalBackendScreen,
    LocalChoiceConfirmScreen,
    LocalConfigureScreen,
    LocalConfirmScreen,
    LocalModelActionScreen,
    LocalSearchResultsScreen,
    ProjectTrustScreen {
        align: center middle;
    }

    ProjectTrustScreen {
        background: $tau-screen-background 60%;
    }

    #extension-select,
    #extension-confirm,
    #extension-input {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $tau-chrome-background;
        border: tall $tau-border;
    }

    #extension-select-title,
    #extension-confirm-title,
    #extension-input-title {
        height: auto;
        color: $tau-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #extension-confirm-message {
        height: auto;
        color: $tau-chrome-text;
        margin-bottom: 1;
    }

    #extension-select-list,
    #extension-confirm-list {
        height: auto;
        max-height: 16;
        background: $tau-transcript-background;
        border: tall $tau-border;
    }

    #extension-input-field {
        background: $tau-transcript-background;
        border: tall $tau-border;
    }

    #extension-select-help,
    #extension-confirm-help,
    #extension-input-help {
        height: 1;
        margin-top: 1;
        color: $tau-muted-text;
    }

    #local-backend-picker,
    #local-backend-screen,
    #local-configure-screen,
    #local-confirm-screen,
    #local-model-action-screen,
    #local-search-results-screen {
        width: 82;
        max-width: 92%;
        height: auto;
        max-height: 82%;
        padding: 1 2;
        background: $tau-chrome-background;
        border: tall $tau-border;
    }

    #local-backend-picker-title,
    #local-backend-title,
    #local-configure-title,
    #local-confirm-title,
    #local-model-action-title,
    #local-search-results-title {
        height: auto;
        color: $tau-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #local-backend-picker-help,
    #local-backend-help,
    #local-configure-screen Label,
    #local-confirm-message,
    #local-search-results-help {
        color: $tau-muted-text;
    }

    #local-backend-list,
    #local-backend-status,
    #local-backend-progress,
    #local-model-list,
    #local-action-menu,
    #local-confirm-list,
    #local-choice-list,
    #local-search-results-list,
    #local-configure-screen Input,
    #local-configure-screen Select,
    #local-model-action-input {
        background: $tau-transcript-background;
        border: tall $tau-border;
        margin-top: 1;
    }

    #local-backend-list,
    #local-model-list,
    #local-action-menu,
    #local-confirm-list,
    #local-choice-list,
    #local-search-results-list {
        height: auto;
        max-height: 16;
    }

    #local-model-list,
    #local-action-menu {
        max-height: 10;
    }

    #local-model-list:focus,
    #local-action-menu:focus {
        border: tall $tau-accent;
    }

    #local-model-list.local-section-inactive > ListItem.-highlight,
    #local-action-menu.local-section-inactive > ListItem.-highlight,
    #local-model-list.local-section-inactive > ListItem.-highlight Label,
    #local-action-menu.local-section-inactive > ListItem.-highlight Label {
        background: $tau-transcript-background;
        color: $tau-chrome-text;
    }

    #local-model-section-title,
    #local-action-section-title {
        height: 1;
        margin-top: 1;
        color: $tau-chrome-text;
        text-style: bold;
    }

    #local-backend-progress-bar {
        width: 100%;
        margin-top: 1;
    }

    #local-backend-progress-bar Bar {
        width: 1fr;
    }

    #local-backend-progress-bar Bar > .bar--bar,
    #local-backend-progress-bar Bar > .bar--complete,
    #local-backend-progress-bar Bar > .bar--indeterminate {
        color: $tau-accent;
        background: $tau-border;
    }

    #local-backend-picker-footer,
    #local-backend-footer,
    #local-configure-footer,
    #local-confirm-footer,
    #local-model-action-footer,
    #local-search-results-footer {
        height: 1;
        margin-top: 1;
        color: $tau-muted-text;
    }

    #local-backend-progress {
        min-height: 1;
        color: $tau-muted-text;
    }

    #project-trust-dialog {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $tau-chrome-background;
        color: $tau-chrome-text;
        border: tall $tau-border;
    }

    #project-trust-title {
        color: $tau-chrome-text;
    }

    #project-trust-path-label,
    #project-trust-summary-label,
    #project-trust-boundary,
    #project-trust-help {
        color: $tau-muted-text;
    }

    #project-trust-list {
        background: $tau-transcript-background;
        color: $tau-screen-text;
        border: tall $tau-border;
    }

    #project-trust-list ListItem.-highlight,
    #project-trust-list ListItem.-highlight Label {
        background: $tau-highlight-background;
        color: $tau-highlight-text;
    }

    #command-output {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $tau-chrome-background;
        color: $tau-chrome-text;
        border: tall $tau-border;
    }

    #command-output-title {
        height: 1;
        color: $tau-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #command-output-scroll {
        height: auto;
        max-height: 18;
        background: $tau-transcript-background;
        border: tall $tau-border;
    }

    #command-output-body {
        color: $tau-screen-text;
        padding: 1;
    }

    #command-output-help {
        height: 1;
        margin-top: 1;
        color: $tau-muted-text;
    }

    LoginMethodPickerScreen,
    LoginProviderPickerScreen,
    ThemePickerScreen,
    ModelPickerScreen {
        align: center middle;
    }

    #login-method-picker,
    #login-provider-picker,
    #theme-picker,
    #model-picker {
        width: 76;
        max-width: 90%;
        height: auto;
        max-height: 70%;
        padding: 1 2;
        background: $tau-chrome-background;
        color: $tau-chrome-text;
        border: tall $tau-border;
    }

    #login-method-title,
    #login-provider-title,
    #theme-picker-title,
    #model-picker-title {
        height: 1;
        color: $tau-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #model-picker-tabs {
        height: 1;
        color: $tau-muted-text;
        margin-bottom: 1;
    }

    #login-method-list,
    #login-provider-list,
    #theme-picker-list,
    #model-picker-list {
        height: auto;
        max-height: 12;
        background: $tau-transcript-background;
        color: $tau-screen-text;
        border: tall $tau-border;
    }

    #login-method-list ListItem Label,
    #login-provider-list ListItem Label,
    #theme-picker-list ListItem Label,
    #model-picker-list ListItem Label {
        color: $tau-screen-text;
    }

    #login-method-list ListItem.-highlight Label,
    #login-provider-list ListItem.-highlight Label,
    #theme-picker-list ListItem.-highlight Label,
    #model-picker-list ListItem.-highlight Label {
        background: $tau-highlight-background;
        color: $tau-highlight-text;
    }

    #login-method-intro {
        height: 1;
        color: $tau-muted-text;
        margin-bottom: 1;
    }

    #login-method-list {
        height: auto;
        max-height: 10;
    }

    #login-provider-search,
    #model-picker-search {
        height: 3;
        margin-bottom: 1;
        background: $tau-prompt-background;
        color: $tau-prompt-text;
        border: tall $tau-prompt-border;
    }

    #login-method-help,
    #login-provider-help,
    #theme-picker-help,
    #model-picker-help {
        height: 1;
        margin-top: 1;
        color: $tau-muted-text;
    }

    CustomProviderLoginScreen,
    LoginScreen,
    OAuthLoginScreen {
        align: center middle;
    }

    #login-screen {
        width: 72;
        max-width: 92%;
        height: auto;
        /* A wrapped authorization URL makes this dialog taller than a short
           terminal. Cap it at the screen and scroll instead of overflowing:
           overflowing centers the excess, which pushes the paste field and
           the footer off the bottom and the title off the top. */

        /* 换行后的授权 URL 会使此对话框高于较矮的终端。将其高度限制在
           屏幕内并通过滚动显示，避免溢出内容居中后把粘贴字段和页脚挤出
           底部、把标题挤出顶部。 */
        max-height: 100%;
        overflow-y: auto;
        padding: 1 2;
        background: $tau-chrome-background;
        border: tall $tau-border;
    }

    #login-title {
        height: 1;
        color: $tau-chrome-text;
        text-style: bold;
        margin-bottom: 1;
    }

    #login-help,
    #custom-provider-help {
        height: 1;
        color: $tau-muted-text;
        margin-bottom: 1;
    }

    #login-api-key,
    #login-oauth-code,
    #custom-provider-name,
    #custom-provider-display-name,
    #custom-provider-base-url,
    #custom-provider-api-key-env,
    #custom-provider-models,
    #custom-provider-default-model,
    #custom-provider-api-key {
        background: $tau-prompt-background;
        color: $tau-prompt-text;
        border: tall $tau-prompt-border;
        margin-bottom: 1;
    }

    #login-oauth-url {
        /* Authorization URLs run ~470 chars (Anthropic); clipping them means a
           user copying the URL out of the TUI loses the trailing query
           parameters and the provider rejects the request. Keep every line. */

        /* 授权 URL 约有 470 个字符（Anthropic）；若截断，用户从 TUI 复制
           URL 时会丢失末尾查询参数，导致提供商拒绝请求，因此保留每一行。 */
        min-height: 1;
        height: auto;
        color: $tau-chrome-text;
        margin-bottom: 1;
    }

    #login-footer {
        height: 1;
        color: $tau-muted-text;
    }
    """
    BINDINGS: ClassVar[list[BindingEntry]] = []

    # 初始化会话状态、主题、扩展组件注册表、后台工作器和终端通知控制器。
    def __init__(
        self,
        session: CodingSession,
        *,
        tui_settings: TuiSettings | None = None,
        startup_message: str | None = None,
        startup_notice: str | None = None,
        startup_update_notice: str | None = None,
        startup_alerts: Sequence[str] = (),
        startup_notices: Sequence[str] = (),
        initial_prompt: str | None = None,
    ) -> None:
        self.tui_settings = tui_settings or TuiSettings()
        self.startup_message = startup_message
        legacy_notices = (startup_notice,) if startup_notice else ()
        self.startup_notices = tuple((*startup_notices, *legacy_notices))
        self.initial_prompt = initial_prompt
        # This override is deliberately separate from durable settings. It is
        # reset with every app instance and never participates in tui.json.

        # 此覆盖值特意与持久设置分离；每个应用实例都会重置它，且永远不会
        # 写入 tui.json。
        self._sidebar_visibility_override: bool | None = None
        super().__init__()
        self._register_tau_textual_themes()
        # Assign the resolved theme's name: it is always registered, while the
        # raw settings value may name a custom theme that failed to load. The
        # guard keeps the watcher from persisting the fallback over the user's
        # configured theme.

        # 使用解析后主题的名称：它始终已注册，而原始设置值可能指向加载失败的
        # 自定义主题。此保护标志可防止监听器用回退主题覆盖用户配置的主题。
        self._applying_settings_theme = True
        self.theme = self.tui_settings.resolved_theme.name
        self._applying_settings_theme = False
        self._bindings = BindingsMap(_app_bindings(self.tui_settings.keybindings))
        self.session = session
        self.state = TuiState(skills=session.skills)
        if startup_update_notice is not None:
            self.state.add_item("status", startup_update_notice, highlight="update")
        for alert in startup_alerts:
            self.state.add_item("status", alert, highlight="alert")
        for notice in self.startup_notices:
            self.state.add_item("status", notice)
        if self.tui_settings.theme != self.tui_settings.resolved_theme.name:
            self.state.add_item(
                "status",
                f"Theme '{self.tui_settings.theme}' was not found; "
                f"using {self.tui_settings.resolved_theme.name}.",
            )
        self._prompt_history: tuple[str, ...] = ()
        self._load_session_messages_from_session()
        self.adapter = TuiEventAdapter(self.state)
        # Component seam: host-owned tracking of extension
        # widgets so a reload/rebind can force-clear them and a crash can
        # quarantine them. Must exist before _connect_extension_runtime, which
        # clears them on every bind.
        # `_extension_slot_widgets` holds the *intended* widget per key (the swap
        # target, set synchronously); `_extension_slot_mounted` tracks what is
        # actually mounted. A deferred remove() must fully drain before the next
        # mount of the same-id widget, so slot/main-view swaps run on a serialized
        # async continuation (see `_reconcile_slot`/`_reconcile_main_view`).

        # 组件接口：由宿主跟踪扩展组件，以便重载或重新绑定时强制清除，并在
        # 崩溃时隔离。它必须先于 _connect_extension_runtime 建立，后者每次
        # 绑定都会清除这些组件。`_extension_slot_widgets` 保存每个键对应的
        # 预期组件（同步设置的交换目标），`_extension_slot_mounted` 跟踪实际
        # 已挂载组件。延迟的 remove() 必须完全结束后才能挂载下一个同 ID
        # 组件，因此槽位和主视图交换通过串行异步延续执行（参见
        # `_reconcile_slot` 和 `_reconcile_main_view`）。
        self._extension_slot_widgets: dict[str, Widget] = {}
        self._extension_slot_mounted: dict[str, Widget] = {}
        self._extension_slot_slot_ids: dict[str, str] = {}
        self._extension_slot_locks: dict[str, asyncio.Lock] = {}
        self._extension_key_interceptors: list[KeyInterceptor] = []
        self._extension_sidebar_contributions: dict[tuple[str, str], _SidebarContribution] = {}
        self._extension_sidebar_widgets: dict[tuple[str, str], Widget] = {}
        self._extension_sidebar_mounted: dict[tuple[str, str], Widget] = {}
        self._extension_sidebar_lock = asyncio.Lock()
        self._extension_sidebar_theme: TuiTheme | None = None
        self._extension_main_view: _MainViewHandle | None = None
        self._extension_main_view_mounted: Widget | None = None
        self._extension_main_view_lock = asyncio.Lock()
        self._extension_swap_tasks: set[asyncio.Task[None]] = set()
        self._extension_component_failures_reported: set[str] = set()
        self._connect_extension_runtime(session)
        self._prompt_worker: Worker[None] | None = None
        self._compaction_worker: Worker[None] | None = None
        self._compacting = False
        self._compaction_run_id = 0
        self._prompt_run_id = 0
        self._optimistic_user_messages: list[tuple[int, str]] = []
        self._completion_state = CompletionState()
        self._completion_visible_line_budget: int | None = None
        self._activity_frame = 0
        self._activity_timer: Timer | None = None
        self._last_tool_timer_refresh_at = 0.0
        self._last_activity_indicator_key: tuple[object, ...] | None = None
        self._last_queue_render_key: tuple[object, ...] | None = None
        self._terminal_title = TerminalTitleController()
        self._terminal_notification = TerminalNotificationController(
            self.tui_settings.turn_notification
        )
        self._app_has_focus = True
        self._active_notification_keys: set[tuple[str, str]] = set()
        self._supports_pyperclip: bool | None = None
        self._sync_session_title()

    async def prompt_project_trust(self, request: ProjectTrustRequest) -> TrustChoice | None:
        """Resolve a trust request through the active Textual modal stack.

        通过当前 Textual 模态栈处理项目信任请求。
        """
        return await self.push_screen_wait(ProjectTrustScreen(request))

    def _sync_session_title(self) -> None:
        """Reflect the active session name in the terminal tab title.

        在终端标签标题中显示当前会话名称。
        """
        self._sync_terminal_title()

    def _is_working(self) -> bool:
        """Return whether the app should show working affordances (agent turn or compaction).

        返回应用是否应显示工作状态提示（智能体轮次或压缩正在运行）。
        """
        return self.state.running or self._compacting

    def _sync_terminal_title(self) -> None:
        """Reflect the active session name and running state in the terminal tab title.

        在终端标签标题中显示当前会话名称和运行状态。
        """
        self._terminal_title.update(
            getattr(self.session, "session_title", None),
            running=self._is_working(),
            frame=self._activity_frame,
        )

    def _sync_text_selection_state(self) -> None:
        """Disable native text selection while the transcript is mutating.

        对话记录变化期间禁用原生文本选择。
        """
        type(self).ALLOW_SELECT = not self.state.running
        if self.state.running and self.screen_stack:
            with suppress(Exception):
                self.screen.clear_selection()

    def copy_to_clipboard(self, text: str) -> None:
        """Copy text using pyperclip when available, then Textual's fallback.

        可用时使用 pyperclip 复制文本，随后仍调用 Textual 的回退实现。
        """
        if self._supports_pyperclip is None:
            try:
                import pyperclip  # type: ignore[import-untyped]
            except ImportError:
                self._supports_pyperclip = False
            else:
                self._supports_pyperclip = True
        if self._supports_pyperclip:
            import pyperclip

            with suppress(Exception):
                pyperclip.copy(text)
        super().copy_to_clipboard(text)

    def _register_tau_textual_themes(self) -> None:
        """Register Tau themes with Textual's theme system.

        向 Textual 的主题系统注册 Tau 主题。

        Textual exposes its own theme menu and command palette entries. Registering
        Tau's built-in themes there makes those controls update the same theme as
        `/theme` instead of changing only Textual's chrome.

        Textual 提供自己的主题菜单和命令面板入口。在其中注册 Tau 内置主题后，
        这些控件会与 `/theme` 更新同一主题，而不是只改变 Textual 外观。
        """
        self._registered_themes.clear()
        for theme_name in available_tui_theme_names():
            self.register_theme(_textual_theme_for_tau_theme(theme_name))

    def _reload_session_themes(self) -> None:
        """Rebind custom themes to the active session's accepted trust snapshot.

        按当前会话已接受的信任快照重新绑定自定义主题。
        """
        theme_dirs = getattr(self.session, "theme_dirs", None)
        if theme_dirs is None:
            trust_resolution = getattr(self.session, "project_trust_resolution", None)
            trusted = trust_resolution is None or trust_resolution.trusted
            theme_dirs = TauResourcePaths(
                cwd=self.session.cwd,
                project_resources_enabled=trusted,
            ).themes_dirs
        try:
            custom_themes, diagnostics = load_custom_tui_themes(theme_dirs)
        except (OSError, RuntimeError) as exc:
            # Theme discovery must fail closed after a trust/cwd transition:
            # never retain themes from the previous project snapshot.

            # 信任状态或工作目录变化后，主题发现必须采用失败关闭策略：
            # 绝不能保留上一项目快照中的主题。
            custom_themes = {}
            diagnostics = []
            self._notify(f"Could not reload custom themes: {exc}", severity="error")

        set_custom_tui_themes(custom_themes)
        self._register_tau_textual_themes()
        resolved_theme = self.tui_settings.resolved_theme.name
        self._applying_settings_theme = True
        try:
            if self.theme == resolved_theme:
                # Re-apply CSS when a same-named custom theme changed in place.

                # 同名自定义主题原位变化时重新应用 CSS。
                self._watch_theme(resolved_theme)
            else:
                self.theme = resolved_theme
        finally:
            self._applying_settings_theme = False
        for diagnostic in diagnostics:
            severity: Literal["information", "warning", "error"] = (
                "error"
                if diagnostic.severity == "error"
                else "warning"
                if diagnostic.severity == "warning"
                else "information"
            )
            self._notify(diagnostic.format(), severity=severity)

    def _watch_theme(self, theme_name: str) -> None:
        """Keep Textual theme changes synchronized with Tau's durable TUI theme.

        使 Textual 主题变化与 Tau 的持久 TUI 主题保持同步。
        """
        super()._watch_theme(theme_name)
        if theme_name not in available_tui_theme_names():
            return
        if getattr(self, "_applying_settings_theme", False):
            return
        tau_theme: TuiThemeName = theme_name
        if self.tui_settings.theme == tau_theme:
            return
        self._replace_tui_settings(theme=tau_theme)
        save_tui_settings(self.tui_settings)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        """Return Tau-specific CSS variables for the selected TUI theme.

        返回所选 TUI 主题对应的 Tau 专用 CSS 变量。
        """
        variables = super().get_theme_variable_defaults()
        return {**variables, **_theme_css_variables(self.tui_settings.resolved_theme)}

    def compose(self) -> ComposeResult:
        """Compose the TUI widgets.

        组合 TUI 组件树。
        """
        with Horizontal(id="workspace"):
            yield SessionSidebar(id="sidebar")
            with Vertical(id="main-pane"):
                yield TranscriptView(
                    id="transcript",
                    min_width=1,
                    wrap=True,
                    highlight=True,
                    markup=False,
                )
                # Component seam: host-managed mount points for
                # extension widgets. Empty until an extension mounts into them.

                # 组件接口：由宿主管理的扩展组件挂载点，在扩展挂载前保持为空。
                yield Container(id="main-slot")
                yield Container(id="above-prompt-slot")
                yield Static("", id="queued-messages")
                with Horizontal(id="prompt-row"):
                    yield Static("τ", id="prompt-prefix")
                    yield PromptInput(
                        placeholder=(
                            "Ask Tau…  Enter submits, "
                            f"{_key_hint(self.tui_settings.keybindings.insert_newline)} "
                            "inserts a newline"
                        ),
                        id="prompt",
                        tui_keybindings=self.tui_settings.keybindings,
                    )
                yield CompactSessionInfo(id="compact-session-info")
                yield Static("", id="autocomplete")
                yield Container(id="below-prompt-slot")

    async def on_mount(self) -> None:
        """Focus the prompt when the app starts.

        应用启动时聚焦提示词输入框。
        """
        prompt = self.query_one(PromptInput)
        prompt.shell_mode_style = self.tui_settings.resolved_theme.role_styles["tool"].border
        self._sync_prompt_shell_mode(prompt.text)
        prompt.focus()
        self._update_responsive_layout(self.size.width, self.size.height)
        self._apply_sidebar_position()
        self._refresh()
        self._sync_text_selection_state()
        self._refresh_completions()
        if self.startup_message:
            self._notify(self.startup_message, severity="warning")
        # UI is live and the bridge is installed (__init__) — release the
        # deferred session_start so handlers can notify / open dialogs.

        # UI 已就绪且桥接已在 __init__ 中安装，因此释放延迟的 session_start，
        # 让处理器可以发送通知或打开对话框。
        await self.session.emit_pending_session_start()
        if self.initial_prompt and self.initial_prompt.strip():
            await self._submit_prompt(self.initial_prompt.strip())

    async def on_event(self, event: events.Event) -> None:
        """Consult extension key interceptors before Textual's dispatch.

        在 Textual 分发之前调用扩展按键拦截器。

        Ports Pi's ``onTerminalInput``: a registered interceptor sees a key at
        the earliest point in key processing — before tau's app-level priority
        bindings (``down``/``up``/``tab``/``alt+enter`` in ``_app_bindings``)
        and before the focused widget. Textual's ``App.on_event`` runs
        ``_check_bindings(key, priority=True)`` ahead of forwarding a key to the
        focused widget, so a focused extension widget would otherwise never
        receive those keys; this pre-dispatch hook is the only place an
        extension can own them.

        Interceptors are consulted only on the main screen (never while a modal
        dialog/picker sits on the screen stack) and only when at least one is
        registered, so the default path is untouched. Interceptors therefore
        see EVERY main-screen key regardless of focus and must self-gate.

        The hard interrupt/exit keys in
        :data:`RESERVED_EXTENSION_INTERCEPTOR_KEYS` are skipped entirely, so
        they always reach normal dispatch even behind a misbehaving interceptor.

        这移植了 Pi 的 ``onTerminalInput``：注册的拦截器会在按键处理的最早
        阶段看到按键，即早于 Tau 应用级优先绑定和聚焦组件。Textual 会先运行
        优先绑定，再把按键转给聚焦组件，因此预分发钩子是扩展接管这些按键的
        唯一位置。

        仅在主屏幕且至少注册一个拦截器时调用它们，模态对话框或选择器存在时
        不调用，所以默认路径不受影响。拦截器会看到主屏幕上的每个按键，必须
        自行限制处理范围。``RESERVED_EXTENSION_INTERCEPTOR_KEYS`` 中的硬中断
        和退出键会被完全跳过，即使拦截器行为异常也始终进入正常分发。
        """
        if (
            isinstance(event, events.Key)
            and not event.is_forwarded
            and event.key not in RESERVED_EXTENSION_INTERCEPTOR_KEYS
            and self._extension_key_interceptors
            and len(self.screen_stack) <= 1
            and self._run_extension_key_interceptors(event, self._current_prompt_text())
        ):
            event.stop()
            event.prevent_default()
            return
        await super().on_event(event)

    def on_unmount(self) -> None:
        """Stop activity animations and drop extension widgets on teardown.

        应用卸载时停止活动动画并移除扩展组件。
        """
        if self._activity_timer is not None:
            self._activity_timer.stop()
            self._activity_timer = None
        self._terminal_title.restore()
        self._clear_extension_components()

    def on_app_blur(self) -> None:
        """Remember that terminal attention should be requested when the run settles.

        记录应用已失焦，以便运行结束时请求终端注意。
        """
        self._app_has_focus = False

    def on_app_focus(self) -> None:
        """Suppress turn notifications while the Tau terminal surface is active.

        Tau 终端界面处于活动状态时抑制轮次通知。
        """
        self._app_has_focus = True

    def on_paste(self, event: events.Paste) -> None:
        """Route pastes that arrive while no widget holds keyboard focus.

        转发在没有组件持有键盘焦点时到达的粘贴事件。

        Textual clears widget focus whenever the terminal reports lost focus
        (``CSI ? 1004 h``), and pastes are dropped when nothing is focused. OS
        drag-and-drop from sources that never hand focus back to the terminal --
        notably the macOS Dock -- delivers the dropped paths in exactly that
        state, so the paste bubbles up here instead of reaching the prompt.
        Clipboard pastes always require terminal focus, so this only reroutes
        drops that would otherwise be silently discarded.

        终端报告失焦时，Textual 会清除组件焦点；没有焦点时粘贴会被丢弃。
        某些操作系统拖放来源（尤其是 macOS Dock）不会把焦点交还终端，路径
        正是在这种状态下冒泡到这里。剪贴板粘贴始终需要终端焦点，因此这里只
        转发本会被静默丢弃的拖放内容。
        """
        if self.focused is not None:
            return
        try:
            prompt = self.screen.query_one("#prompt", PromptInput)
        except NoMatches:
            # A modal screen owns the input; leave its own handling alone.

            # 模态屏幕拥有输入控制权，保留其自身处理逻辑。
            return
        event.stop()
        prompt.insert_pasted_text(event.text)

    def on_resize(self, event: Resize) -> None:
        """Update responsive chrome when the terminal changes size.

        终端尺寸变化时更新响应式界面外框。
        """
        self._completion_visible_line_budget = None
        self._update_responsive_layout(event.size.width, event.size.height)

    @on(SidebarFileItem.OpenRequested)
    def on_sidebar_file_open_requested(self, event: SidebarFileItem.OpenRequested) -> None:
        """Open a sidebar resource file in the main-area editor.

        在主区域编辑器中打开侧栏资源文件。
        """
        event.stop()
        item = event.item
        current = self._extension_main_view
        if (
            current is not None
            and isinstance(current.widget, SidebarFileEditor)
            and current.widget.is_dirty
        ):
            message = "Save or close the current file before opening another."
            self._notify(message, severity="warning")
            current.widget.query_one("#sidebar-file-editor-status", Static).update(message)
            current.widget.query_one("#sidebar-file-editor-input", TextArea).focus()
            return
        try:
            source, snapshot = _read_sidebar_file(item.path)
        except (OSError, UnicodeDecodeError) as exc:
            self._notify(f"Could not read {item.path}: {exc}", severity="error")
            return
        self._open_extension_main_view(
            lambda handle, theme: SidebarFileEditor(
                handle=handle,
                path=item.path,
                label=item.file_label,
                kind=item.kind,
                source=source,
                snapshot=snapshot,
            )
        )

    def on_click(self, event: events.Click) -> None:
        """Return keyboard focus to the prompt after clicks in the main TUI.

        点击主 TUI 后把键盘焦点还给提示词输入框。
        """
        if event.button != 1:
            return
        if self._extension_main_view is not None:
            # An extension main view (e.g. a subagent conversation viewer) owns
            # the main area and its keyboard; yanking focus back to the prompt
            # would silently reroute every key — esc, toggles, typed text — to
            # the main chat. Clicking the prompt itself still focuses it via
            # Textual's native mouse-down focus.

            # 扩展主视图（例如子智能体对话查看器）拥有主区域及键盘；强行把
            # 焦点拉回提示词会把 Esc、切换键和输入文本等所有按键静默转发到
            # 主聊天。点击提示词本身仍会通过 Textual 原生鼠标按下行为聚焦。
            return
        with suppress(NoMatches):
            self.screen.query_one("#prompt", PromptInput).focus()

    @on(events.TextSelected)
    async def on_text_selected(self) -> None:
        """Optionally copy selected text automatically.

        根据设置自动复制选中的文本。
        """
        active_screen = self.screen
        if not (
            self.tui_settings.auto_copy_selection
            or getattr(active_screen, "auto_copy_selection", False)
        ):
            return
        selection = active_screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
            self._notify("Copied selection to clipboard.")

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Update prompt autocomplete when the prompt text changes.

        提示词文本变化时更新自动补全。
        """
        if event.text_area.id != "prompt":
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.sync_pending_paste()
        # Read text and cursor from the widget so both come from one snapshot.

        # 从组件读取文本和光标，使两者来自同一状态快照。
        text = prompt.text
        self._sync_prompt_shell_mode(text)
        self._completion_state = self._build_completion_state(text, cursor=prompt.cursor_position)
        self._refresh_completions()

    def on_text_area_selection_changed(self, event: TextArea.SelectionChanged) -> None:
        """Close prompt autocomplete when the caret leaves the completed token.

        插入光标离开当前补全令牌时关闭提示词自动补全。
        """
        if event.text_area.id != "prompt":
            return
        # Edits post SelectionChanged before Changed; check after Changed has rebuilt.

        # 编辑操作会先发送 SelectionChanged 再发送 Changed；在 Changed 重建后检查。
        self.call_later(self._close_completions_if_caret_left_token)

    # 当光标离开补全令牌范围时清空补全状态并刷新显示。
    def _close_completions_if_caret_left_token(self) -> None:
        if not self._completion_state.items:
            return
        item = self._completion_state.items[0]
        cursor = self.query_one("#prompt", PromptInput).cursor_position
        if item.start < cursor <= item.end:
            return
        self._completion_state = CompletionState()
        self._refresh_completions()

    async def action_submit_prompt(self) -> None:
        """Accept a changing non-file completion, or submit the current prompt text.

        接受正在变化的非文件补全，或提交当前提示词文本。
        """
        selected = self._completion_state.selected
        if selected is not None and selected.kind is not CompletionKind.FILE_REFERENCE:
            prompt = self.query_one("#prompt", PromptInput)
            text_before_completion = prompt.text
            self.action_accept_completion()
            if prompt.text != text_before_completion:
                return
        await self._submit_prompt_from_editor(streaming_behavior="steer")

    async def action_submit_follow_up(self) -> None:
        """Submit the current prompt as a queued follow-up while running.

        运行期间把当前提示词作为排队的后续消息提交。
        """
        await self._submit_prompt_from_editor(streaming_behavior="follow_up")

    # 校验并清空编辑器内容，再按当前运行状态执行命令、排队或直接提交。
    async def _submit_prompt_from_editor(
        self,
        *,
        streaming_behavior: Literal["steer", "follow_up"],
    ) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        raw_text = prompt.text_for_submission()

        text = raw_text.strip()
        if not text:
            prompt.text = ""
            prompt._clear_pending_paste()
            self._completion_state = CompletionState()
            self._refresh_completions()
            return

        if self._is_compaction_active():
            if text.startswith("/compact"):
                self._notify("A compaction is already running.", severity="warning")
            else:
                prompt.text = raw_text
                prompt.move_cursor(_text_end_location(raw_text))
                self._notify(
                    "Compaction is still running. You can keep editing, but wait to submit.",
                    severity="warning",
                )
            return

        prompt.text = ""
        prompt._clear_pending_paste()
        self._completion_state = CompletionState()
        self._refresh_completions()

        terminal_command = parse_terminal_command(text)
        if terminal_command is not None:
            self.run_worker(
                self._run_terminal_command(
                    terminal_command.command,
                    add_to_context=terminal_command.add_to_context,
                ),
                group="terminal-command",
                exclusive=True,
            )
            return

        command = self.session.handle_command(text)
        if command.handled:
            if command.clear_requested:
                self.state.clear()
            if command.reload_requested:
                try:
                    summary = await self.session.reload()
                except ValueError as exc:
                    command = replace(command, message=f"Could not reload: {exc}")
                else:
                    self._reload_session_themes()
                    command = replace(command, message=format_reload_summary(summary))
            if command.new_session_requested:
                await self._new_session()
            if command.compact_summary is not None:
                if self._is_compaction_active():
                    self._notify("A compaction is already running.", severity="warning")
                elif self._is_agent_or_queue_active():
                    prompt.text = raw_text
                    prompt.move_cursor(_text_end_location(raw_text))
                    self._notify(
                        "Wait for the current agent turn and queued messages to finish "
                        "before compacting.",
                        severity="warning",
                    )
                    return
                else:
                    self._compaction_worker = self.run_worker(
                        self._run_compaction(command.compact_summary),
                        exclusive=False,
                    )
            if command.export_requested:
                try:
                    exported_path = await self.session.export(
                        command.export_destination,
                        format=command.export_format,
                    )
                    self._append_command_message(
                        text,
                        f"Exported session to {exported_path}",
                    )
                except Exception as exc:  # noqa: BLE001 - surface command failures in the TUI

                    # noqa: BLE001 - 在 TUI 中显示命令失败。
                    self._notify(f"Could not export session: {exc}", severity="error")
            if command.resume_session_id is not None:
                await self._resume_session(command.resume_session_id)
            if command.resume_picker_requested:
                self.action_open_session_picker()
            if command.prompts_picker_requested:
                self._open_prompt_template_picker()
            if command.tree_picker_requested:
                if self._is_agent_or_queue_active():
                    prompt.text = raw_text
                    prompt.move_cursor(_text_end_location(raw_text))
                    self._notify(TREE_RUNNING_MESSAGE, severity="warning")
                    return
                await self._open_tree_picker()
            if command.login_picker_requested:
                self._open_login_picker()
            if command.custom_provider_login_requested:
                self._open_custom_provider_login()
            if command.local_requested:
                self._open_local_backend_picker()
            if command.sidebar_toggle_requested:
                self._toggle_sidebar_visibility()
            if command.login_provider is not None:
                self._open_login(command.login_provider, method=command.login_method)
            if command.logout_picker_requested:
                self._open_logout_picker()
            if command.logout_provider is not None:
                self._logout(command.logout_provider)
            if command.model_selection_model is not None:
                self.run_worker(
                    self._switch_model(
                        ModelChoice(
                            provider_name=command.model_selection_provider
                            or self.session.provider_name,
                            model=command.model_selection_model,
                        )
                    ),
                    exclusive=False,
                )
            if command.model_picker_requested:
                self._open_model_picker()
            if command.tools_picker_requested:
                self._open_tools_reference()
            if command.scoped_models_picker_requested:
                self._open_scoped_models_picker()
            if command.skills_picker_requested:
                self._open_skills_picker()
            if command.theme_picker_requested:
                self._open_theme_picker()
            if command.session_name is not None:
                try:
                    await self.session.set_session_name(command.session_name)
                except ValueError as exc:
                    self._notify(f"Could not rename session: {exc}", severity="error")
                    self._refresh()
                    return
                self._sync_session_title()
            if command.thinking_level is not None:
                await self._set_thinking_level(command.thinking_level)
            if command.theme is not None:
                self._set_tui_theme(command.theme)
            self.state.set_skills(self.session.skills)
            if command.message:
                if _command_message_uses_notification(text, command.message):
                    self._notify(command.message)
                elif _command_message_uses_transcript(text):
                    self._append_command_message(
                        text,
                        command.message,
                        system_prompt_inspection=command.system_prompt_inspection,
                    )
                else:
                    self._show_command_message(text, command.message)
            self._refresh()
            if command.exit_requested:
                self.exit()
            return

        if self.state.running:
            self._remember_prompt(text)
            await self._queue_prompt(text, streaming_behavior=streaming_behavior)
            return

        self._remember_prompt(text)
        await self._submit_prompt(text)

    def _remember_prompt(self, text: str) -> None:
        """Remember a submitted user prompt for lightweight input recall.

        记住已提交的用户提示词，以便进行轻量输入召回。
        """
        if not text.strip():
            return
        self._prompt_history = (*self._prompt_history, text)

    def _load_session_messages_from_session(self) -> None:
        """Load visible session messages and reseed prompt history from them.

        加载可见会话消息，并用其中的用户消息重新填充提示词历史。
        """
        self.state.load_messages(self.session.messages)
        self._prompt_history = tuple(
            message.text
            for message in self.session.messages
            if isinstance(message, UserMessage) and message.text.strip()
        )

    def _is_compaction_active(self) -> bool:
        """Return whether a manual compaction worker is still running.

        返回手动压缩工作器是否仍在运行。
        """
        worker = self._compaction_worker
        if worker is not None and not worker.is_finished and not worker.is_cancelled:
            return True
        return self._compacting

    def _is_agent_or_queue_active(self) -> bool:
        """Return whether compaction would race an active or queued agent turn.

        返回压缩是否会与活动中或已排队的智能体轮次发生竞争。
        """
        self._sync_queue_state()
        worker = self._prompt_worker
        is_worker_active = worker is not None and not worker.is_finished and not worker.is_cancelled
        is_session_running = bool(getattr(self.session, "is_running", False))
        return (
            self.state.running
            or is_session_running
            or is_worker_active
            or self.state.queued_message_count > 0
        )

    async def _run_compaction(self, summary: str) -> None:
        """Run manual compaction without disabling prompt editing.

        在不禁用提示词编辑的情况下运行手动压缩。
        """
        self._compaction_run_id += 1
        run_id = self._compaction_run_id
        self._compacting = True
        try:
            self.state.clear()
            self.state.add_item("status", "Compacting session…")
            self._refresh()
            compact_message = await self.session.compact(summary)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - surface command failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示命令失败。
            self._notify(f"Error: {exc}", severity="error")
            return
        finally:
            # A cancelled run can tear down after a newer compaction started, so only
            # clear working state this run still owns.

            # 被取消的运行可能在新压缩启动后才清理，因此只清除仍由本次运行
            # 持有的工作状态。
            if self._compaction_run_id == run_id:
                self._compacting = False
                self._compaction_worker = None
                self._refresh_chrome_if_mounted()
        self.state.clear()
        self.state.set_skills(self.session.skills)
        self._load_session_messages_from_session()
        self._notify(compact_message)
        self._refresh()
        if not self._app_has_focus:
            self._terminal_notification.notify_turn_finished()

    async def _submit_prompt(
        self,
        text: str,
        *,
        source: Literal["interactive", "extension"] = "interactive",
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Add a prompt to the transcript and start the agent worker.

        将提示词加入对话记录并启动智能体工作器。
        """
        self._prompt_run_id += 1
        run_id = self._prompt_run_id
        # Custom messages are never rendered optimistically: the optimistic
        # dedupe matches on exact content equality with the post-expansion
        # event (see _consume_optimistic_user_event), and a mismatch would
        # double-render. They render once, from the confirmed user event,
        # which carries their custom_type/details.

        # 自定义消息从不进行乐观渲染：乐观去重依靠与扩展后事件内容完全相等
        # （参见 _consume_optimistic_user_event），不匹配会造成重复渲染。
        # 它们只根据带 custom_type/details 的已确认用户事件渲染一次。
        if custom_type is None and _should_optimistically_render_prompt(text):
            self._optimistic_user_messages.append((run_id, text))
            await self._append_optimistic_user_message(text)
        self._prompt_worker = self.run_worker(
            self._run_prompt(text, run_id, source=source, custom_type=custom_type, details=details),
            exclusive=True,
        )

    async def _append_optimistic_user_message(
        self,
        text: str,
        *,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Render a submitted user message immediately without rebuilding the transcript.

        立即渲染已提交的用户消息，而不重建整个对话记录。
        """
        start_index = len(self.state.items)
        self.state.add_user_message(text, custom_type=custom_type, details=details)
        self._follow_transcript_output()
        if not self.screen_stack:
            self._refresh()
            return
        theme = self.tui_settings.resolved_theme
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            self._refresh()
            return
        for item in self.state.items[start_index:]:
            await transcript.append_item(
                item,
                theme=theme,
                show_tool_results=self.state.show_tool_results,
                scroll_end=True,
                custom_markup=self.state.resolve_custom_markup(
                    item, expanded=self.state.show_tool_results
                ),
            )
        self._refresh_chrome(theme=theme)

    def _consume_optimistic_user_event(self, event: CodingSessionEvent, *, run_id: int) -> bool:
        """Return whether a user event confirms an already-rendered optimistic message.

        返回用户事件是否确认了一条已乐观渲染的消息。
        """
        if not isinstance(event, MessageEndEvent) or not isinstance(event.message, UserMessage):
            return False
        for index, (pending_run_id, pending_text) in enumerate(self._optimistic_user_messages):
            if pending_run_id == run_id and pending_text == event.message.content:
                del self._optimistic_user_messages[index]
                return True
        return False

    def _replace_transformed_optimistic_user_message(
        self, event: CodingSessionEvent, *, run_id: int
    ) -> bool:
        """Reconcile a transformed prompt with its optimistic render.

        将经过转换的提示词与其乐观渲染结果进行协调。

        An extension `input` hook may transform the submitted text inside
        session.prompt, so the confirmed UserMessage no longer matches the
        optimistically rendered original (the exact-equality path above).
        Rewrite the optimistic item in place and redraw, instead of letting
        the confirmed event append a second user item alongside the stale
        original. Runs after _consume_optimistic_user_event, so it only fires
        when this run's pending optimistic text mismatches — the run's own
        prompt confirmation is the first user event of the run, so a queued
        steering/follow-up user message can never be mistaken for it.

        扩展的 `input` 钩子可能在 session.prompt 内转换提交文本，使确认后的
        UserMessage 不再匹配乐观渲染的原文。这里原位改写乐观项目并重绘，
        避免确认事件在过期原文旁再追加一个用户项目。此方法在精确匹配消费
        之后运行，仅处理当前运行的不匹配原文；本轮提示词确认是首个用户事件，
        因此不会误认排队的引导或后续消息。
        """
        if not isinstance(event, MessageEndEvent) or not isinstance(event.message, UserMessage):
            return False
        for index, (pending_run_id, pending_text) in enumerate(self._optimistic_user_messages):
            if pending_run_id != run_id:
                continue
            del self._optimistic_user_messages[index]
            for item in reversed(self.state.items):
                if item.role == "user" and item.text == pending_text:
                    item.text = event.message.text
                    break
            self._refresh()
            self._sync_session_title()
            return True
        return False

    def _clear_optimistic_user_messages(self, *, run_id: int) -> None:
        """Drop unconfirmed optimistic messages once their run is no longer active.

        运行不再活动后丢弃其尚未确认的乐观消息。
        """
        self._optimistic_user_messages = [
            pending for pending in self._optimistic_user_messages if pending[0] != run_id
        ]

    async def _append_confirmed_user_message(self, message: AgentMessage) -> None:
        """Render a non-optimistic user/custom event incrementally when possible.

        尽可能增量渲染非乐观的用户或自定义事件。
        """
        if isinstance(message, UserMessage):
            await self._append_optimistic_user_message(message.text)
            return
        if isinstance(message, CustomMessage):
            if message.display:
                await self._append_optimistic_user_message(
                    message.text,
                    custom_type=message.custom_type,
                    details=message.details if isinstance(message.details, dict) else None,
                )
            return
        self._refresh()

    def _connect_extension_runtime(self, session: CodingSession) -> None:
        """Give the extension runtime a UI bridge and an idle-run entry point.

        为扩展运行时提供 UI 桥接和空闲运行入口。
        """
        runtime = getattr(session, "extension_runtime", None)
        if runtime is None:
            return
        # Force-clear any extension widgets before installing the new bridge.
        # This runs once, at construction; later teardowns (/reload, resume,
        # new) come through the installed bridge's clear_components(), driven
        # by the runtime, so extension widgets and key interceptors never
        # survive a world they were mounted in.

        # 安装新桥接前强制清除所有扩展组件。构造时执行一次；后续因 /reload、
        # 恢复或新建会话触发的拆卸由运行时通过 clear_components() 驱动，确保
        # 扩展组件和按键拦截器不会存活到其挂载环境之外。
        self._clear_extension_components()
        runtime.set_ui_bridge(_TuiExtensionUiBridge(self))
        runtime.set_turn_requested_callback(self._on_extension_turn_requested)
        # Let the transcript render custom messages via registered renderers.

        # 让对话记录通过已注册渲染器显示自定义消息。
        self.state.custom_renderer = runtime.render_custom_message
        # Let tool calls render through their tool's render_call, if any.

        # 若工具提供 render_call，则用它渲染工具调用。
        self.state.tool_call_renderer = runtime.render_tool_call
        # And tool results through their tool's render_result, if any.

        # 若工具提供 render_result，则用它渲染工具结果。
        self.state.tool_result_renderer = runtime.render_tool_result

    def _on_extension_turn_requested(
        self,
        content: str,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Deliver an extension message through the serialized prompt path.

        通过串行化提示词路径投递扩展消息。
        """
        self.call_later(self._deliver_extension_message, content, custom_type, details)

    # 等待当前提示词运行完成，再把扩展消息交给统一提交路径。
    async def _deliver_extension_message(
        self,
        content: str,
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        if self.session.is_running or self._prompt_worker is not None:
            # A run started while the delivery was in flight; drain with it.

            # 投递期间若启动了运行，则等待它一并完成。
            queue_follow_up = getattr(self.session, "queue_follow_up_message", None)
            if callable(queue_follow_up):
                queue_follow_up(content, custom_type=custom_type, details=details)
            return
        await self._submit_prompt(
            content, source="extension", custom_type=custom_type, details=details
        )

    # -- component seam ------------------------------------------------------

    # -- 组件接口 ------------------------------------------------------------

    def _current_prompt_text(self) -> str:
        """Return the prompt-editor text, or "" before the prompt exists.

        返回提示词编辑器文本；提示词组件尚不存在时返回空字符串。
        """
        try:
            return self.query_one("#prompt", PromptInput).text
        except NoMatches:
            return ""

    def _register_extension_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:
        """Register a pre-dispatch key interceptor; return an unsubscribe fn.

        注册预分发按键拦截器，并返回取消订阅函数。
        """
        self._extension_key_interceptors.append(handler)

        # 从应用的拦截器列表中移除本处理器。
        def unsubscribe() -> None:
            with suppress(ValueError):
                self._extension_key_interceptors.remove(handler)

        return unsubscribe

    def _run_extension_key_interceptors(self, event: Key, text: str) -> bool:
        """Consult interceptors; return True if one consumed the key.

        调用各拦截器；任一拦截器消费按键时返回 True。

        Each call is guarded: a raising interceptor is diagnosed once and
        treated as "not consumed", so a broken interceptor degrades to normal
        typing rather than a dead prompt.

        每次调用都受到保护：抛出异常的拦截器只诊断一次并视为“未消费”，
        使损坏的拦截器退化为正常输入，而不是让提示词失去响应。
        """
        for interceptor in tuple(self._extension_key_interceptors):
            try:
                if interceptor(event, text):
                    return True
            except Exception as exc:  # noqa: BLE001 - isolation boundary

                # noqa: BLE001 - 此处是扩展故障的隔离边界。
                # Notify like the other failure classes so a broken interceptor
                # is not silently invisible, and dedup per-interceptor so a
                # second faulty handler still gets diagnosed.

                # 像其他故障类别一样发出通知，避免损坏的拦截器被静默忽略；
                # 按拦截器去重，确保第二个故障处理器仍会得到诊断。
                self._record_extension_component_failure(
                    f"key_interceptor:{id(interceptor)}", exc, notify=True
                )
        return False

    def _schedule_extension_swap(self, coro: Coroutine[object, object, None]) -> None:
        """Run a slot/main-view reconcile coroutine on the app loop.

        在应用事件循环上运行槽位或主视图协调协程。

        The task is retained until it finishes so it cannot be garbage-collected
        mid-flight (asyncio only holds a weak reference). If there is no running
        loop (only possible outside a live TUI), the coroutine is closed rather
        than left un-awaited.

        任务会保留到结束，避免在运行中被垃圾回收（asyncio 只持有弱引用）。
        若没有运行中的事件循环（只可能发生在活动 TUI 之外），则关闭协程，
        避免留下未等待协程。
        """
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:  # no running loop — not a live TUI
            coro.close()
            return
        self._extension_swap_tasks.add(task)
        task.add_done_callback(self._extension_swap_tasks.discard)

    @staticmethod
    def _string_slot_widget(lines: Sequence[str]) -> Static:
        """Build a slot ``Static`` from display lines (Rich markup, safe fallback).

        根据显示行构建槽位 ``Static``（使用 Rich 标记并提供安全回退）。

        Joins ``lines`` with newlines and parses them as Rich markup; if the
        markup is malformed the literal text is shown instead, mirroring the
        custom-message renderer's guard so a bad string never crashes the TUI.

        使用换行连接 ``lines`` 并按 Rich 标记解析；若标记格式错误，则显示
        字面文本，与自定义消息渲染器的保护一致，避免错误字符串导致 TUI 崩溃。
        """
        content = "\n".join(lines)
        return Static(_custom_markup_to_text(content))

    def _set_extension_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        placement: Placement,
    ) -> None:
        """Mount an extension widget into a prompt-adjacent slot, or unmount it.

        将扩展组件挂载到提示词相邻槽位，或将其卸载。

        ``content`` is a ``factory(theme)`` callable, a list of display lines
        the host renders into a ``Static``, or ``None`` to unmount. The string
        form is normalized into a factory here so the reconcile/quarantine/
        replace machinery below is untouched.

        The intended widget is recorded synchronously so mid-swap reads (clear,
        quarantine, refresh) see what *should* occupy the slot; the actual
        mount/unmount runs on a serialized continuation so a deferred remove()
        of a same-id widget fully drains before the replacement mounts (else the
        DOM briefly holds two widgets with one id -> ``DuplicateIds``).

        ``content`` 可以是 ``factory(theme)``、由宿主渲染为 ``Static`` 的显示
        行列表，或表示卸载的 ``None``。字符串形式在此规范化为工厂，使后续
        协调、隔离和替换机制保持一致。

        预期组件会同步记录，使交换过程中的清除、隔离和刷新能看到槽位应有的
        内容；实际挂载和卸载则通过串行延续执行，确保同 ID 组件的延迟 remove()
        完全结束后才挂载替代项，避免 DOM 短暂出现重复 ID。
        """
        factory: SlotWidgetFactory | None
        if content is None:
            factory = None
        elif callable(content):
            # Check callable() first: a Sequence[str] test must never swallow a
            # factory (and a factory is not a Sequence).

            # 先检查 callable()：Sequence[str] 判断绝不能吞掉工厂，且工厂本身
            # 也不是 Sequence。
            factory = content
        else:
            # A plain list of display lines: build the widget host-side so the
            # extension needs no Textual import. A bare str is treated as one
            # line (never split into characters).

            # 对普通显示行列表，由宿主构建组件，使扩展无需导入 Textual；裸字符串
            # 视为单独一行，绝不拆成字符。
            lines = [content] if isinstance(content, str) else list(content)
            factory = lambda _theme: self._string_slot_widget(lines)  # noqa: E731
        new_widget: Widget | None = None
        if factory is not None:
            try:
                new_widget = factory(self.tui_settings.resolved_theme)
            except Exception as exc:  # noqa: BLE001 - isolation boundary

                # noqa: BLE001 - 此处是扩展故障的隔离边界。
                self._record_extension_component_failure(f"slot:{key}", exc, notify=True)
                return
        slot_id = "above-prompt-slot" if placement == "above_prompt" else "below-prompt-slot"
        if new_widget is None:
            self._extension_slot_widgets.pop(key, None)
            self._extension_slot_slot_ids.pop(key, None)
        else:
            self._extension_slot_widgets[key] = new_widget
            self._extension_slot_slot_ids[key] = slot_id
        self._schedule_extension_swap(self._reconcile_slot(key))

    async def _reconcile_slot(self, key: str) -> None:
        """Make the mounted slot widget match the intended target (serialized).

        以串行方式使已挂载槽位组件与预期目标一致。

        Reads the *live* target each time (not a snapshot), so a burst of set
        calls collapses to "last writer wins": the first continuation removes the
        stale mount, later ones find the target already satisfied and no-op.

        每次都读取实时目标而不是快照，使连续设置遵循“最后写入者生效”：首个
        延续移除过期挂载，后续延续发现目标已满足后直接返回。
        """
        lock = self._extension_slot_locks.setdefault(key, asyncio.Lock())
        async with lock:
            target = self._extension_slot_widgets.get(key)
            mounted = self._extension_slot_mounted.get(key)
            if mounted is not None and mounted is not target:
                with suppress(Exception):
                    await mounted.remove()
                if self._extension_slot_mounted.get(key) is mounted:
                    self._extension_slot_mounted.pop(key, None)
            # Re-read after the await; the target may have changed meanwhile.

            # 等待后重新读取，因为目标可能已在此期间变化。
            target = self._extension_slot_widgets.get(key)
            if target is None or self._extension_slot_mounted.get(key) is target:
                return
            slot_id = self._extension_slot_slot_ids.get(key, "below-prompt-slot")
            try:
                self.query_one(f"#{slot_id}", Container).mount(target)
            except Exception as exc:  # noqa: BLE001 - isolation boundary

                # noqa: BLE001 - 此处是扩展故障的隔离边界。
                if self._extension_slot_widgets.get(key) is target:
                    self._extension_slot_widgets.pop(key, None)
                    self._extension_slot_slot_ids.pop(key, None)
                self._record_extension_component_failure(f"slot:{key}", exc, notify=True)
                return
            self._extension_slot_mounted[key] = target

    def _build_extension_sidebar_widget(
        self,
        owner: tuple[str, str],
        contribution: _SidebarContribution,
        *,
        theme: TuiTheme,
    ) -> Widget | None:
        """Build one host-framed sidebar section, isolating its body factory.

        构建由宿主提供外框的侧栏区段，并隔离正文工厂异常。
        """
        content = contribution.content
        try:
            if callable(content):
                body = content(theme)
            else:
                lines = [content] if isinstance(content, str) else list(content)
                body = self._string_slot_widget(lines)
            if not isinstance(body, Widget):
                raise TypeError("sidebar factory must return a Textual Widget")
            header = Text(contribution.title, style=f"bold {theme.prompt_text}")
            return Vertical(
                Static(_sidebar_separator(theme=theme), classes="sidebar-separator"),
                Static(header, classes="extension-sidebar-title"),
                Container(body, classes="extension-sidebar-body"),
                classes="extension-sidebar-section",
            )
        except Exception as exc:  # noqa: BLE001 - isolation boundary

            # noqa: BLE001 - 此处是扩展故障的隔离边界。
            extension_name, key = owner
            self._record_extension_component_failure(
                f"sidebar:{extension_name}:{key}",
                exc,
                notify=True,
                extension_name=extension_name,
            )
            return None

    def _set_extension_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:
        """Add or update a sidebar contribution while preserving key order.

        在保留键顺序的同时添加或更新侧栏贡献。
        """
        if self.tui_settings.sidebar_position == "off":
            return
        owner = (extension_name, key)
        normalized_content: SidebarContent
        if callable(content):
            normalized_content = content
        elif isinstance(content, str):
            normalized_content = (content,)
        else:
            normalized_content = tuple(content)
        contribution = _SidebarContribution(title=title, content=normalized_content)
        theme = self.tui_settings.resolved_theme
        previous = self._extension_sidebar_contributions.get(owner)
        if previous == contribution and self._extension_sidebar_theme == theme:
            return
        mounted = self._extension_sidebar_mounted.get(owner)
        target = self._extension_sidebar_widgets.get(owner)
        if (
            previous is not None
            and not callable(previous.content)
            and not callable(normalized_content)
            and mounted is not None
            and mounted is target
            and self._extension_sidebar_theme == theme
        ):
            try:
                header = Text(title, style=f"bold {theme.prompt_text}")
                mounted.query_one(".extension-sidebar-title", Static).update(header)
                body = mounted.query_one(".extension-sidebar-body", Container).query_one(Static)
                body.update(_custom_markup_to_text("\n".join(normalized_content)))
            except Exception as exc:  # noqa: BLE001 - isolation boundary

                # noqa: BLE001 - 此处是扩展故障的隔离边界。
                self._record_extension_component_failure(
                    f"sidebar:{extension_name}:{key}",
                    exc,
                    notify=True,
                    extension_name=extension_name,
                )
                return
            self._extension_sidebar_contributions[owner] = contribution
            return
        widget = self._build_extension_sidebar_widget(owner, contribution, theme=theme)
        if widget is None:
            return
        self._extension_sidebar_contributions[owner] = contribution
        self._extension_sidebar_widgets[owner] = widget
        self._extension_sidebar_theme = theme
        self._schedule_extension_swap(self._reconcile_sidebar())

    def _remove_extension_sidebar_section(self, extension_name: str, key: str) -> None:
        """Forget and unmount one extension-owned sidebar contribution.

        移除记录并卸载一个由扩展拥有的侧栏贡献。
        """
        owner = (extension_name, key)
        if owner not in self._extension_sidebar_contributions:
            return
        self._extension_sidebar_contributions.pop(owner, None)
        self._extension_sidebar_widgets.pop(owner, None)
        self._schedule_extension_swap(self._reconcile_sidebar())

    def _rebuild_extension_sidebar_sections(self, *, theme: TuiTheme) -> None:
        """Recreate sidebar factories for a changed live theme.

        当前主题变化时重新创建侧栏工厂组件。
        """
        if self.tui_settings.sidebar_position == "off":
            return
        changed = False
        for owner, contribution in tuple(self._extension_sidebar_contributions.items()):
            widget = self._build_extension_sidebar_widget(owner, contribution, theme=theme)
            if widget is not None:
                self._extension_sidebar_widgets[owner] = widget
                changed = True
        self._extension_sidebar_theme = theme
        if changed:
            self._schedule_extension_swap(self._reconcile_sidebar())

    async def _reconcile_sidebar(self) -> None:
        """Mount sidebar sections in registration order after removals drain.

        等待移除完成后，按注册顺序挂载侧栏区段。
        """
        async with self._extension_sidebar_lock:
            target_items = tuple(self._extension_sidebar_widgets.items())
            mounted_items = tuple(self._extension_sidebar_mounted.items())
            if mounted_items == target_items:
                return
            try:
                slot = self.query_one("#sidebar-extension-sections", Container)
            except NoMatches:
                return
            # Remove only stale roots. An unchanged Textual widget cannot be
            # removed and mounted again: removal prunes its composed children.
            # Keeping unchanged roots also avoids rerunning unrelated factories.

            # 仅移除过期根组件。未变化的 Textual 组件不能先移除再挂载，因为移除
            # 会裁剪其组合子组件；保留它们也避免重新运行无关工厂。
            for owner, mounted in mounted_items:
                if self._extension_sidebar_widgets.get(owner) is mounted:
                    continue
                with suppress(Exception):
                    await mounted.remove()
                if self._extension_sidebar_mounted.get(owner) is mounted:
                    self._extension_sidebar_mounted.pop(owner, None)
            # Re-read after awaits: rapid updates collapse to the latest target.

            # 等待后重新读取，使快速更新合并为最新目标。
            target_items = tuple(self._extension_sidebar_widgets.items())
            for index, (owner, target) in enumerate(target_items):
                if self._extension_sidebar_mounted.get(owner) is target:
                    continue
                later_mounted = next(
                    (
                        self._extension_sidebar_mounted.get(later_owner)
                        for later_owner, _ in target_items[index + 1 :]
                        if self._extension_sidebar_mounted.get(later_owner) is not None
                    ),
                    None,
                )
                try:
                    await slot.mount(target, before=later_mounted)
                except Exception as exc:  # noqa: BLE001 - isolation boundary

                    # noqa: BLE001 - 此处是扩展故障的隔离边界。
                    extension_name, key = owner
                    if self._extension_sidebar_widgets.get(owner) is target:
                        self._extension_sidebar_widgets.pop(owner, None)
                        self._extension_sidebar_contributions.pop(owner, None)
                    self._record_extension_component_failure(
                        f"sidebar:{extension_name}:{key}",
                        exc,
                        notify=True,
                        extension_name=extension_name,
                    )
                    continue
                self._extension_sidebar_mounted[owner] = target
            self._extension_sidebar_mounted = {
                owner: target
                for owner, target in target_items
                if self._extension_sidebar_mounted.get(owner) is target
            }

    def _open_extension_main_view(self, factory: MainViewFactory) -> MainViewHandle:
        """Open a display-toggled main-area view mounting ``factory(handle, theme)``.

        打开通过显示状态切换的主区域视图，并挂载 ``factory(handle, theme)``。

        Prompt focus is intentionally left where it is (the extension widget can
        focus its own composer), so a registered key interceptor keeps firing
        while the prompt is focused and can close the view on Esc.

        The handle is returned synchronously (the factory needs it and callers
        store it at once), but the mount is sequenced after any previous view's
        remove() drains, so switching views never collides on the shared main
        slot. ``is_open`` reports the *intended* state: the new handle is open
        the instant it is returned even though its widget mounts a tick later.

        提示词焦点特意保持原位，使扩展组件可聚焦自己的编辑器；提示词仍聚焦时，
        已注册按键拦截器仍可响应，并用 Esc 关闭视图。

        句柄会同步返回，因为工厂和调用方立即需要它；但挂载会等上一视图的
        remove() 完成后按序执行，避免共享主槽位冲突。``is_open`` 表示预期状态：
        新句柄返回时即视为打开，即使其组件要到下一次事件循环才完成挂载。
        """
        handle = _MainViewHandle(self, asyncio.get_event_loop().create_future())
        try:
            widget = factory(handle, self.tui_settings.resolved_theme)
        except Exception as exc:  # noqa: BLE001 - isolation boundary

            # noqa: BLE001 - 此处是扩展故障的隔离边界。
            self._record_extension_component_failure("main_view", exc, notify=True)
            # Nothing awaits this handle (the extension gets the dead one), but
            # resolve it anyway so no future leaks unresolved.

            # 没有代码会等待此句柄（扩展得到失效句柄），但仍解析它，避免未来对象
            # 永久悬而未决。
            handle._resolve(None)
            return _DeadMainViewHandle()
        handle.widget = widget
        previous = self._extension_main_view
        if previous is not None and previous is not handle:
            # Superseded (last writer wins): its close() becomes a no-op so a
            # stale Esc/unmount can't tear down the view that replaced it, and
            # its pending wait() resolves with None.

            # 被替代时采用最后写入者生效：旧句柄的 close() 变为空操作，避免过期
            # Esc 或卸载拆掉替代视图，同时其待处理 wait() 以 None 结束。
            self._release_main_view_handle(previous)
        self._extension_main_view = handle
        self._schedule_extension_swap(self._reconcile_main_view())
        return handle

    async def _reconcile_main_view(self) -> None:
        """Make the mounted main view match the intended handle (serialized).

        以串行方式使已挂载主视图与预期句柄一致。
        """
        async with self._extension_main_view_lock:
            target = self._extension_main_view
            target_widget = target.widget if target is not None else None
            mounted = self._extension_main_view_mounted
            if mounted is not None and mounted is not target_widget:
                with suppress(Exception):
                    await mounted.remove()
                if self._extension_main_view_mounted is mounted:
                    self._extension_main_view_mounted = None
            # Re-read after the await.

            # 等待后重新读取目标状态。
            target = self._extension_main_view
            target_widget = target.widget if target is not None else None
            if target_widget is not None and self._extension_main_view_mounted is not target_widget:
                try:
                    slot = self.query_one("#main-slot", Container)
                    slot.mount(target_widget)
                except Exception as exc:  # noqa: BLE001 - isolation boundary

                    # noqa: BLE001 - 此处是扩展故障的隔离边界。
                    if self._extension_main_view is target:
                        self._extension_main_view = None
                    self._release_main_view_handle(target)
                    self._record_extension_component_failure("main_view", exc, notify=True)
                    self._restore_main_transcript()
                    return
                self._extension_main_view_mounted = target_widget
                with suppress(NoMatches):
                    self.query_one("#transcript", TranscriptView).display = False
                slot.display = True
            elif self._extension_main_view is None and self._extension_main_view_mounted is None:
                # A close (target cleared) with nothing left to show.

                # 关闭操作已清除目标，且没有剩余内容可显示。
                self._restore_main_transcript()

    def _close_extension_main_view(self, handle: _MainViewHandle) -> None:
        """Unmount a main view and restore the main transcript (sequenced).

        按序卸载主视图并恢复主对话记录。
        """
        if self._extension_main_view is not handle:
            return
        self._extension_main_view = None
        self._schedule_extension_swap(self._reconcile_main_view())

    def _release_main_view_handle(self, handle: _MainViewHandle | None) -> None:
        """Mark a host-torn-down handle closed and resolve its ``wait()`` with None.

        将由宿主拆卸的句柄标记为关闭，并用 None 结束其 ``wait()``。

        Used by the teardown paths the host drives itself (supersede, session
        rebind, mount failure, quarantine) — as opposed to an explicit
        ``handle.close(result)`` from the extension — so a pending ``wait()``
        never leaks unresolved and ``is_open`` reports False.

        此方法用于宿主主动驱动的拆卸路径（替代、会话重绑、挂载失败、隔离），
        区别于扩展显式调用 ``handle.close(result)``；它确保待处理的 ``wait()``
        不会泄漏为未完成状态，并让 ``is_open`` 返回 False。
        """
        if handle is None:
            return
        handle._open = False
        handle._resolve(None)

    def _restore_main_transcript(self) -> None:
        """Hide the main slot and bring the main transcript back into focus.

        隐藏主槽位，并让主对话记录重新获得焦点。
        """
        if not self.screen_stack:
            return
        with suppress(NoMatches):
            self.query_one("#main-slot", Container).display = False
        with suppress(NoMatches):
            pane = self.query_one("#transcript", TranscriptView)
            pane.display = True
            # Re-anchor the restored main transcript so returning to a live
            # conversation lands at the bottom (mirrors _follow_transcript_output).

            # 重新锚定恢复后的主对话记录，使返回实时会话时落在底部；这与
            # _follow_transcript_output 的行为一致。
            pane.follow_output()
        with suppress(NoMatches):
            self.query_one("#prompt", PromptInput).focus()

    def _refresh_extension_components(self) -> None:
        """Re-render all mounted extension widgets (analog of requestRender).

        重新渲染所有已挂载扩展组件，作用类似 requestRender。
        """
        for widget in (
            *self._extension_slot_widgets.values(),
            *self._extension_sidebar_widgets.values(),
        ):
            with suppress(Exception):
                widget.refresh()
        handle = self._extension_main_view
        if handle is not None and handle.widget is not None:
            with suppress(Exception):
                handle.widget.refresh()

    def _clear_extension_components(self) -> None:
        """Force-clear every tracked extension widget, view, and interceptor.

        强制清除所有已跟踪的扩展组件、视图和拦截器。

        The runtime drives this through the UI bridge on `/reload` and session
        rebinds (resume/new); it also runs on app teardown, so a leaked
        extension widget never survives a session switch. Intent is cleared
        synchronously — mid-swap reads and in-flight continuations then see
        empty state — while the actual unmounts run on the same serialized
        per-key reconciles as ordinary swaps, so a clear followed immediately
        by a re-mount of a same-id widget (a session_start handler re-mounting
        after a rebind) can never hold two widgets with one id
        (``DuplicateIds``).

        运行时会在 `/reload` 和会话重绑（恢复或新建）时通过 UI 桥接驱动此
        方法，应用卸载时也会运行，因此泄漏的扩展组件不会跨会话存活。预期
        状态同步清空，使交换中的读取和进行中的延续看到空状态；实际卸载沿用
        普通交换的逐键串行协调，从而保证清除后立即重新挂载同 ID 组件时不会
        同时存在两个同 ID 组件并触发 ``DuplicateIds``。
        """
        slot_keys = {*self._extension_slot_widgets, *self._extension_slot_mounted}
        self._extension_slot_widgets.clear()
        self._extension_slot_slot_ids.clear()
        for key in slot_keys:
            self._schedule_extension_swap(self._reconcile_slot(key))
        self._extension_sidebar_contributions.clear()
        self._extension_sidebar_widgets.clear()
        self._extension_sidebar_theme = None
        self._schedule_extension_swap(self._reconcile_sidebar())
        handle = self._extension_main_view
        self._extension_main_view = None
        self._release_main_view_handle(handle)
        self._schedule_extension_swap(self._reconcile_main_view())
        self._extension_key_interceptors.clear()
        # A recurring failure context must notify again in the new world.

        # 新环境中重复出现的故障上下文必须能够再次发出通知。
        self._extension_component_failures_reported.clear()

    def _tracked_extension_widgets(self) -> tuple[Widget, ...]:
        """Return every extension widget the host currently tracks (intended or mounted).

        返回宿主当前跟踪的全部扩展组件，包括预期组件和已挂载组件。
        """
        widgets: list[Widget] = []
        seen: set[int] = set()
        for widget in (
            *self._extension_slot_widgets.values(),
            *self._extension_slot_mounted.values(),
            *self._extension_sidebar_widgets.values(),
            *self._extension_sidebar_mounted.values(),
        ):
            if id(widget) not in seen:
                seen.add(id(widget))
                widgets.append(widget)
        handle = self._extension_main_view
        main_widgets = (
            handle.widget if handle is not None else None,
            self._extension_main_view_mounted,
        )
        for main_widget in main_widgets:
            if main_widget is not None and id(main_widget) not in seen:
                seen.add(id(main_widget))
                widgets.append(main_widget)
        return tuple(widgets)

    def _extension_root_for(self, widget: Widget, tracked: tuple[Widget, ...]) -> Widget | None:
        """Return the tracked extension root that owns ``widget``, if any.

        返回拥有 ``widget`` 的已跟踪扩展根组件；不存在时返回 None。
        """
        node: Widget | None = widget
        while node is not None:
            for root in tracked:
                if node is root:
                    return root
            node = node.parent if isinstance(node.parent, Widget) else None
        return None

    def _quarantine_extension_widget(self, error: BaseException) -> bool:
        """Remove the tracked extension widget implicated in ``error``.

        移除与 ``error`` 有关的已跟踪扩展组件。

        Returns True when a culprit was found and torn down (so the app can
        swallow the exception and stay alive), False otherwise (so core bugs
        still surface). Textual runs ``render`` on the compositor's own reflow
        loop, so a child's render/compose/on_mount crash cannot be caught at the
        mount site; walking the traceback for a frame owned by a tracked widget
        is the only handle we get.

        找到并拆除故障组件时返回 True，使应用可吞掉异常继续运行；否则返回
        False，让核心错误继续上抛。Textual 在合成器自己的重排循环中执行
        ``render``，因此无法在挂载点捕获子组件的 render、compose 或 on_mount
        崩溃；遍历回溯并寻找属于已跟踪组件的栈帧是唯一可用的定位手段。
        """
        tracked = self._tracked_extension_widgets()
        if not tracked:
            return False
        tb = error.__traceback__
        culprit: Widget | None = None
        while tb is not None:
            candidate = tb.tb_frame.f_locals.get("self")
            if isinstance(candidate, Widget):
                root = self._extension_root_for(candidate, tracked)
                if root is not None:
                    culprit = root
                    break
            tb = tb.tb_next
        if culprit is None:
            return False
        # Suppress the ghost first: a widget that crashed in on_mount never
        # finished mounting, so remove() cannot fully prune it, but hiding and
        # disabling it makes it inert and invisible. A render-crash widget
        # removes cleanly. Either way the app keeps running.

        # 先抑制幽灵组件：在 on_mount 中崩溃的组件从未完成挂载，remove() 无法
        # 完全清理它，但隐藏并禁用后可使其不可见且不再活动。渲染崩溃组件则
        # 可以正常移除；无论哪种情况，应用都继续运行。
        with suppress(Exception):
            culprit.display = False
        with suppress(Exception):
            culprit.disabled = True
        sidebar_owner: tuple[str, str] | None = None
        if (
            self._extension_main_view is not None and self._extension_main_view.widget is culprit
        ) or self._extension_main_view_mounted is culprit:
            if self._extension_main_view_mounted is culprit:
                self._extension_main_view_mounted = None
            handle = self._extension_main_view
            self._extension_main_view = None
            self._release_main_view_handle(handle)
            with suppress(Exception):
                culprit.remove()
            self._restore_main_transcript()
        else:
            sidebar_owner = next(
                (
                    owner
                    for owner, widget in (
                        *self._extension_sidebar_widgets.items(),
                        *self._extension_sidebar_mounted.items(),
                    )
                    if widget is culprit
                ),
                None,
            )
            if sidebar_owner is not None:
                self._extension_sidebar_widgets.pop(sidebar_owner, None)
                self._extension_sidebar_mounted.pop(sidebar_owner, None)
                self._extension_sidebar_contributions.pop(sidebar_owner, None)
            else:
                for tracker in (self._extension_slot_widgets, self._extension_slot_mounted):
                    key = next((k for k, w in tracker.items() if w is culprit), None)
                    if key is not None:
                        tracker.pop(key, None)
            with suppress(Exception):
                culprit.remove()
        extension_name = sidebar_owner[0] if sidebar_owner else None
        context = (
            f"sidebar:{sidebar_owner[0]}:{sidebar_owner[1]}"
            if sidebar_owner
            else f"render:{id(culprit)}"
        )
        self._record_extension_component_failure(
            context,
            error,
            notify=True,
            extension_name=extension_name,
        )
        return True

    def _handle_exception(self, error: Exception) -> None:
        """Quarantine a crashing extension widget instead of tearing down.

        隔离崩溃的扩展组件，而不是拆掉整个应用。

        Overrides Textual's private ``App._handle_exception`` (there is no
        public error hook — ``hasattr(App, "on_exception")`` is False on the
        pinned Textual). If the traceback touches a tracked extension widget we
        remove it and keep running; otherwise we defer to Textual's default so
        core's own bugs still surface. This private-API coupling is a contract
        cost the component-seam experiment deliberately accepts.

        此方法覆盖 Textual 的私有 ``App._handle_exception``，因为固定版本没有
        公共错误钩子。若回溯触及已跟踪扩展组件，则移除它并继续运行；否则交给
        Textual 默认处理，使核心自身错误仍能暴露。这是组件接口实验有意接受的
        私有 API 耦合成本。
        """
        if self._quarantine_extension_widget(error):
            return
        super()._handle_exception(error)

    def _record_extension_component_failure(
        self,
        context: str,
        error: BaseException,
        *,
        notify: bool = False,
        extension_name: str | None = None,
    ) -> None:
        """Diagnose an extension-component failure once per context.

        每个上下文只诊断一次扩展组件故障。

        The notification carries a short exception summary so the failure is
        identifiable at a glance; the full traceback goes to the app log for a
        post-mortem (the two together are what let us pin the deferred-remove
        ``DuplicateIds`` race).

        通知包含简短异常摘要，便于快速识别；完整回溯写入应用日志以供事后分析，
        两者结合可定位延迟移除导致的 ``DuplicateIds`` 竞争。
        """
        # Always log the traceback, even on a duplicate context, so a repeating
        # failure leaves a full trail.

        # 即使上下文重复也始终记录回溯，使反复故障留下完整轨迹。
        with suppress(Exception):
            self.log.error(
                f"Extension component failed ({context}):\n"
                + "".join(traceback.format_exception(type(error), error, error.__traceback__))
            )
        if context in self._extension_component_failures_reported:
            return
        self._extension_component_failures_reported.add(context)
        if extension_name is not None:
            runtime = getattr(self.session, "extension_runtime", None)
            if runtime is not None:
                runtime.record_ui_failure(extension_name, context, error)
        if notify:
            summary = f"{type(error).__name__}: {error}"
            if len(summary) > 120:
                summary = summary[:117] + "..."
            self._notify(
                f"An extension component failed ({context}) and was removed ({summary}).",
                severity="error",
            )

    def _follow_transcript_output(self) -> None:
        """Put the transcript back in follow mode for explicit user actions.

        对明确的用户操作，将对话记录恢复为跟随模式。
        """
        if not self.screen_stack:
            return
        with suppress(NoMatches):
            self.query_one("#transcript", TranscriptView).follow_output()

    # 运行终端命令，立即挂载工具行，并在完成后原位更新结果。
    async def _run_terminal_command(self, command: str, *, add_to_context: bool) -> None:
        run_terminal_command = getattr(self.session, "run_terminal_command", None)
        if not callable(run_terminal_command):
            self._notify("Terminal commands are not available.", severity="error")
            return

        item_index = len(self.state.items)
        self.state.add_item(
            "tool",
            f"$ {command.strip()}",
            always_show_tool_result=True,
        )
        item = self.state.items[item_index]
        self._follow_transcript_output()
        transcript = self.query_one("#transcript", TranscriptView)
        await transcript.append_item(
            item,
            theme=self.tui_settings.resolved_theme,
            show_tool_results=True,
            scroll_end=True,
        )
        self._refresh_chrome()

        try:
            result = await run_terminal_command(command, add_to_context=add_to_context)
        except Exception as exc:  # noqa: BLE001 - surface command execution failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示命令执行失败。
            if item_index < len(self.state.items):
                item = self.state.items[item_index]
                item.tool_result_text = format_terminal_command_result_block(
                    ok=False,
                    added_to_context=add_to_context,
                    output=str(exc),
                )
            self._notify(f"Could not run command: {exc}", severity="error")
            await transcript.update_item(
                item,
                theme=self.tui_settings.resolved_theme,
                show_tool_results=True,
            )
            self._refresh_chrome()
            return

        if item_index >= len(self.state.items):
            return
        item = self.state.items[item_index]
        item.text = f"$ {result.command}"
        item.tool_result_text = format_terminal_command_result_block(
            ok=result.ok,
            added_to_context=result.added_to_context,
            output=result.output,
        )
        self._follow_transcript_output()
        await transcript.update_item(
            item,
            theme=self.tui_settings.resolved_theme,
            show_tool_results=True,
        )
        self._refresh_chrome()

    def _replace_tui_settings(self, *, theme: TuiThemeName) -> None:
        """Replace the current immutable TUI settings with a new theme.

        使用新主题替换当前不可变 TUI 设置。
        """
        self.tui_settings = TuiSettings(
            keybindings=self.tui_settings.keybindings,
            theme=theme,
            auto_copy_selection=self.tui_settings.auto_copy_selection,
            sidebar_position=self.tui_settings.sidebar_position,
            turn_notification=self.tui_settings.turn_notification,
        )

    # 校验、持久化并应用新的 TUI 主题。
    def _set_tui_theme(self, theme: TuiThemeName) -> None:
        if theme not in available_tui_theme_names():
            self._notify(f"Unknown theme: {theme}", severity="error")
            return
        self._replace_tui_settings(theme=theme)
        save_tui_settings(self.tui_settings)
        self.theme = theme
        self._refresh()

    async def _queue_prompt(
        self,
        text: str,
        *,
        streaming_behavior: Literal["steer", "follow_up"],
    ) -> None:
        """Queue a prompt for the active agent worker.

        为当前活动的智能体工作器排队一条提示词。
        """
        try:
            async for event in self.session.prompt(text, streaming_behavior=streaming_behavior):
                self.adapter.apply(event)
        except Exception as exc:  # noqa: BLE001 - surface queueing failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示消息排队失败。
            self._notify(f"Could not queue message: {exc}", severity="error")
            return
        self._refresh_chrome()

    async def _run_prompt(
        self,
        text: str,
        run_id: int | None = None,
        *,
        source: Literal["interactive", "extension"] = "interactive",
        custom_type: str | None = None,
        details: dict[str, JSONValue] | None = None,
    ) -> None:
        """Run one prompt and stream session events into the TUI state.

        运行一条提示词，并把会话事件流式写入 TUI 状态。
        """
        active_run_id = self._prompt_run_id if run_id is None else run_id
        try:
            async for event in self.session.prompt(
                text, source=source, custom_type=custom_type, details=details
            ):
                if active_run_id != self._prompt_run_id:
                    return
                if self._consume_optimistic_user_event(event, run_id=active_run_id):
                    self._sync_text_selection_state()
                    self._refresh_chrome()
                    continue
                if self._replace_transformed_optimistic_user_message(event, run_id=active_run_id):
                    self._sync_text_selection_state()
                    continue
                if not (_is_user_message_end_event(event) and self.screen_stack):
                    self.adapter.apply(event)
                self._sync_text_selection_state()
                if (
                    isinstance(event, MessageEndEvent)
                    and isinstance(event.message, AssistantMessage)
                    and event.message.stop_reason == "error"
                ):
                    _attach_diagnostic_log_path_to_error(self.state, self.session)
                    will_auto_retry = getattr(self.session, "will_auto_retry", None)
                    if not (callable(will_auto_retry) and will_auto_retry(event.message)):
                        _attach_retry_hint_to_error(self.state, event.message)
                elif (
                    isinstance(event, CompactionEndEvent)
                    and event.reason == "overflow"
                    and (event.aborted or event.error_message)
                ):
                    _attach_diagnostic_log_path_to_error(self.state, self.session)
                await self._apply_streaming_transcript_event(event)
                if isinstance(event, AgentSettledEvent) and not self._app_has_focus:
                    self._terminal_notification.notify_turn_finished()
        except Exception as exc:  # noqa: BLE001 - surface unexpected worker errors in the TUI

            # noqa: BLE001 - 在 TUI 中显示工作器的意外错误。
            if active_run_id != self._prompt_run_id:
                return
            message = _format_prompt_error(exc, self.session)
            self.state.error = message
            self.state.add_item("error", message)
            self.state.running = False
            self._sync_text_selection_state()
            self._refresh()
        finally:
            self._clear_optimistic_user_messages(run_id=active_run_id)
            if active_run_id == self._prompt_run_id:
                self._prompt_worker = None

    async def _apply_streaming_transcript_event(self, event: CodingSessionEvent) -> None:
        """Apply an agent event to mounted transcript widgets without full redraws.

        将智能体事件应用到已挂载对话组件，而不进行完整重绘。
        """
        if not self.screen_stack:
            self._refresh()
            return
        theme = self.tui_settings.resolved_theme
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            self._refresh()
            return
        if isinstance(event, AgentStartEvent):
            self._refresh_chrome()
            return
        if isinstance(event, AgentEndEvent):
            await transcript.finish_assistant_message()
            self._refresh_chrome()
            return
        if isinstance(event, MessageStartEvent):
            return
        if isinstance(event, MessageUpdateEvent):
            nested = event.assistant_message_event
            if isinstance(nested, TextDeltaEvent):
                await transcript.append_assistant_delta(nested.delta, theme=theme)
            elif isinstance(nested, ThinkingEndEvent):
                await transcript.finish_thinking_message()
            elif isinstance(nested, ThinkingDeltaEvent):
                await transcript.append_thinking_delta(
                    nested.delta,
                    theme=theme,
                    show_thinking=self.state.show_thinking,
                )
            return
        if isinstance(event, MessageEndEvent):
            if isinstance(event.message, (UserMessage, CustomMessage)):
                await self._append_confirmed_user_message(event.message)
                self._sync_session_title()
                return
            if isinstance(event.message, AssistantMessage):
                if event.message.stop_reason in {"error", "aborted"}:
                    # The adapter projected any partial response plus the error
                    # into canonical display state. Rebuild once at this terminal
                    # boundary so the mounted transcript cannot drop the error.

                    # 适配器已把部分响应和错误投影到规范显示状态；在此终止边界
                    # 重建一次，确保已挂载对话记录不会漏掉错误。
                    self._refresh()
                    return
                visible_blocks = [
                    block
                    for block in event.message.content
                    if (
                        isinstance(block, TextContent)
                        and bool(block.text)
                        or isinstance(block, ThinkingContent)
                        and bool(block.thinking)
                    )
                ]
                canonical_items = self.state.items[-len(visible_blocks) :] if visible_blocks else []
                if (
                    any(isinstance(block, ThinkingContent) for block in visible_blocks)
                    or len(visible_blocks) > 1
                ):
                    # Replace only this message's provisional streaming widgets;
                    # unrelated history remains mounted and selectable.

                    # 仅替换本消息的临时流式组件；无关历史保持挂载且可选择。
                    await transcript.finish_structured_assistant_message(
                        canonical_items,
                        theme=theme,
                        show_thinking=self.state.show_thinking,
                    )
                else:
                    canonical_item = canonical_items[-1] if canonical_items else None
                    await transcript.finish_assistant_message(
                        event.message.text,
                        item=canonical_item,
                    )
                self._refresh_chrome()
                return
            return
        if isinstance(event, ToolExecutionStartEvent):
            await transcript.finish_assistant_message()
            item = self.state.find_tool_item(event.tool_call_id)
            if item is not None:
                expanded = self.state.show_tool_results or item.always_show_tool_result
                updated = await transcript.update_item(
                    item,
                    theme=theme,
                    show_tool_results=expanded,
                    invocation=self.state.resolve_tool_invocation(item, expanded=expanded),
                    result_markup=self.state.resolve_tool_result(item, expanded=expanded),
                )
                if not updated:
                    await transcript.append_item(
                        item,
                        theme=theme,
                        show_tool_results=expanded,
                        invocation=self.state.resolve_tool_invocation(item, expanded=expanded),
                    )
            self._refresh_chrome()
            return
        if isinstance(event, ToolExecutionUpdateEvent):
            await transcript.finish_assistant_message()
            updated_item = self.state.find_tool_item(event.tool_call_id)
            if updated_item is not None:
                expanded = self.state.show_tool_results or updated_item.always_show_tool_result
                await transcript.update_item(
                    updated_item,
                    theme=theme,
                    show_tool_results=expanded,
                    invocation=self.state.resolve_tool_invocation(updated_item, expanded=expanded),
                    result_markup=self.state.resolve_tool_result(updated_item, expanded=expanded),
                )
            self._refresh_chrome()
            return
        if isinstance(event, (AutoRetryStartEvent, CompactionStartEvent)):
            await transcript.finish_assistant_message()
            if self.state.items and (
                isinstance(event, AutoRetryStartEvent) or event.reason == "overflow"
            ):
                await transcript.append_item(
                    self.state.items[-1],
                    theme=theme,
                    show_tool_results=self.state.show_tool_results,
                )
            self._refresh_chrome()
            return
        if isinstance(event, CompactionEndEvent):
            if event.reason == "overflow" and (event.aborted or event.error_message):
                self._refresh()
            else:
                self._refresh_chrome()
            return
        if isinstance(event, ToolExecutionEndEvent):
            updated_item = self.state.find_tool_item(event.tool_call_id)
            if updated_item is not None:
                expanded = self.state.show_tool_results or updated_item.always_show_tool_result
                await transcript.update_item(
                    updated_item,
                    theme=theme,
                    show_tool_results=expanded,
                    invocation=self.state.resolve_tool_invocation(updated_item, expanded=expanded),
                    result_markup=self.state.resolve_tool_result(updated_item, expanded=expanded),
                )
            self._refresh_chrome()
            return
        if isinstance(event, QueueUpdateEvent):
            self._refresh_chrome()
            return
        self._refresh_chrome()

    def action_cancel(self) -> None:
        """Cancel the active compaction or agent turn.

        取消当前活动的压缩或智能体轮次。
        """
        if self._cancel_active_compaction(notify=True):
            return
        self._cancel_active_prompt(notify=True)

    def _cancel_active_compaction(self, *, notify: bool) -> bool:
        """Cancel the active manual compaction worker and restore visible session state.

        取消活动的手动压缩工作器，并恢复可见会话状态。
        """
        worker = self._compaction_worker
        if worker is None or worker.is_finished or worker.is_cancelled:
            return False

        worker.cancel()
        self._compaction_run_id += 1
        self._compaction_worker = None
        self._compacting = False
        self.state.clear()
        self.state.set_skills(self.session.skills)
        self._load_session_messages_from_session()
        self._refresh()
        if notify:
            self._notify("Cancelled compaction.")
        return True

    def _cancel_active_prompt(self, *, notify: bool, interrupt: bool = False) -> None:
        """Cancel the active prompt worker and ignore any late events from it.

        取消活动的提示词工作器，并忽略它随后到达的事件。
        """
        del interrupt
        worker = self._prompt_worker
        is_worker_active = worker is not None and not worker.is_cancelled
        is_session_running = bool(getattr(self.session, "is_running", False))
        if not (self.state.running or is_session_running or is_worker_active):
            return

        self._prompt_run_id += 1
        cancel = getattr(self.session, "cancel", None)
        if callable(cancel):
            cancel()
        if worker is not None and not worker.is_cancelled:
            worker.cancel()
        self._prompt_worker = None
        self.state.running = False
        self.state.assistant_buffer = ""
        self._sync_text_selection_state()
        self._refresh()
        if notify:
            self._notify("Interrupted current operation.")

    def action_accept_completion(self) -> None:
        """Accept the currently selected prompt completion.

        接受当前选中的提示词补全项。
        """
        if isinstance(self.screen, ModelPickerScreen):
            self.screen.action_toggle_mode()
            return
        if isinstance(self.screen, ToolsReferenceScreen):
            self.screen.action_open_selected()
            return
        if isinstance(
            self.screen,
            SessionPickerScreen
            | PromptTemplatePickerScreen
            | SkillPickerScreen
            | TreePickerScreen
            | LoginMethodPickerScreen
            | LoginProviderPickerScreen
            | ThemePickerScreen
            | ExtensionSelectScreen
            | ExtensionConfirmScreen
            | LocalBackendPickerScreen
            | LocalBackendScreen
            | LocalChoiceConfirmScreen
            | LocalConfirmScreen
            | LocalSearchResultsScreen
            | ProjectTrustScreen,
        ):
            self.screen.action_select_cursor()
            return
        prompt = self.query_one("#prompt", PromptInput)
        item = self._completion_state.selected
        applied = self._apply_selected_completion(prompt.text)
        if applied is None or item is None:
            return
        prompt.text = applied
        cursor = item.cursor_after_apply()
        prompt.cursor_position = cursor
        self._completion_state = self._build_completion_state(prompt.text, cursor=cursor)
        self._refresh_completions()

    def action_completion_next(self) -> None:
        """Select the next prompt completion or move down in the active editor.

        选择下一个提示词补全项，或在当前编辑器中向下移动。
        """
        if isinstance(self.focused, TextArea) and self.focused.id == "sidebar-file-editor-input":
            self.focused.action_cursor_down()
            return
        if isinstance(self.screen, PromptTemplateEditorScreen):
            self.screen.query_one("#prompt-template-editor-input", TextArea).action_cursor_down()
            return
        if isinstance(self.screen, CommandOutputScreen):
            self.screen.action_scroll_down()
            return
        if isinstance(
            self.screen,
            SessionPickerScreen
            | PromptTemplatePickerScreen
            | SkillPickerScreen
            | TreePickerScreen
            | LoginMethodPickerScreen
            | LoginProviderPickerScreen
            | ThemePickerScreen
            | ModelPickerScreen
            | ToolsReferenceScreen
            | ExtensionSelectScreen
            | ExtensionConfirmScreen
            | LocalBackendPickerScreen
            | LocalBackendScreen
            | LocalChoiceConfirmScreen
            | LocalConfirmScreen
            | LocalSearchResultsScreen
            | ProjectTrustScreen,
        ):
            self.screen.action_cursor_down()
            return
        if not self._completion_state.items:
            self.query_one("#prompt", PromptInput).action_cursor_down()
            return
        self._completion_state = self._completion_state.select_next()
        self._refresh_completions()

    def action_completion_previous(self) -> None:
        """Select the previous prompt completion or move up in the active editor.

        选择上一个提示词补全项，或在当前编辑器中向上移动。
        """
        if isinstance(self.focused, TextArea) and self.focused.id == "sidebar-file-editor-input":
            self.focused.action_cursor_up()
            return
        if isinstance(self.screen, PromptTemplateEditorScreen):
            self.screen.query_one("#prompt-template-editor-input", TextArea).action_cursor_up()
            return
        if isinstance(self.screen, CommandOutputScreen):
            self.screen.action_scroll_up()
            return
        if isinstance(
            self.screen,
            SessionPickerScreen
            | PromptTemplatePickerScreen
            | SkillPickerScreen
            | TreePickerScreen
            | LoginMethodPickerScreen
            | LoginProviderPickerScreen
            | ThemePickerScreen
            | ModelPickerScreen
            | ToolsReferenceScreen
            | ExtensionSelectScreen
            | ExtensionConfirmScreen
            | LocalBackendPickerScreen
            | LocalBackendScreen
            | LocalChoiceConfirmScreen
            | LocalConfirmScreen
            | LocalSearchResultsScreen
            | ProjectTrustScreen,
        ):
            self.screen.action_cursor_up()
            return
        if not self._completion_state.items:
            if self.action_edit_queued_message():
                return
            if self.action_recall_previous_prompt():
                return
            self.query_one("#prompt", PromptInput).action_cursor_up()
            return
        self._completion_state = self._completion_state.select_previous()
        self._refresh_completions()

    def action_recall_previous_prompt(self) -> bool:
        """Recall the most recent submitted prompt into an empty prompt input.

        将最近提交的提示词召回到空的提示词输入框。
        """
        prompt = self.query_one("#prompt", PromptInput)
        # Only recall into an empty input so an accidental Up press does not
        # erase a prompt the user is still writing.

        # 仅在输入为空时召回，避免误按向上键清除用户尚在编写的提示词。
        if prompt.text.strip() or not self._prompt_history:
            return False
        previous_prompt = self._prompt_history[-1]
        prompt.text = previous_prompt
        prompt.move_cursor(_text_end_location(previous_prompt))
        self._completion_state = self._build_completion_state(prompt.text)
        self._refresh_completions()
        return True

    def action_edit_queued_message(self) -> bool:
        """Move the latest queued message back into the prompt for editing.

        将最新排队消息移回提示词输入框进行编辑。
        """
        if not self.state.running:
            return False
        prompt = self.query_one("#prompt", PromptInput)
        if prompt.text.strip():
            return False

        message = self._pop_latest_queued_message()
        if not message:
            return False
        prompt.text = message
        prompt.move_cursor(_text_end_location(message))
        self._sync_queue_state()
        self._completion_state = self._build_completion_state(prompt.text)
        self._refresh()
        return True

    def action_edit_queued_follow_up(self) -> bool:
        """Move the latest queued message back into the prompt for editing.

        将最新排队消息移回提示词输入框进行编辑。
        """
        return self.action_edit_queued_message()

    def _pop_latest_queued_message(self) -> str | None:
        """Pop the latest queued follow-up or steering message from the session.

        从会话中弹出最新排队的后续或引导消息。
        """
        pop_follow_up = getattr(self.session, "pop_latest_follow_up_message", None)
        if callable(pop_follow_up):
            message = pop_follow_up()
            if isinstance(message, str) and message:
                return message

        pop_steering = getattr(self.session, "pop_latest_steering_message", None)
        if callable(pop_steering):
            message = pop_steering()
            if isinstance(message, str) and message:
                return message

        return None

    def action_open_command_palette(self) -> None:
        """Open the slash-command palette in the prompt.

        在提示词输入框中打开斜杠命令面板。
        """
        prompt = self.query_one("#prompt", PromptInput)
        prompt.focus()
        prompt.text = "/"
        prompt.move_cursor((0, 1))
        self._completion_state = self._build_completion_state(prompt.text)
        self._refresh_completions()

    def action_open_session_picker(self) -> None:
        """Open local sessions immediately, then load other projects.

        立即打开本地会话，然后加载其他项目。
        """
        if self.state.running:
            self._notify("Tau is already working. Press Escape to cancel.")
            return
        if getattr(self.session, "session_manager", None) is None:
            self._notify("No sessions found.")
            return
        picker = SessionPickerScreen(
            (),
            local_cwd=Path(self.session.cwd),
            theme=self.tui_settings.resolved_theme,
            loading_other_projects=True,
            current_project_loaded=False,
        )
        self.push_screen(picker, callback=self._handle_session_picker_result)
        self.run_worker(self._refresh_open_session_picker(picker), exclusive=False)

    async def _refresh_open_session_picker(self, picker: SessionPickerScreen) -> None:
        """Load session indexes without blocking Textual's event loop.

        在不阻塞 Textual 事件循环的情况下加载会话索引。
        """
        try:
            local_records = await asyncio.to_thread(_local_session_records, self.session)
        except Exception as exc:  # noqa: BLE001 - still attempt the global index

            # noqa: BLE001 - 本地索引失败后仍继续尝试全局索引。
            if self.screen is picker:
                self._notify(f"Could not load current project sessions: {exc}", severity="warning")
        else:
            if not await self._wait_for_open_session_picker(picker):
                return
            picker.update_records(local_records, loading_other_projects=True)

        try:
            records = await asyncio.to_thread(_session_records, self.session)
        except Exception as exc:  # noqa: BLE001 - keep local sessions usable

            # noqa: BLE001 - 全局索引失败时仍保持本地会话可用。
            if self.screen is picker:
                picker.finish_loading()
                self._notify(f"Could not load other projects: {exc}", severity="warning")
            return
        if await self._wait_for_open_session_picker(picker):
            picker.update_records(records)

    async def _wait_for_open_session_picker(self, picker: SessionPickerScreen) -> bool:
        """Wait until this picker is mounted, or report that it was closed.

        等待此选择器完成挂载，或报告它已经关闭。
        """
        if self.screen is not picker:
            return False
        while not picker.is_mounted:
            await asyncio.sleep(0)
            if self.screen is not picker:
                return False
        return True

    # 打开已加载提示词模板的搜索选择器。
    def _open_prompt_template_picker(self) -> None:
        self.push_screen(
            PromptTemplatePickerScreen(self.session.prompt_templates),
            callback=self._handle_prompt_template_picker_result,
        )

    # 根据选择器结果插入模板、打开编辑器或保持不变。
    def _handle_prompt_template_picker_result(
        self, result: PromptTemplatePickerResult | None
    ) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.focus()
        if result is None:
            return
        if result.action == "edit":
            try:
                source = result.template.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self._notify(f"Could not read /{result.template.name}: {exc}", severity="error")
                self._open_prompt_template_picker()
                return
            self.push_screen(
                PromptTemplateEditorScreen(result.template, source),
                callback=lambda edited: self._handle_prompt_template_edit(result.template, edited),
            )
            return
        invocation = f"/{result.template.name}"
        prompt.text = invocation
        prompt.move_cursor(_text_end_location(invocation))
        self._completion_state = self._build_completion_state(invocation)
        self._refresh_completions()

    # 打开所选提示词模板的 TUI 编辑器。
    def _handle_prompt_template_edit(self, template: PromptTemplate, source: str | None) -> None:
        if source is None:
            self._open_prompt_template_picker()
            return
        self.run_worker(self._save_prompt_template_edit(template, source), exclusive=False)

    # 保存模板编辑结果，并向用户报告文件错误。
    async def _save_prompt_template_edit(self, template: PromptTemplate, source: str) -> None:
        try:
            template.path.write_text(source, encoding="utf-8")
        except OSError as exc:
            self._notify(f"Could not save /{template.name}: {exc}", severity="error")
            self._open_prompt_template_picker()
            return

        try:
            reload_result = self.session.reload()
            if isawaitable(reload_result):
                await reload_result
        except Exception as exc:  # noqa: BLE001 - saved file remains valid; surface reload errors

            # noqa: BLE001 - 已保存文件仍然有效，同时向用户显示重载错误。
            self._notify(
                f"Saved /{template.name}, but could not reload resources: {exc}",
                severity="error",
            )
        else:
            self.state.set_skills(self.session.skills)
            self._completion_state = self._build_completion_state("")
            self._refresh()
            self._notify(f"Saved /{template.name} and reloaded resources.")
        self._open_prompt_template_picker()

    def _open_skills_picker(self) -> None:
        """Open loaded-skill discovery.

        打开已加载技能的发现选择器。
        """
        self.push_screen(
            SkillPickerScreen(self.session.skills, theme=self.tui_settings.resolved_theme),
            callback=self._handle_skill_picker_result,
        )

    # 将所选技能引用插入当前提示词。
    def _handle_skill_picker_result(self, result: SkillPickerResult | None) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        if result is None:
            prompt.text = ""
        elif result.action == "insert":
            prompt.text = f"/skill:{result.skill.name}"
        else:
            prompt.text = ""
            self.state.add_item(
                "status",
                f"Skill: {result.skill.name} (not added to context)\n{result.skill.content}",
            )
            self._refresh()
        prompt.move_cursor(_text_end_location(prompt.text))
        prompt.focus()

    def action_cycle_thinking(self) -> None:
        """Cycle the active thinking mode.

        循环切换当前思考模式。
        """
        self.run_worker(self._cycle_thinking_level(), exclusive=False)

    def action_cycle_model(self) -> None:
        """Cycle forward through scoped models.

        向前循环切换范围模型。
        """
        self._cycle_model(reverse=False)

    def action_cycle_model_reverse(self) -> None:
        """Cycle backward through scoped models.

        向后循环切换范围模型。
        """
        self._cycle_model(reverse=True)

    # 安排按指定方向循环切换范围模型。
    def _cycle_model(self, *, reverse: bool) -> None:
        if self.state.running:
            self._notify("Tau is already working. Press Escape to cancel.")
            return
        self.run_worker(self._cycle_scoped_model(reverse=reverse), exclusive=False)

    def action_toggle_tool_results(self) -> None:
        """Toggle inline tool result details without rebuilding unrelated history.

        切换内联工具结果详情，而不重建无关历史。
        """
        self.state.toggle_tool_results()
        self.run_worker(self._update_tool_results_visibility(), exclusive=False)

    # 将工具结果可见性变化增量应用到已挂载对话记录。
    async def _update_tool_results_visibility(self) -> None:
        transcript = self.query_one("#transcript", TranscriptView)
        await transcript.update_tool_results_visibility(
            self.state,
            theme=self.tui_settings.resolved_theme,
        )

    def action_toggle_thinking(self) -> None:
        """Toggle thinking-token display in the transcript.

        切换对话记录中的思考令牌显示。
        """
        self.state.toggle_thinking()
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.update_thinking_visibility(
            self.state,
            theme=self.tui_settings.resolved_theme,
        )

    # 收到有效会话 ID 后安排恢复该会话。
    def _handle_session_picker_result(self, session_id: str | None) -> None:
        if session_id is None:
            return
        self.run_worker(self._resume_session(session_id), exclusive=False)

    # 恢复指定会话，并重新绑定主题、扩展和可见状态。
    async def _resume_session(self, session_id: str) -> None:
        try:
            previous_cwd = Path(self.session.cwd).resolve()
            resume_message = await self.session.resume(session_id)
            self._reload_session_themes()
            self.state.clear()
            self.state.set_skills(self.session.skills)
            self._load_session_messages_from_session()
            current_cwd = Path(self.session.cwd).resolve()
            if current_cwd != previous_cwd:
                resume_message = f"{resume_message} ({_short_path(current_cwd)})"
            self._notify(resume_message)
        except Exception as exc:  # noqa: BLE001 - surface command failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示命令失败。
            self._notify(f"Error: {exc}", severity="error")
        self._refresh()

    # 加载会话树并打开分支与标签操作选择器。
    async def _open_tree_picker(self) -> None:
        if self._is_agent_or_queue_active():
            self._notify(TREE_RUNNING_MESSAGE, severity="warning")
            return
        tree_choices = getattr(self.session, "tree_choices", None)
        if tree_choices is None:
            self._notify("Session tree is not available.", severity="warning")
            return
        try:
            choices = tuple(await tree_choices())
        except Exception as exc:  # noqa: BLE001 - surface command failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示命令失败。
            self._notify(f"Error: {exc}", severity="error")
            return
        if not choices:
            self._notify("No session entries are available for branching.", severity="warning")
            return
        self.push_screen(
            TreePickerScreen(
                choices,
                theme=self.tui_settings.resolved_theme,
                on_label_change=self._set_tree_label,
            ),
            callback=self._handle_tree_picker_result,
        )

    # 更新树条目标签并返回更新时间戳。
    async def _set_tree_label(self, entry_id: str, label: str | None) -> float:
        set_label = getattr(self.session, "set_label", None)
        if set_label is None:
            raise RuntimeError("Session labels are not available.")
        entry = set_label(entry_id, label)
        if isawaitable(entry):
            entry = await entry
        self._notify("Label cleared." if label is None else f"Label set to [{label}].")
        return float(entry.timestamp)

    # 根据树选择结果执行分支、重命名或删除标签操作。
    def _handle_tree_picker_result(self, result: TreePickerResult | None) -> None:
        if result is None:
            return
        self.run_worker(
            self._branch_to_tree_entry(
                result.entry_id,
                summarize=result.summarize,
                custom_instructions=result.custom_instructions,
            ),
            exclusive=False,
        )

    # 从选定树条目创建分支会话并切换当前 TUI 状态。
    async def _branch_to_tree_entry(
        self,
        entry_id: str,
        *,
        summarize: bool,
        custom_instructions: str | None = None,
    ) -> None:
        if self._is_agent_or_queue_active():
            self._notify(TREE_RUNNING_MESSAGE, severity="warning")
            return
        branch_to_entry = getattr(self.session, "branch_to_entry", None)
        if branch_to_entry is None:
            self._notify("Session tree is not available.", severity="warning")
            return
        try:
            if summarize:
                self.state.clear()
                self.state.add_item("status", "Summarizing branch…")
                self._refresh()

            result = branch_to_entry(
                entry_id,
                summarize=summarize,
                custom_instructions=custom_instructions,
            )
            if isawaitable(result):
                result = await result
            self.state.clear()
            self.state.set_skills(self.session.skills)
            self._load_session_messages_from_session()
            if isinstance(result, SessionTreeBranchResult):
                if result.input_prefill is not None:
                    prompt = self.query_one("#prompt", PromptInput)
                    prompt.value = result.input_prefill
                    prompt.move_cursor(_text_end_location(result.input_prefill))
                    prompt.focus()
                self._notify(result.message)
            elif isinstance(result, str):
                self._notify(result)
        except Exception as exc:  # noqa: BLE001 - surface command failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示命令失败。
            self._notify(f"Error: {exc}", severity="error")
        self._refresh()

    # 清空当前界面状态并创建一个全新的编码会话。
    async def _new_session(self) -> None:
        self._cancel_active_prompt(notify=False, interrupt=True)
        new_session = getattr(self.session, "new_session", None)
        if new_session is None:
            self._notify("Session manager is not available.")
            return
        try:
            await new_session()
            self._reload_session_themes()
            self.state.clear()
            self.state.set_skills(self.session.skills)
            self._load_session_messages_from_session()
        except Exception as exc:  # noqa: BLE001 - surface command failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示命令失败。
            self._notify(f"Error: {exc}", severity="error")
        self._refresh()

    # 把所选补全值写入提示词，并返回补全后的文本或空值。
    def _apply_selected_completion(self, value: str) -> str | None:
        item = self._completion_state.selected
        if item is None:
            return None
        return item.apply(value)

    def _append_command_message(
        self,
        command_text: str,
        message: str,
        *,
        system_prompt_inspection: SystemPromptInspection | None = None,
    ) -> None:
        """Append non-persistent command output to the visible transcript.

        将非持久化命令输出追加到可见对话记录。
        """
        is_system_prompt = command_text.split(maxsplit=1)[0].casefold() == "/system"
        separator = "\n\n" if is_system_prompt else "\n"
        title = _command_output_title(command_text)
        if is_system_prompt:
            title = f"### {title}"
        self.state.add_item(
            "status",
            f"{title}{separator}{message}",
            system_prompt=is_system_prompt,
            system_prompt_sources=(
                system_prompt_inspection.sources if system_prompt_inspection is not None else None
            ),
        )

    # 根据命令类型使用对话记录、模态窗口或通知显示命令结果。
    def _show_command_message(self, command_text: str, message: str) -> None:
        self.push_screen(
            CommandOutputScreen(
                _command_output_title(command_text),
                message,
                theme=self.tui_settings.resolved_theme,
                auto_copy_selection=command_text.strip().split(maxsplit=1)[0] == "/session",
            )
        )

    # 打开登录方式选择器。
    def _open_login_picker(self) -> None:
        self.push_screen(
            LoginMethodPickerScreen(theme=self.tui_settings.resolved_theme),
            callback=self._handle_login_method_result,
        )

    # 根据所选登录方式继续打开提供商或自定义提供商流程。
    def _handle_login_method_result(self, method: str | None) -> None:
        if method is None:
            return
        if method == "subscription":
            providers = _subscription_login_providers(BUILTIN_PROVIDER_CATALOG)
        elif method == "api-key":
            providers = _api_key_login_providers(BUILTIN_PROVIDER_CATALOG)
        elif method == "custom":
            self._open_custom_provider_login()
            return
        else:
            self._notify(f"Unknown login method: {method}", severity="error")
            return
        if not providers:
            self._notify("No login providers are available for that method.", severity="warning")
            return
        self.push_screen(
            LoginProviderPickerScreen(
                providers,
                theme=self.tui_settings.resolved_theme,
                back_on_cancel=True,
            ),
            callback=lambda provider_name: self._handle_login_provider_result(
                provider_name,
                method=method,
            ),
        )

    # 处理提供商选择结果，并支持返回登录方式选择器。
    def _handle_login_provider_result(
        self,
        provider_name: str | _LoginFlowAction | None,
        *,
        method: str | None = None,
    ) -> None:
        if provider_name is _LoginFlowAction.BACK:
            self._open_login_picker()
        elif provider_name is not None:
            self._open_login(provider_name, method=method)

    # 打开 OpenAI 兼容自定义提供商登录表单。
    def _open_custom_provider_login(self) -> None:
        self.push_screen(
            CustomProviderLoginScreen(theme=self.tui_settings.resolved_theme),
            callback=self._handle_custom_provider_login_result,
        )

    # 校验并持久化自定义提供商配置及凭据。
    def _handle_custom_provider_login_result(
        self,
        result: CustomProviderLoginResult | _LoginFlowAction | None,
    ) -> None:
        if result is _LoginFlowAction.BACK:
            self._open_login_picker()
            return
        if result is None:
            return
        provider = OpenAICompatibleProviderConfig(
            name=result.provider_name,
            base_url=result.base_url.rstrip("/"),
            api_key_env=result.api_key_env,
            credential_name=result.provider_name,
            models=result.models,
            default_model=result.default_model,
        )
        catalog_entry = ProviderCatalogEntry(
            name=provider.name,
            display_name=result.display_name,
            kind="openai-compatible",
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            credential_name=provider.credential_name,
            models=provider.models,
            default_model=provider.default_model,
            docs_url=provider.base_url,
        )
        try:
            save_user_catalog_entries((catalog_entry,))
            FileCredentialStore().set(provider.credential_name or provider.name, result.api_key)
            settings = load_provider_settings()
            updated = upsert_openai_compatible_provider(settings, provider, set_default=False)
            save_provider_settings(updated)
            self.session.reload_provider_settings()
            try:
                self.session.set_provider(provider.name, persist_default=False)
            except TypeError:
                self.session.set_provider(provider.name)
        except Exception as exc:  # noqa: BLE001 - surface login failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示登录失败。
            self._notify(f"Could not save custom provider: {exc}", severity="error")
            return
        self._notify(f"Saved custom provider {result.display_name}.")
        self._refresh()

    # 按提供商能力打开 OAuth 或 API 密钥登录界面。
    def _open_login(self, provider_name: str, *, method: str | None = None) -> None:
        entry = builtin_provider_entry(provider_name)
        if entry is None:
            self._notify(f"Unknown provider: {provider_name}", severity="error")
            return
        use_oauth = method == "subscription" or (
            method is None and "api_key" not in entry.auth_methods
        )
        if use_oauth and get_oauth_provider(entry.name) is not None:
            login = None
            if entry.name == "openai-codex":

                # 使用 OpenAI Codex 的回调接口完成 OAuth 登录。
                async def login(callbacks: OAuthLoginCallbacks) -> OAuthCredential:
                    return await login_openai_codex(
                        on_auth=callbacks.on_auth,
                        on_prompt=callbacks.on_prompt,
                        on_manual_code_input=callbacks.on_manual_code_input,
                        on_progress=callbacks.on_progress,
                    )

            self.push_screen(
                OAuthLoginScreen(
                    entry,
                    theme=self.tui_settings.resolved_theme,
                    login=login,
                ),
                callback=lambda credential: self._handle_oauth_login_navigation_result(
                    entry, credential
                ),
            )
            return
        self.push_screen(
            LoginScreen(entry, theme=self.tui_settings.resolved_theme),
            callback=lambda api_key: self._handle_api_key_login_navigation_result(entry, api_key),
        )

    # 处理 API 密钥登录导航结果，或返回登录方式选择器。
    def _handle_api_key_login_navigation_result(
        self,
        entry: ProviderCatalogEntry,
        result: str | _LoginFlowAction | None,
    ) -> None:
        if result is _LoginFlowAction.BACK:
            self._open_login_picker()
        else:
            self._handle_login_result(entry, result)

    # 保存 API 密钥并把对应提供商切换为当前会话提供商。
    def _handle_login_result(self, entry: ProviderCatalogEntry, api_key: str | None) -> None:
        if api_key is None:
            return
        if entry.credential_name is None:
            self._notify(
                f"Provider {entry.name} does not support saved credentials.",
                severity="error",
            )
            return
        try:
            FileCredentialStore().set(entry.credential_name, api_key)
            provider = provider_config_from_catalog_entry(entry.name)
            upsert_saved_provider(provider, set_default=False)
            self.session.reload_provider_settings()
            try:
                self.session.set_provider(entry.name, persist_default=False)
            except TypeError:
                self.session.set_provider(entry.name)
        except Exception as exc:  # noqa: BLE001 - surface login failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示登录失败。
            self._notify(f"Could not save login: {exc}", severity="error")
            return
        self._notify(f"Saved login for {entry.display_name}.")
        self._refresh()

    # 处理 OAuth 登录导航结果，或返回登录方式选择器。
    def _handle_oauth_login_navigation_result(
        self,
        entry: ProviderCatalogEntry,
        result: OAuthCredential | _LoginFlowAction | None,
    ) -> None:
        if result is _LoginFlowAction.BACK:
            self._open_login_picker()
        else:
            self._handle_oauth_login_result(entry, result)

    # 保存 OAuth 凭据并把对应提供商切换为当前会话提供商。
    def _handle_oauth_login_result(
        self,
        entry: ProviderCatalogEntry,
        credential: OAuthCredential | None,
    ) -> None:
        if credential is None:
            return
        if entry.credential_name is None:
            self._notify(
                f"Provider {entry.name} does not support saved credentials.",
                severity="error",
            )
            return
        try:
            FileCredentialStore().set_oauth(entry.credential_name, credential)
            provider = provider_config_from_catalog_entry(entry.name)
            upsert_saved_provider(provider, set_default=False)
            self.session.reload_provider_settings()
            try:
                self.session.set_provider(entry.name, persist_default=False)
            except TypeError:
                self.session.set_provider(entry.name)
        except Exception as exc:  # noqa: BLE001 - surface login failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示登录失败。
            self._notify(f"Could not save login: {exc}", severity="error")
            return
        self._notify(f"Saved login for {entry.display_name}.")
        self._refresh()

    # 打开已有持久凭据的提供商退出选择器。
    def _open_logout_picker(self) -> None:
        providers = _stored_credential_providers(BUILTIN_PROVIDER_CATALOG)
        if not providers:
            self._notify(NO_STORED_CREDENTIALS_MESSAGE, severity="warning")
            return
        self.push_screen(
            LoginProviderPickerScreen(
                providers,
                theme=self.tui_settings.resolved_theme,
                title="Logout",
            ),
            callback=self._handle_logout_provider_result,
        )

    # 将有效的退出选择结果交给凭据删除流程。
    def _handle_logout_provider_result(self, provider_name: str | _LoginFlowAction | None) -> None:
        if isinstance(provider_name, str):
            self._logout(provider_name)

    # 删除指定提供商的持久凭据并刷新会话设置。
    def _logout(self, provider_name: str) -> None:
        entry = builtin_provider_entry(provider_name)
        if entry is None:
            self._notify(f"Unknown provider: {provider_name}", severity="error")
            return

        if entry.credential_name is None:
            self._notify(NO_STORED_CREDENTIALS_MESSAGE, severity="warning")
            return
        credential_store = FileCredentialStore()
        if not _credential_store_has_entry(credential_store, entry.credential_name):
            self._notify(NO_STORED_CREDENTIALS_MESSAGE, severity="warning")
            return

        try:
            credential_store.delete(entry.credential_name)
            self.session.reload_provider_settings()
        except Exception as exc:  # noqa: BLE001 - surface logout failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示退出登录失败。
            self._notify(f"Could not log out: {exc}", severity="error")
            return

        if entry.kind == "openai-codex":
            self._notify(f"Logged out of {entry.display_name}.")
        else:
            self._notify(
                f"Removed stored API key for {entry.display_name}. "
                "Environment variables and providers.json config are unchanged."
            )
        self._refresh()

    # 返回会话当前可用的提供商与模型组合。
    def _available_model_choices(self) -> tuple[ModelChoice, ...]:
        fallback_choices = (
            ModelChoice(provider_name=self.session.provider_name, model=model)
            for model in self.session.available_models
        )
        return tuple(
            getattr(
                self.session,
                "available_model_choices",
                fallback_choices,
            )
        )

    def _open_local_backend_picker(self) -> None:
        """Open the generic local-backend chooser and require confirmation.

        打开通用本地后端选择器，并要求用户确认。
        """
        runtime = getattr(self.session, "extension_runtime", None)
        registry = getattr(runtime, "local_backend_registry", None)
        if registry is None:
            self._notify("Local backend controls are unavailable.", severity="warning")
            return
        self.push_screen(
            LocalBackendPickerScreen(registry, theme=self.tui_settings.resolved_theme),
            callback=self._handle_local_backend_picker_result,
        )

    # 延迟处理本地后端选择结果，避免回调打开的新屏幕被旧屏幕弹出。
    def _handle_local_backend_picker_result(self, backend_id: str | None) -> None:
        # Screen.dismiss() invokes its result callback before popping the screen.
        # Defer the transition so the picker cannot pop the backend screen that
        # this callback opens.

        # Screen.dismiss() 会先调用结果回调，再弹出当前屏幕。延迟切换可避免
        # 选择器把此回调新打开的后端屏幕一并弹出。
        self.call_later(self._finish_local_backend_picker, backend_id)

    # 验证选择的后端仍可用，并打开其管理屏幕。
    def _finish_local_backend_picker(self, backend_id: str | None) -> None:
        self._restore_prompt_focus()
        if backend_id is None:
            return
        runtime = getattr(self.session, "extension_runtime", None)
        registry = getattr(runtime, "local_backend_registry", None)
        if registry is None or registry.effective(backend_id) is None:
            self._notify("The selected local backend is no longer available.", severity="warning")
            return
        self.push_screen(
            LocalBackendScreen(
                registry,
                backend_id,
                theme=self.tui_settings.resolved_theme,
                on_use=self._use_local_model,
                notify_callback=self._notify_local_backend,
                is_idle=lambda: not self._is_agent_or_queue_active(),
            )
        )

    # 在关闭临时屏幕后恢复提示词输入焦点。
    def _restore_prompt_focus(self) -> None:
        with suppress(NoMatches):
            self.query_one("#prompt", PromptInput).focus()

    # 把本地后端通知级别映射为 Tau 通知严重程度。
    def _notify_local_backend(self, message: str, level: str) -> None:
        severity: Literal["information", "warning", "error"] = {
            "info": "information",
            "warning": "warning",
            "error": "error",
        }.get(level, "information")  # type: ignore[assignment]
        self._notify(message, severity=severity)

    # 在会话空闲时切换到指定的本地提供商模型。
    async def _use_local_model(self, provider_id: str, model_id: str) -> None:
        if self._is_agent_or_queue_active():
            self._notify(
                "Tau is still working. Press Escape to interrupt before switching models.",
                severity="warning",
            )
            return
        await self._switch_model(ModelChoice(provider_name=provider_id, model=model_id))

    def _open_tools_reference(self) -> None:
        """Open a read-only view of tools from the active session.

        打开当前会话工具的只读视图。
        """
        self.push_screen(
            ToolsReferenceScreen(
                self.session.tools,
                extension_sources=self.session.extension_tool_sources,
                theme=self.tui_settings.resolved_theme,
            )
        )

    # 打开包含可用模型和范围模型的模型选择器。
    def _open_model_picker(self) -> None:
        choices = self._available_model_choices()
        scoped = tuple(getattr(self.session, "scoped_model_choices", ()))
        if not choices and not scoped:
            self._notify(
                "No configured providers are usable. Run /login to set up a provider.",
                severity="warning",
            )
            return
        self.push_screen(
            ModelPickerScreen(
                choices,
                scoped_choices=scoped,
                current_model=self.session.model,
                provider_name=self.session.provider_name,
                theme=self.tui_settings.resolved_theme,
                on_toggle_scoped=None,
                picker_kind="model",
            ),
            callback=self._handle_model_picker_result,
        )
        self.run_worker(self._refresh_open_model_picker(), exclusive=False)

    # 在选择器保持打开时异步刷新模型目录及选项。
    async def _refresh_open_model_picker(self) -> None:
        refresh = getattr(self.session, "refresh_model_catalogs", None)
        if not callable(refresh):
            return
        try:
            await refresh()
        except Exception as error:
            if isinstance(self.screen, ModelPickerScreen):
                self._notify(f"Could not refresh model catalogs: {error}", severity="warning")
            return
        if not isinstance(self.screen, ModelPickerScreen):
            return
        picker = self.screen
        while not picker.is_mounted:
            await asyncio.sleep(0)
            if self.screen is not picker:
                return
        picker.update_choices(
            self._available_model_choices(),
            tuple(getattr(self.session, "scoped_model_choices", ())),
        )

    # 打开用于管理范围模型集合的选择器。
    def _open_scoped_models_picker(self) -> None:
        choices = self._available_model_choices()
        scoped = tuple(getattr(self.session, "scoped_model_choices", ()))
        if not choices and not scoped:
            self._notify(
                "No configured providers are usable. Run /login to set up a provider.",
                severity="warning",
            )
            return
        self.push_screen(
            ModelPickerScreen(
                choices,
                scoped_choices=scoped,
                current_model=self.session.model,
                provider_name=self.session.provider_name,
                theme=self.tui_settings.resolved_theme,
                on_toggle_scoped=self._toggle_scoped_model,
                picker_kind="scoped",
            ),
            callback=self._handle_scoped_models_picker_result,
        )
        self.run_worker(self._refresh_open_model_picker(), exclusive=False)

    # 切换单个模型是否属于范围模型集合。
    def _toggle_scoped_model(self, choice: ModelChoice) -> Sequence[ModelChoice]:
        toggle_scoped_model = getattr(self.session, "toggle_scoped_model", None)
        if toggle_scoped_model is None:
            self._notify("Scoped model controls are not available.", severity="warning")
            return tuple(getattr(self.session, "scoped_model_choices", ()))
        try:
            return tuple(toggle_scoped_model(choice))
        except Exception as exc:  # noqa: BLE001 - surface session state failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示会话状态更新失败。
            self._notify(f"Could not update scoped models: {exc}", severity="error")
            return tuple(getattr(self.session, "scoped_model_choices", ()))

    # 关闭范围模型选择器后刷新界面外框。
    def _handle_scoped_models_picker_result(self, choice: ModelChoice | None) -> None:
        del choice
        self._refresh_chrome()

    # 根据模型选择结果安排异步切换。
    def _handle_model_picker_result(self, choice: ModelChoice | None) -> None:
        if choice is None:
            return
        self.run_worker(self._switch_model(choice), exclusive=False)

    # 通过会话提供的最佳接口切换提供商和模型。
    async def _switch_model(self, choice: ModelChoice) -> None:
        try:
            select = getattr(self.session, "select_provider_model", None)
            if select is not None:
                result = select(choice)
                if isawaitable(result):
                    await result
            else:
                set_model_choice = getattr(self.session, "set_model_choice", None)
                if set_model_choice is None:
                    if choice.provider_name != self.session.provider_name:
                        self.session.set_provider(choice.provider_name)
                    self.session.set_model(choice.model)
                else:
                    set_model_choice(choice)
        except Exception as exc:  # noqa: BLE001 - surface model switch failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示模型切换失败。
            self._notify(f"Could not switch model: {exc}", severity="error")
            return
        self._refresh_chrome()

    # 打开可用 TUI 主题选择器。
    def _open_theme_picker(self) -> None:
        self.push_screen(
            ThemePickerScreen(
                current_theme=self.tui_settings.theme,
                theme=self.tui_settings.resolved_theme,
                theme_names=available_tui_theme_names(),
            ),
            callback=self._handle_theme_picker_result,
        )

    # 将有效主题选择结果应用到 TUI。
    def _handle_theme_picker_result(self, theme: TuiThemeName | None) -> None:
        if theme is None:
            return
        self._set_tui_theme(theme)

    # 调用会话接口设置明确的思考等级。
    async def _set_thinking_level(self, level: str) -> None:
        setter = getattr(self.session, "set_thinking_level", None)
        if setter is None:
            self._notify("Thinking controls are not available.", severity="warning")
            return
        try:
            result = setter(level)
            if isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - surface session state failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示会话状态更新失败。
            self._notify(f"Could not change thinking mode: {exc}", severity="error")
            return
        self._refresh_chrome()

    # 调用会话接口循环切换思考等级。
    async def _cycle_thinking_level(self) -> None:
        cycler = getattr(self.session, "cycle_thinking_level", None)
        if cycler is None:
            self._notify("Thinking controls are not available.", severity="warning")
            return
        try:
            result = cycler()
            if isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - surface session state failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示会话状态更新失败。
            self._notify(f"Could not change thinking mode: {exc}", severity="error")
            return
        self._refresh_chrome()

    # 按指定方向调用会话接口循环切换范围模型。
    async def _cycle_scoped_model(self, *, reverse: bool = False) -> None:
        cycler = getattr(self.session, "cycle_scoped_model", None)
        if cycler is None:
            self._notify("Scoped model controls are not available.", severity="warning")
            return
        try:
            result = cycler(reverse=reverse)
            if isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - surface session state failures in the TUI

            # noqa: BLE001 - 在 TUI 中显示会话状态更新失败。
            self._notify(f"Could not switch scoped model: {exc}", severity="error")
            return
        self._refresh_chrome()

    # 对相同消息和严重程度去重后显示应用通知。
    def _notify(
        self,
        message: str,
        *,
        severity: Literal["information", "warning", "error"] = "information",
    ) -> None:
        key = (message, severity)
        if key in self._active_notification_keys:
            return
        self._active_notification_keys.add(key)
        self.set_timer(
            self.NOTIFICATION_TIMEOUT,
            lambda: self._active_notification_keys.discard(key),
            name=f"notification-dedupe-{hash(key)}",
        )
        self.notify(message, severity=severity, markup=False)

    # 同步刷新完整对话记录及界面外框。
    def _refresh(self) -> None:
        theme = self.tui_settings.resolved_theme
        self._refresh_chrome(theme=theme)
        transcript = self.query_one("#transcript", TranscriptView)
        transcript.update_from_state(self.state, theme=theme)

    def _refresh_chrome(self, *, theme: TuiTheme | None = None) -> None:
        """Refresh non-transcript chrome without remounting transcript blocks.

        刷新对话记录之外的界面外框，而不重新挂载对话块。
        """
        theme = theme or self.tui_settings.resolved_theme
        self._sync_session_title()
        self._sync_text_selection_state()
        self._sync_queue_state()
        sidebar = self.query_one("#sidebar", SessionSidebar)
        sidebar.update_from_session(self.session, theme=theme)
        if self._extension_sidebar_theme != theme:
            self._rebuild_extension_sidebar_sections(theme=theme)
        compact_info = self.query_one("#compact-session-info", CompactSessionInfo)
        compact_info.update_from_session(self.session, theme=theme)
        queued_messages = self.query_one("#queued-messages", Static)
        queue_render_key = (
            self.state.queued_steering,
            self.state.queued_follow_up,
            theme.name,
            theme.muted_text,
        )
        if queue_render_key != self._last_queue_render_key:
            self._last_queue_render_key = queue_render_key
            queued_messages.display = self.state.queued_message_count > 0
            queued_messages.update(_render_queued_messages(self.state, theme=theme))
        self._sync_activity_indicator()
        self._refresh_footer_bindings()

    # 从会话同步排队消息数量和预览内容。
    def _sync_queue_state(self) -> None:
        queue_event = getattr(self.session, "queue_update_event", None)
        if not callable(queue_event):
            return
        self.adapter.apply(queue_event())

    def _refresh_chrome_if_mounted(self) -> None:
        """Refresh chrome when the app is mounted, ignoring teardown races.

        应用已挂载时刷新界面外框，并忽略卸载竞争。
        """
        if not self.screen_stack:
            return
        with suppress(NoMatches):
            self._refresh_chrome()

    # 根据工作状态启动或停止活动动画计时器。
    def _sync_activity_indicator(self) -> None:
        self._sync_terminal_title()
        if self._is_working():
            if self._activity_timer is None:
                self._activity_timer = self.set_interval(
                    ACTIVITY_TICK_SECONDS,
                    self._tick_activity,
                    name="activity-indicator",
                )
            else:
                self._activity_timer.resume()
            self._apply_activity_indicator()
            return
        self._activity_frame = 0
        if self._activity_timer is not None:
            self._activity_timer.pause()
        self._apply_activity_indicator()

    # 推进一帧活动动画并刷新提示词指示器。
    def _tick_activity(self) -> None:
        if not self._is_working():
            return
        self._activity_frame += 1
        self._apply_activity_indicator()
        self._sync_terminal_title()
        now = asyncio.get_running_loop().time()
        if now - self._last_tool_timer_refresh_at >= 1.0:
            self._last_tool_timer_refresh_at = now
            self.call_later(self._refresh_pending_tool_timer)

    async def _refresh_pending_tool_timer(self) -> None:
        """Refresh elapsed time on the tool row that is currently executing.

        刷新当前执行中工具行的已用时间。
        """
        if not self.state.running:
            return
        item = next(
            (
                candidate
                for candidate in reversed(self.state.items)
                if candidate.role == "tool" and candidate.tool_result_text is None
            ),
            None,
        )
        if item is None:
            return
        try:
            transcript = self.query_one("#transcript", TranscriptView)
        except NoMatches:
            return
        expanded = self.state.show_tool_results or item.always_show_tool_result
        await transcript.update_item(
            item,
            theme=self.tui_settings.resolved_theme,
            show_tool_results=expanded,
            invocation=self.state.resolve_tool_invocation(item, expanded=expanded),
            result_markup=self.state.resolve_tool_result(item, expanded=expanded),
        )

    # 根据工作、排队和 shell 状态更新提示词前缀及边框。
    def _apply_activity_indicator(self) -> None:
        theme = self.tui_settings.resolved_theme
        try:
            prompt = self.query_one("#prompt", PromptInput)
            prompt_prefix = self.query_one("#prompt-prefix", Static)
        except NoMatches:
            return
        shell_mode = _is_terminal_command_prompt(prompt.text)
        render_key = (
            theme.name,
            theme.accent,
            theme.screen_background,
            theme.prompt_border,
            self._activity_frame,
            self._is_working(),
            shell_mode,
        )
        if render_key == self._last_activity_indicator_key:
            return
        self._last_activity_indicator_key = render_key
        prompt.styles.border_left = (
            "tall",
            _activity_prompt_border_color(
                theme,
                frame=self._activity_frame,
                running=self._is_working(),
                shell_mode=shell_mode,
            ),
        )
        prompt_prefix.update(
            _render_activity_indicator(
                theme,
                frame=self._activity_frame,
                running=self._is_working(),
                shell_mode=shell_mode,
            ),
            layout=False,
        )

    # 重算补全可见窗口并更新建议组件的内容与显示状态。
    def _refresh_completions(self) -> None:
        suggestions = self.query_one("#autocomplete", Static)
        suggestions.display = bool(self._completion_state.items)
        if not self._completion_state.items:
            self._completion_visible_line_budget = None
            suggestions.update(
                render_completion_suggestions(
                    CompletionState(),
                    theme=self.tui_settings.resolved_theme,
                )
            )
            self._refresh_footer_bindings()
            return
        max_lines = self._completion_window_line_budget(suggestions)
        suggestions.update(
            render_completion_suggestions(
                _visible_completion_state(
                    self._completion_state,
                    max_lines=max_lines,
                    width=max(suggestions.content_size.width or suggestions.size.width, 1),
                ),
                theme=self.tui_settings.resolved_theme,
            )
        )
        self._refresh_footer_bindings()

    def _completion_window_line_budget(self, suggestions: Static) -> int:
        """Return a stable completion window size for the current suggestion box.

        返回当前建议框稳定的补全窗口大小。

        The autocomplete widget has ``height: auto``. If we used its current
        rendered height as the next render limit unconditionally, selecting an
        item could render fewer rows, which would shrink the widget, which would
        then make the next render limit smaller again. Keep the largest measured
        height for the current completion session so navigation does not feed
        back into progressively smaller boxes.

        自动补全组件使用 ``height: auto``。如果无条件把当前渲染高度用作下一次
        渲染上限，选择项目可能减少渲染行数，继而缩小组件，再进一步缩小后续
        渲染上限。这里保留当前补全会话测得的最大高度，防止导航反馈造成建议框
        持续缩小。
        """
        measured_limit = _completion_visible_line_limit(suggestions)
        if suggestions.size.height <= 0:
            if self._completion_visible_line_budget is None:
                self._completion_visible_line_budget = self._initial_completion_line_budget()
            return self._completion_visible_line_budget
        self._completion_visible_line_budget = max(
            self._completion_visible_line_budget or measured_limit,
            measured_limit,
        )
        return self._completion_visible_line_budget

    def _initial_completion_line_budget(self) -> int:
        """Estimate the first completion window size before Textual lays it out.

        在 Textual 完成布局前估算首个补全窗口大小。
        """
        terminal_height = self.size.height
        if terminal_height <= 0:
            return COMPLETION_MAX_VISIBLE_LINES

        reserved_rows = COMPLETION_MIN_TRANSCRIPT_LINES + COMPLETION_WIDGET_CHROME_LINES
        for selector in ("#prompt-row", "#compact-session-info", "#queued-messages"):
            with suppress(NoMatches):
                widget = self.query_one(selector)
                if widget.display:
                    reserved_rows += widget.outer_size.height

        available_rows = terminal_height - reserved_rows
        terminal_fraction_rows = max(1, terminal_height // COMPLETION_INITIAL_TERMINAL_FRACTION)
        return max(
            1,
            min(COMPLETION_MAX_VISIBLE_LINES, available_rows, terminal_fraction_rows),
        )

    # 根据终端宽高切换侧栏、紧凑会话信息和补全框布局。
    def _update_responsive_layout(self, width: int, height: int) -> None:
        if self._sidebar_visibility_override is not None:
            show_sidebar = self._sidebar_visibility_override
        elif self.tui_settings.sidebar_position == "off":
            show_sidebar = False
        else:
            show_sidebar = width >= SIDEBAR_MIN_WIDTH and height >= SIDEBAR_MIN_HEIGHT
        self.set_class(not show_sidebar, "-hide-sidebar")

    def _apply_sidebar_position(self) -> None:
        """Apply the configured (or off-setting fallback) sidebar position.

        应用配置的侧栏位置，或应用关闭设置对应的回退位置。
        """
        pos = self.tui_settings.sidebar_position
        # A configured ``off`` has no prior visible position, so an explicit
        # session-only show uses the normal right-hand placement.

        # 配置为 ``off`` 时没有先前可见位置，因此仅当前会话明确显示时采用通常的
        # 右侧位置。
        show_right = pos == "right" or (pos == "off" and self._sidebar_visibility_override is True)
        self.set_class(show_right, "-sidebar-right")

    def _toggle_sidebar_visibility(self) -> None:
        """Toggle sidebar visibility without changing durable TUI settings.

        切换侧栏可见性，而不修改持久化 TUI 设置。
        """
        currently_visible = not self.has_class("-hide-sidebar")
        self._sidebar_visibility_override = not currently_visible
        self._apply_sidebar_position()
        self._update_responsive_layout(self.size.width, self.size.height)
        state = "shown" if self._sidebar_visibility_override else "hidden"
        self._notify(f"Sidebar {state} for this session.")

    # 根据提示词文本、光标和会话资源构建补全状态。
    def _build_completion_state(self, text: str, *, cursor: int | None = None) -> CompletionState:
        registry = _session_command_registry(self.session)
        return build_completion_state(
            text,
            cursor=cursor,
            command_registry=registry,
            skills=self.session.skills,
            prompt_templates=self.session.prompt_templates,
            model_names=self.session.available_models,
            provider_names=(
                *self.session.available_providers,
                *LOGIN_PROVIDER_ALIASES,
            ),
            thinking_levels=getattr(self.session, "available_thinking_levels", ()),
            theme_names=available_tui_theme_names(),
            session_options=(_session_options(self.session) if text.startswith("/resume ") else ()),
            cwd=self.session.cwd,
        )

    # 根据运行与补全状态更新提示词底部快捷键绑定。
    def _refresh_footer_bindings(self) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.set_footer_mode(
            _prompt_footer_mode(self._completion_state, working=self._is_working())
        )

    # 根据终端命令前缀切换提示词输入框的 shell 模式样式。
    def _sync_prompt_shell_mode(self, text: str) -> None:
        prompt = self.query_one("#prompt", PromptInput)
        prompt.shell_mode_style = self.tui_settings.resolved_theme.role_styles["tool"].border
        prompt.set_class(_is_terminal_command_prompt(text), "-shell-mode")
        prompt.refresh()
        self._apply_activity_indicator()


def _activity_prompt_border_color(
    theme: TuiTheme,
    *,
    frame: int,
    running: bool,
    shell_mode: bool,
) -> str:
    """Return the prompt border color for the current activity animation frame.

    返回当前活动动画帧对应的提示框边框颜色。
    """
    del frame, running
    if shell_mode:
        return theme.role_styles["tool"].border
    return theme.prompt_border


def _render_activity_indicator(
    theme: TuiTheme,
    *,
    frame: int,
    running: bool,
    shell_mode: bool = False,
) -> Text:
    """Render the prompt prefix: a moving square while running, ``$`` in shell mode.

    渲染提示词前缀：运行时显示移动方块，shell 模式下显示 ``$``。
    """
    if shell_mode and not running:
        return Text("$", style=f"bold {theme.role_styles['tool'].border}")
    if not running:
        return Text("τ", style=f"bold {theme.accent}")

    cycle_length = (ACTIVITY_INDICATOR_HEIGHT - 1) * 2
    cycle_position = frame % cycle_length
    active_row = (
        cycle_position
        if cycle_position < ACTIVITY_INDICATOR_HEIGHT
        else cycle_length - cycle_position
    )
    direction = 1 if cycle_position < ACTIVITY_INDICATOR_HEIGHT else -1
    trail_rows = {
        active_row: theme.accent,
        active_row - direction: _blend_hex_colors(
            theme.accent,
            theme.screen_background,
            fraction=0.35,
        ),
        active_row - (direction * 2): _blend_hex_colors(
            theme.accent,
            theme.screen_background,
            fraction=0.65,
        ),
    }

    rendered = Text()
    for row in range(ACTIVITY_INDICATOR_HEIGHT):
        color = trail_rows.get(row)
        if color is None:
            rendered.append(" ")
        else:
            rendered.append("■", style=color)
        if row < ACTIVITY_INDICATOR_HEIGHT - 1:
            rendered.append("\n")
    return rendered


def _is_terminal_command_prompt(text: str) -> bool:
    """Return whether the prompt is currently in terminal-command mode.

    返回提示词当前是否处于终端命令模式。
    """
    return _terminal_command_prefix_span(text) is not None


def _should_optimistically_render_prompt(text: str) -> bool:
    """Return whether submitted text can be safely shown before session expansion.

    判断会话展开文本前是否可以安全地先显示已提交的提示词。
    """
    stripped = text.strip()
    return bool(stripped) and not stripped.startswith("/")


def _is_user_message_end_event(event: CodingSessionEvent) -> bool:
    """Return whether an agent event closes a user-context message.

    判断代理事件是否结束了一条用户上下文消息。
    """
    return isinstance(event, MessageEndEvent) and isinstance(
        event.message, (UserMessage, CustomMessage)
    )


def _terminal_command_prefix_span(text: str) -> tuple[int, int] | None:
    """Return the input span for a leading ! or !! terminal-command prefix.

    返回开头的 `!` 或 `!!` 终端命令前缀在输入文本中的范围。
    """
    leading_whitespace = len(text) - len(text.lstrip())
    stripped = text[leading_whitespace:]
    if stripped.startswith("!!"):
        return (leading_whitespace, leading_whitespace + 2)
    if stripped.startswith("!"):
        return (leading_whitespace, leading_whitespace + 1)
    return None


def _blend_hex_colors(start: str, end: str, *, fraction: float) -> str:
    """Blend two ``#rrggbb`` colors by ``fraction``.

    按照 ``fraction`` 指定的比例混合两个 ``#rrggbb`` 颜色。
    """
    start_rgb = _hex_to_rgb(start)
    end_rgb = _hex_to_rgb(end)
    blended = tuple(
        round(start_channel + (end_channel - start_channel) * fraction)
        for start_channel, end_channel in zip(start_rgb, end_rgb, strict=True)
    )
    return f"#{blended[0]:02x}{blended[1]:02x}{blended[2]:02x}"


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Parse a six-digit hexadecimal color into its RGB channels.

    将六位十六进制颜色解析为 RGB 通道值。
    """
    value = color.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"Expected #rrggbb color, got {color!r}")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _completion_visible_line_limit(suggestions: Static) -> int:
    """Return the number of completion render lines that fit in the widget body.

    返回补全组件内容区域可容纳的渲染行数。
    """
    if suggestions.size.height > 0:
        return max(min(COMPLETION_MAX_VISIBLE_LINES, suggestions.size.height), 1)
    return COMPLETION_MAX_VISIBLE_LINES


def _visible_completion_state(
    state: CompletionState,
    *,
    max_lines: int,
    width: int | None = None,
) -> CompletionState:
    """Return a completion-state window with the selected item visible.

    返回一个包含当前选中项的补全状态窗口。
    """
    if not state.items or max_lines <= 0:
        return CompletionState()

    selected_line_limit = max(max_lines - 1, 1)
    start = 0
    while start < state.selected_index:
        candidate = CompletionState(
            items=state.items[start:],
            selected_index=state.selected_index - start,
        )
        if _completion_selected_render_line(candidate, width=width) < selected_line_limit:
            break
        start += 1

    end = len(state.items)
    while end > state.selected_index + 1:
        candidate = CompletionState(
            items=state.items[start:end],
            selected_index=state.selected_index - start,
        )
        if _completion_render_line_count(candidate, width=width) <= max_lines:
            break
        end -= 1

    while start < state.selected_index:
        candidate = CompletionState(
            items=state.items[start:end],
            selected_index=state.selected_index - start,
        )
        if _completion_render_line_count(candidate, width=width) <= max_lines:
            break
        start += 1

    return CompletionState(
        items=state.items[start:end],
        selected_index=state.selected_index - start,
    )


def _completion_selected_render_line(state: CompletionState, *, width: int | None = None) -> int:
    """Return the rendered line number for the selected completion item.

    返回选中补全项对应的渲染行号。
    """
    line = 0
    has_rendered_text = False
    previous_category: str | None = None
    for index, item in enumerate(state.items):
        if item.category != previous_category:
            if has_rendered_text:
                line += 1
            if item.category:
                line += 1
                has_rendered_text = True
            previous_category = item.category
        elif has_rendered_text:
            line += 1
        if index == state.selected_index:
            return line
        line += _completion_item_extra_wrapped_lines(item, width=width)
        has_rendered_text = True
    return line


def _completion_render_line_count(state: CompletionState, *, width: int | None = None) -> int:
    """Return how many lines the completion state renders into.

    返回补全状态渲染后所占的行数。
    """
    if not state.items:
        return 0
    line_count = 0
    previous_category: str | None = None
    for index, item in enumerate(state.items):
        if item.category != previous_category:
            if index:
                line_count += 1
            if item.category:
                line_count += 1
            previous_category = item.category
        line_count += 1 + _completion_item_extra_wrapped_lines(item, width=width)
    return line_count


def _completion_item_extra_wrapped_lines(
    item: CompletionItem,
    *,
    width: int | None,
) -> int:
    """Return extra rendered lines used when a completion description wraps.

    返回补全描述换行后额外占用的渲染行数。
    """
    if width is None or width <= 0 or not item.description:
        return 0
    output = StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=False,
        color_system=None,
        legacy_windows=False,
    )
    console.print(
        render_completion_suggestions(
            CompletionState(items=(item,), selected_index=0),
            theme=TAU_DARK_THEME,
        ),
        end="",
    )
    line_count = len(output.getvalue().splitlines())
    return max(line_count - 1, 0)


def _session_command_registry(session: CodingSession) -> CommandRegistry:
    """Return the session command registry or construct the default registry.

    返回会话命令注册表；若没有则创建默认注册表。
    """
    registry = getattr(session, "command_registry", None)
    if isinstance(registry, CommandRegistry):
        return registry
    return create_default_command_registry()


def _session_options(session: CodingSession) -> tuple[CompletionOption, ...]:
    """Convert resumable sessions into prompt-completion options.

    将可恢复会话转换为提示补全选项。
    """
    return tuple(_session_option(record) for record in _session_records(session))


def _local_session_records(session: CodingSession) -> tuple[SessionCompletionRecord, ...]:
    """Return only the current project's indexed sessions.

    仅返回当前项目已索引的会话。
    """
    manager = getattr(session, "session_manager", None)
    if manager is None:
        return ()
    try:
        records = manager.list_sessions(session.cwd)
    except TypeError:
        records = manager.list_sessions()
    local_cwd = Path(session.cwd).resolve()
    return tuple(record for record in records if Path(record.cwd).resolve() == local_cwd)


def _session_records(session: CodingSession) -> tuple[SessionCompletionRecord, ...]:
    """Return indexed sessions for resume, current directory first.

    返回可恢复的已索引会话，并优先列出当前目录中的会话。

    Sessions from the session's working directory are listed newest-first,
    followed by sessions from every other directory (also newest-first), so a
    session from another project is visible in the picker without leaving it.

    会话工作目录中的会话按更新时间从新到旧排列，随后列出其他目录中的会话，
    也按更新时间从新到旧排列。这样无需离开当前项目，也能在选择器中找到其他项目的会话。
    """
    manager = getattr(session, "session_manager", None)
    if manager is None:
        return ()
    try:
        records = list(manager.list_sessions())
    except TypeError:
        # Older managers only accept an explicit cwd argument.
        # 较旧的管理器只接受显式传入的 cwd 参数。
        records = list(manager.list_sessions(session.cwd))
    local_cwd = Path(session.cwd).resolve()
    local = [record for record in records if Path(record.cwd).resolve() == local_cwd]
    other = [record for record in records if Path(record.cwd).resolve() != local_cwd]
    return tuple(local) + tuple(other)


def _session_option(record: SessionCompletionRecord) -> CompletionOption:
    """Build a completion option from the session's title, model, and directory.

    根据会话标题、模型和目录创建补全选项。
    """
    description_parts = [record.title if record.title else "Untitled session"]
    if record.model:
        description_parts.append(record.model)
    description_parts.append(_short_path(record.cwd))
    return CompletionOption(value=record.id, description=" - ".join(description_parts))


def _short_path(path: Path) -> str:
    """Shorten a path beneath the home directory with a leading ``~``.

    将主目录下的路径缩写为以 ``~`` 开头的形式。
    """
    home = Path.home()
    try:
        relative = path.relative_to(home)
        return "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        return str(path)


def _session_picker_label(record: SessionCompletionRecord) -> str:
    # The project column provides directory context. Keep the model last so it
    # truncates before the relative age and title.
    # 项目列提供目录上下文。模型信息放在最后，这样截断时会优先省略模型，保留
    # 相对时间和会话标题。
    parts = [_session_updated_at_label(record.updated_at)]
    title = _named_session_title(record.title)
    if title is not None:
        parts.append(title)
    if record.model:
        parts.append(record.model)
    return "  ".join(parts)


def _filter_session_records(
    records: Sequence[SessionCompletionRecord],
    query: str,
) -> tuple[SessionCompletionRecord, ...]:
    """Filter sessions by a case-insensitive match in title or model name.

    按标题或模型名称进行不区分大小写的会话筛选。
    """
    normalized = query.strip().casefold()
    if not normalized:
        return tuple(records)
    return tuple(
        record
        for record in records
        if normalized in (record.title or "").casefold() or normalized in record.model.casefold()
    )


def _tree_picker_label(
    choice: SessionTreeChoice,
    *,
    theme: TuiTheme,
    highlighted: bool = False,
    show_label_timestamp: bool = False,
) -> Text:
    """Format a session-tree row with active and bookmark styling.

    使用活动状态和书签样式格式化会话树中的一行。
    """
    marker = "* " if choice.active else "  "
    label = choice.label
    indent_width = len(label) - len(label.lstrip(" "))
    indent = label[:indent_width]
    body = label[indent_width:]
    author, separator, rest = body.partition(":")
    text = Text(f"{marker}{indent}")
    if choice.bookmark_label is not None:
        bookmark_color = theme.highlight_text if highlighted else theme.success
        text.append(f"[{choice.bookmark_label}] ", style=bookmark_color)
        if show_label_timestamp and choice.label_timestamp is not None:
            timestamp = datetime.fromtimestamp(choice.label_timestamp).strftime("%Y-%m-%d %H:%M")
            timestamp_color = theme.highlight_text if highlighted else theme.muted_text
            text.append(f"{timestamp} ", style=timestamp_color)
    if separator:
        author_color = theme.highlight_text if highlighted else theme.accent
        text.append(author, style=author_color)
        text.append(f"{separator}{rest}")
    else:
        text.append(body)
    return text


def _active_tree_choice_index(choices: Sequence[SessionTreeChoice]) -> int:
    """Return the index of the active tree choice, defaulting to the first row.

    返回活动树选项的索引；找不到时默认使用首行。
    """
    return _tree_choice_index(choices, None)


def _tree_choice_index(choices: Sequence[SessionTreeChoice], entry_id: str | None) -> int:
    """Find an entry by ID, then fall back to the active row or first row.

    按 ID 查找条目；未找到时回退到活动行或首行。
    """
    if entry_id is not None:
        for index, choice in enumerate(choices):
            if choice.entry_id == entry_id:
                return index
    for index, choice in enumerate(choices):
        if choice.active:
            return index
    return 0


def _session_updated_at_label(timestamp: float) -> str:
    """Return a compact relative age label for a session (e.g. ``2h ago``).

    返回简洁的会话相对时间标签（例如 ``2h ago``）。
    """
    delta = (datetime.now() - datetime.fromtimestamp(timestamp)).total_seconds()
    minutes = int(delta // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    return datetime.fromtimestamp(timestamp).strftime("%b %d")


def _named_session_title(title: str | None) -> str | None:
    """Return a meaningful session title, excluding blank and default names.

    返回有效的会话标题，排除空标题和默认名称。
    """
    if title is None:
        return None
    stripped = title.strip()
    if not stripped or stripped.lower() == "untitled session":
        return None
    return stripped


def _login_provider_label(provider: ProviderCatalogEntry) -> str:
    """Format a provider entry as its display name and identifier.

    将提供者条目格式化为显示名称和标识符。
    """
    return f"{provider.display_name} — {provider.name}"


def _subscription_login_providers(
    providers: Sequence[ProviderCatalogEntry],
) -> tuple[ProviderCatalogEntry, ...]:
    """Select providers that support OAuth subscription login.

    筛选支持 OAuth 订阅登录的提供者。
    """
    provider_ids = oauth_provider_ids()
    return tuple(provider for provider in providers if provider.name in provider_ids)


def _api_key_login_providers(
    providers: Sequence[ProviderCatalogEntry],
) -> tuple[ProviderCatalogEntry, ...]:
    """Select providers that support API-key authentication.

    筛选支持 API 密钥认证的提供者。
    """
    return tuple(provider for provider in providers if "api_key" in provider.auth_methods)


def _stored_credential_providers(
    providers: Sequence[ProviderCatalogEntry],
) -> tuple[ProviderCatalogEntry, ...]:
    """Select providers with credentials already stored locally.

    筛选本地已保存凭据的提供者。
    """
    credential_store = FileCredentialStore()
    return tuple(
        provider
        for provider in providers
        if provider.credential_name is not None
        and _credential_store_has_entry(credential_store, provider.credential_name)
    )


def _credential_store_has_entry(
    credential_store: FileCredentialStore,
    credential_name: str,
) -> bool:
    """Return whether a credential name has an API key or OAuth value.

    判断指定凭据名称下是否保存了 API 密钥或 OAuth 凭据。
    """
    return (
        credential_store.get(credential_name) is not None
        or credential_store.get_oauth(credential_name) is not None
    )


def _theme_picker_label(theme_name: TuiThemeName, *, current_theme: TuiThemeName) -> str:
    """Mark the current theme in the theme picker label.

    在主题选择器标签中标记当前主题。
    """
    marker = "✓" if theme_name == current_theme else " "
    return f"{marker} {theme_name}"


def _model_picker_label(
    choice: ModelChoice,
    *,
    current_model: str,
    current_provider: str,
    scoped: bool = False,
    unavailable: bool = False,
) -> str:
    """Format a model choice with current, scoped, and unavailable markers.

    使用当前、作用域限定和不可用标记格式化模型选项。
    """
    marker = (
        "* "
        if (choice.provider_name == current_provider and choice.model == current_model)
        else "  "
    )
    suffix = (" [scoped]" if scoped else "") + (" [unavailable]" if unavailable else "")
    return f"{marker}{choice.provider_name}:{choice.model}{suffix}"


def _filter_login_providers(
    providers: Sequence[ProviderCatalogEntry],
    query: str,
) -> tuple[ProviderCatalogEntry, ...]:
    """Filter provider choices by name or display name.

    按提供者标识符或显示名称筛选选项。
    """
    normalized = query.strip().casefold()
    if not normalized:
        return tuple(providers)
    return tuple(
        provider
        for provider in providers
        if normalized in provider.name.casefold() or normalized in provider.display_name.casefold()
    )


def _filter_model_choices(choices: Sequence[ModelChoice], query: str) -> tuple[ModelChoice, ...]:
    """Filter model choices by provider name or model identifier.

    按提供者名称或模型标识符筛选模型选项。
    """
    normalized = query.strip().lower()
    if not normalized:
        return tuple(choices)
    return tuple(
        choice
        for choice in choices
        if normalized in choice.provider_name.lower() or normalized in choice.model.lower()
    )


def _command_message_uses_transcript(command_text: str) -> bool:
    """Return whether slash-command output should appear inline in the transcript.

    判断斜杠命令输出是否应以内联形式显示在会话记录中。
    """
    command_name = command_text.split(maxsplit=1)[0].casefold()
    return command_name in {"/reload", "/system"}


def _command_message_uses_notification(command_text: str, message: str) -> bool:
    """Return whether slash-command output should appear as a notification.

    判断斜杠命令输出是否应作为通知显示。
    """
    command_name = command_text.split(maxsplit=1)[0].casefold()
    return command_name == "/name" and message.startswith("Session renamed: ")


def _command_output_title(command_text: str) -> str:
    """Choose a concise title for output from a slash command.

    为斜杠命令输出选择简洁标题。
    """
    command_name = command_text.split(maxsplit=1)[0].removeprefix("/")
    return f"/{command_name or 'help'}"


def _is_thinking_cycle_key(key: str, configured_key: str) -> bool:
    """Return whether a pressed key matches the configured thinking shortcut.

    判断按下的按键是否匹配已配置的思考级别快捷键。
    """
    if key == configured_key:
        return True
    return configured_key == "shift+tab" and key == "backtab"


def _render_queued_messages(state: TuiState, *, theme: TuiTheme) -> Group:
    """Render queued prompts stacked above the prompt input.

    将排队中的提示词逐条渲染在提示输入框上方。
    """
    rows: list[Text] = []
    for message in state.queued_steering:
        row = Text("↪ steering · queued: ", style=theme.muted_text)
        row.append(_queued_message_preview(message), style=theme.prompt_text)
        rows.append(row)
    for message in state.queued_follow_up:
        row = Text("↳ follow-up · queued: ", style=theme.muted_text)
        row.append(_queued_message_preview(message), style=theme.prompt_text)
        rows.append(row)
    return Group(*rows)


def _queued_message_preview(message: str) -> str:
    """Return the single-line preview shown above the prompt.

    返回显示在提示输入框上方的单行预览。
    """
    lines = message.splitlines()
    return lines[0] if lines else ""


def _prompt_footer_mode(
    completion_state: CompletionState,
    *,
    working: bool,
) -> Literal["normal", "completion", "file_completion", "running"]:
    """Choose the footer mode from completion selection and run state.

    根据补全选择和运行状态决定提示栏模式。
    """
    selected = completion_state.selected
    if selected is not None:
        if selected.kind is CompletionKind.FILE_REFERENCE:
            return "file_completion"
        return "completion"
    if working:
        return "running"
    return "normal"


def _key_hint(key: str) -> str:
    """Format a configured key combination for display in shortcut hints.

    将已配置的组合键格式化为快捷键提示文本。
    """
    return "+".join(part.capitalize() for part in key.split("+"))


def _app_bindings(keybindings: TuiKeybindings) -> list[Binding]:
    """Build app-level Textual bindings from the durable key settings.

    根据持久化按键设置构建应用级 Textual 绑定。
    """
    return [
        Binding(keybindings.cancel, "cancel", "Cancel"),
        Binding(keybindings.command_palette, "open_command_palette", "Commands"),
        Binding(keybindings.session_picker, "open_session_picker", "Sessions"),
        Binding(keybindings.thinking_cycle, "cycle_thinking", "Thinking"),
        Binding(keybindings.model_cycle, "cycle_model", "Model"),
        Binding(
            keybindings.model_cycle_reverse,
            "cycle_model_reverse",
            "Previous model",
            show=False,
        ),
        Binding(
            keybindings.accept_completion,
            "accept_completion",
            "Complete",
            priority=True,
        ),
        Binding(
            keybindings.queue_follow_up,
            "submit_follow_up",
            "Follow-up",
            priority=True,
        ),
        Binding(
            keybindings.completion_next,
            "completion_next",
            "Next completion",
            priority=True,
        ),
        Binding(
            keybindings.completion_previous,
            "completion_previous",
            "Previous completion",
            priority=True,
        ),
        Binding(keybindings.toggle_tool_results, "toggle_tool_results", "Tool results"),
        Binding(keybindings.toggle_thinking, "toggle_thinking", "Thinking tokens"),
        Binding(keybindings.copy_message, "clear_prompt", "Clear input"),
        Binding(keybindings.quit, "quit", "Quit"),
    ]


def _prompt_bindings(
    keybindings: TuiKeybindings,
    *,
    mode: Literal["normal", "completion", "file_completion", "running"],
) -> list[Binding]:
    """Build prompt-editor bindings for its current interaction mode.

    为提示编辑器当前的交互模式构建按键绑定。
    """
    if mode in {"completion", "file_completion"}:
        bindings = [
            Binding(
                keybindings.accept_completion,
                "accept_completion",
                "Complete",
                key_display=(
                    _key_hint(keybindings.accept_completion)
                    if mode == "file_completion"
                    else f"{_key_hint(keybindings.accept_completion)}/Enter"
                ),
                priority=True,
            ),
            Binding(
                keybindings.completion_next,
                "completion_next",
                "Choose",
                key_display=(
                    f"{_key_hint(keybindings.completion_previous)}/"
                    f"{_key_hint(keybindings.completion_next)}"
                ),
                priority=True,
            ),
            Binding(keybindings.cancel, "cancel", "Close", priority=True),
        ]
        if mode == "file_completion":
            bindings.insert(1, Binding("enter", "submit_prompt", "Submit raw", priority=True))
        return bindings + _hidden_prompt_bindings(keybindings, visible_bindings=bindings)
    if mode == "running":
        bindings = [
            Binding("enter", "submit_prompt", "Steer", priority=True),
            Binding(keybindings.queue_follow_up, "submit_follow_up", "Follow-up", priority=True),
            Binding(keybindings.cancel, "cancel", "Cancel", priority=True),
            Binding(
                keybindings.toggle_thinking,
                "toggle_thinking",
                "Thinking",
                priority=True,
            ),
            Binding(
                keybindings.toggle_tool_results,
                "toggle_tool_results",
                "Tools",
                priority=True,
            ),
        ]
        return bindings + _hidden_prompt_bindings(keybindings, visible_bindings=bindings)
    bindings = [
        Binding("enter", "submit_prompt", "Submit", priority=True),
        Binding(
            keybindings.insert_newline,
            "insert_newline",
            "Newline",
            priority=True,
        ),
        Binding(keybindings.command_palette, "open_command_palette", "Commands", priority=True),
        Binding(keybindings.session_picker, "open_session_picker", "Sessions", priority=True),
        Binding(keybindings.thinking_cycle, "cycle_thinking", "Thinking", priority=True),
        Binding(keybindings.model_cycle, "cycle_model", "Model", priority=True),
        Binding(
            keybindings.model_cycle_reverse,
            "cycle_model_reverse",
            "Previous model",
            show=False,
            priority=True,
        ),
        Binding(
            keybindings.copy_message,
            "clear_prompt",
            "Clear",
            priority=True,
        ),
        Binding(keybindings.quit, "quit", "Quit", priority=True),
    ]
    return bindings + _hidden_prompt_bindings(keybindings, visible_bindings=bindings)


def _hidden_prompt_bindings(
    keybindings: TuiKeybindings,
    *,
    visible_bindings: Sequence[Binding],
) -> list[Binding]:
    """Add hidden bindings for configured actions absent from the visible footer.

    为已配置但未显示在提示栏中的操作添加隐藏绑定。
    """
    visible_keys = {key for binding in visible_bindings for key in binding.key.split(",")}
    candidates = (
        (keybindings.command_palette, "open_command_palette"),
        (keybindings.session_picker, "open_session_picker"),
        (keybindings.queue_follow_up, "submit_follow_up"),
        (keybindings.insert_newline, "insert_newline"),
        (keybindings.thinking_cycle, "cycle_thinking"),
        (keybindings.model_cycle, "cycle_model"),
        (keybindings.model_cycle_reverse, "cycle_model_reverse"),
        (keybindings.toggle_tool_results, "toggle_tool_results"),
        (keybindings.toggle_thinking, "toggle_thinking"),
        (keybindings.copy_message, "clear_prompt"),
        (keybindings.accept_completion, "accept_completion"),
        (keybindings.completion_next, "completion_next"),
        (keybindings.completion_previous, "completion_previous"),
        (keybindings.quit, "quit"),
    )
    return [
        Binding(key, action, show=False, priority=True)
        for key, action in candidates
        if key not in visible_keys
    ]


def _text_end_location(text: str) -> tuple[int, int]:
    """Return the TextArea cursor location at the end of text.

    返回文本末尾对应的 TextArea 光标位置。
    """
    line, _, column_text = text.rpartition("\n")
    return (line.count("\n") + 1 if line else 0, len(column_text))


def _format_prompt_error(exc: BaseException, session: CodingSession) -> str:
    """Format an exception and append the diagnostic log path when available.

    格式化异常；如果诊断日志路径可用，则一并附上。
    """
    detail = str(exc) or type(exc).__name__
    message = f"Error: {detail}"
    log_path = getattr(session, "last_diagnostic_log_path", None)
    if isinstance(log_path, Path):
        return f"{message}\nLog: {log_path}"
    return message


_TERMINAL_ERROR_RETRY_HINT = "Run ended before completion. Send a message to retry."


def _attach_retry_hint_to_error(state: TuiState, message: AssistantMessage) -> None:
    """Clarify that a terminal provider error ended the run and can be retried.

    说明终止性提供者错误已结束本轮运行，用户可以重试。

    Context-overflow errors are auto-compacted and retried by the session, so
    they are skipped to avoid asking the user to retry while Tau already is.

    上下文溢出错误会由会话自动压缩并重试，因此此处跳过它们，避免 Tau 正在重试时
    又提示用户手动重试。
    """
    if is_context_overflow_error(message):
        return
    if state.error is not None and _TERMINAL_ERROR_RETRY_HINT not in state.error:
        state.error = f"{state.error}\n{_TERMINAL_ERROR_RETRY_HINT}"
    for item in reversed(state.items):
        if item.role == "error":
            if _TERMINAL_ERROR_RETRY_HINT not in item.text:
                item.text = f"{item.text}\n{_TERMINAL_ERROR_RETRY_HINT}"
            return


def _attach_diagnostic_log_path_to_error(state: TuiState, session: CodingSession) -> None:
    """Add the diagnostic log path to the error state and transcript item.

    将诊断日志路径添加到错误状态和会话记录项中。
    """
    log_path = getattr(session, "last_diagnostic_log_path", None)
    if not isinstance(log_path, Path) or state.error is None:
        return
    message = f"Error: {state.error}\nLog: {log_path}"
    state.error = message
    for item in reversed(state.items):
        if item.role == "error":
            item.text = message
            return
    state.add_item("error", message)


def _explicit_resume_record(
    manager: SessionManager,
    *,
    session_id: str | None,
) -> CodingSessionRecord | None:
    """Resolve the explicitly requested session record or raise if unknown.

    查找显式指定的会话记录；若 ID 未知则抛出异常。
    """
    if session_id is None:
        return None
    record = manager.get_session(session_id)
    if record is None:
        raise RuntimeError(f"Unknown session: {session_id}")
    return record


def _create_startup_session_record(
    manager: SessionManager,
    *,
    cwd: Path,
    selection: ProviderSelection,
    inference_provider: str | None = None,
) -> CodingSessionRecord:
    """Prepare a session record while supporting older manager signatures.

    准备会话记录，并兼容旧版管理器的方法签名。
    """
    if inference_provider is None:
        return manager.prepare_session(
            cwd=cwd,
            model=selection.model,
            provider_name=selection.provider.name,
        )
    try:
        return manager.prepare_session(
            cwd=cwd,
            model=selection.model,
            provider_name=selection.provider.name,
            inference_provider=inference_provider,
            inference_provider_mode="fixed",
        )
    except TypeError:
        try:
            return manager.prepare_session(
                cwd=cwd,
                model=selection.model,
                provider_name=selection.provider.name,
                inference_provider=inference_provider,
            )
        except TypeError:
            return manager.prepare_session(
                cwd=cwd,
                model=selection.model,
                provider_name=selection.provider.name,
            )


def _resolve_tui_startup_selection(
    settings: Any,
    *,
    record: Any | None,
    provider_name: str | None,
    model: str | None,
    explicit_resume: bool,
) -> ProviderSelection:
    """Resolve the startup provider/model from explicit, resumed, or default state.

    根据显式参数、待恢复会话或默认配置解析启动提供者和模型。
    """
    if provider_name is not None or model is not None:
        return resolve_provider_selection(settings, provider_name=provider_name, model=model)

    if explicit_resume:
        record_selection = _selection_from_session_record(settings, record)
        if record_selection is not None:
            return record_selection

    default_selection = resolve_provider_selection(settings)
    if provider_has_usable_credentials(
        default_selection.provider,
        credential_reader=FileCredentialStore(),
    ):
        return default_selection

    fallback_selection = _first_usable_startup_selection(settings)
    return fallback_selection or default_selection


def _first_usable_startup_selection(settings: Any) -> ProviderSelection | None:
    """Return the first configured provider with usable stored credentials.

    返回首个具有可用存储凭据的已配置提供者。
    """
    credential_store = FileCredentialStore()
    for provider in settings.providers:
        if provider_has_usable_credentials(provider, credential_reader=credential_store):
            return ProviderSelection(provider=provider, model=provider.default_model)
    return None


def _selection_from_session_record(settings: Any, record: Any | None) -> ProviderSelection | None:
    """Reconstruct a usable provider/model selection from a saved session.

    根据已保存会话重建可用的提供者和模型选择。
    """
    if record is None:
        return None
    record_model = getattr(record, "model", None)
    if not isinstance(record_model, str) or not record_model:
        return None

    record_provider = getattr(record, "provider_name", None)
    if isinstance(record_provider, str) and record_provider:
        try:
            return resolve_provider_selection(
                settings,
                provider_name=record_provider,
                model=record_model,
            )
        except Exception:
            return None

    for choice in _usable_scoped_startup_choices(settings):
        if choice.model == record_model:
            return resolve_provider_selection(
                settings,
                provider_name=choice.provider_name,
                model=choice.model,
            )

    credential_store = FileCredentialStore()
    for provider in settings.providers:
        if record_model not in provider.models:
            continue
        if not provider_has_usable_credentials(provider, credential_reader=credential_store):
            continue
        return ProviderSelection(provider=provider, model=record_model)
    return None


def _usable_scoped_startup_choices(settings: Any) -> tuple[ModelChoice, ...]:
    """Return scoped choices whose model and provider credentials are valid.

    返回模型与提供者凭据均有效的作用域选项。
    """
    credential_store = FileCredentialStore()
    choices: list[ModelChoice] = []
    for item in settings.scoped_models:
        try:
            provider = settings.get_provider(item.provider)
        except Exception:
            continue
        if item.model not in provider.models:
            continue
        if not provider_has_usable_credentials(provider, credential_reader=credential_store):
            continue
        choices.append(ModelChoice(provider_name=item.provider, model=item.model))
    return tuple(choices)


def _resource_conflict_alert(
    diagnostics: Sequence[ResourceDiagnostic],
) -> str | None:
    """Format skill and prompt precedence conflicts as one startup alert.

    将技能和提示模板的优先级冲突整理为一条启动提醒。
    """
    prefix = "overrides lower-precedence resource at "
    conflicts = [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic.kind in {"skill", "prompt"}
        and diagnostic.name is not None
        and diagnostic.path is not None
        and diagnostic.message.startswith(prefix)
    ]
    if not conflicts:
        return None

    lines = ["Conflicting skills/prompts detected:"]
    for diagnostic in conflicts:
        resource_kind = "skill" if diagnostic.kind == "skill" else "prompt template"
        shadowed_path = diagnostic.message.removeprefix(prefix)
        lines.append(
            f"- {resource_kind} '{diagnostic.name}': {diagnostic.path} overrides {shadowed_path}"
        )
    lines.append("Rename or remove duplicate resources to clear this alert.")
    return "\n".join(lines)


def _startup_inference_provider(
    selection: ProviderSelection,
    record: CodingSessionRecord | None,
) -> str | None:
    """Restore a saved or configured inference route for Hugging Face models.

    为 Hugging Face 模型恢复已保存或已配置的推理路由。
    """
    provider = selection.provider
    if not isinstance(provider, OpenAICompatibleProviderConfig) or provider.name != "huggingface":
        return None
    if record is not None and record.model == selection.model:
        return record.inference_provider
    return provider.inference_providers.get(selection.model)


def _startup_inference_provider_mode(
    selection: ProviderSelection,
    record: CodingSessionRecord | None,
) -> Literal["automatic", "fixed"]:
    """Return the saved route mode or infer it from the configured route.

    返回已保存的路由模式，或根据配置路由推断模式。
    """
    if record is not None and record.model == selection.model:
        return record.inference_provider_mode
    return "fixed" if _startup_inference_provider(selection, None) is not None else "automatic"


async def run_tui_app(
    *,
    model: str | None,
    cwd: Path,
    session_id: str | None = None,
    new_session: bool = False,
    provider_name: str | None = None,
    auto_compact_token_threshold: int | None = None,
    initial_prompt: str | None = None,
    session_manager: SessionManager | None = None,
    startup_notice: str | None = None,
    startup_update_notice: str | None = None,
    startup_notices: Sequence[str] = (),
    extension_paths: tuple[Path, ...] = (),
    extensions_enabled: bool = True,
    project_extensions_enabled: bool = False,
    custom_system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    trust_override: TrustOverride | None = None,
    thinking_level_override: ThinkingLevel | None = None,
) -> str | None:
    """Run the Textual app and return the active id when its session is persisted.

    运行 Textual 应用，并在会话持久化后返回当前会话 ID。
    """
    _configure_herdr_textual_mouse()
    if new_session and session_id is not None:
        raise RuntimeError("--session and --new-session cannot be used together")

    provider_settings = load_provider_settings()
    shell_settings = load_shell_settings()
    manager = session_manager or SessionManager()
    record = _explicit_resume_record(
        manager,
        session_id=session_id,
    )
    selection: ProviderSelection | None = None
    try:
        selection = _resolve_tui_startup_selection(
            provider_settings,
            record=record,
            provider_name=provider_name,
            model=model,
            explicit_resume=session_id is not None,
        )
    except ProviderConfigError:
        # A resumed record may point at a process-local provider that is not in
        # durable settings. Let the staged loader resolve it after trusted
        # built-in/project extensions are loaded.
        #
        # 恢复记录可能指向未写入持久化设置的进程内提供者。待可信的内置或项目扩展加载后，
        # 再由暂存加载器解析该提供者。
        dynamic_resume = (
            session_id is not None
            and record is not None
            and record.provider_name is not None
            and provider_name is None
            and model is None
        )
        explicit_dynamic = provider_name is not None and model is not None
        if not dynamic_resume and not explicit_dynamic:
            raise
    startup_message: str | None = None
    startup_error_notice: str | None = None
    explicit_selection = provider_name is not None or model is not None
    selected_provider_name: str = (
        provider_name
        if provider_name is not None
        else (record.provider_name if record is not None else None)
        or (selection.provider.name if selection is not None else DEFAULT_PROVIDER_NAME)
    )
    selected_model = (
        model
        if explicit_selection and model is not None
        else (record.model if record is not None else None)
        or (selection.model if selection is not None else DEFAULT_MODEL)
    )
    # Keep static-provider construction compatible with embedded TUI callers,
    # while dynamic providers are deliberately left for CodingSession.load()
    # after trusted extension setup. The provider passed below is owned by the
    # prepared session when the real loader is used.
    # 保持静态提供者构造方式与嵌入式 TUI 调用方兼容；动态提供者则延后到
    # `CodingSession.load()` 在可信扩展设置完成后再解析。真实加载器运行时，
    # 传入的提供者由准备好的会话负责管理。
    initial_provider: ClosableModelProvider | None = None
    runtime_provider_config: ProviderConfig | None = selection.provider if selection else None
    inference_provider = _startup_inference_provider(selection, record) if selection else None
    inference_provider_mode: Literal["automatic", "fixed"] = (
        _startup_inference_provider_mode(selection, record) if selection else "automatic"
    )
    if selection is not None:
        try:
            initial_provider = create_model_provider(
                selection.provider,
                model=selection.model,
                inference_provider=inference_provider,
                thinking_level=resolve_startup_thinking_level(
                    selection.provider,
                    selection.model,
                    cli_override=thinking_level_override,
                ),
            )
        except RuntimeError as exc:
            login_required_message = (
                "Login required. Run /login to choose a provider, "
                f"or /login {selected_provider_name} to continue with the current provider."
            )
            startup_message = f"{login_required_message}\n\nStartup error: {exc}"
            startup_error_notice = (
                f"Startup provider creation failed for "
                f"{selection.provider.name}:{selection.model}: {exc}"
            )
            initial_provider = LoginRequiredProvider(startup_message)
            runtime_provider_config = None
    elif not explicit_selection:
        startup_message = (
            "Login required. Run /login to choose a provider, "
            f"or /login {selected_provider_name} to continue with the current provider."
        )
        initial_provider = LoginRequiredProvider(startup_message)
    session: CodingSession | None = None
    try:
        index_on_first_persist = False
        if record is None:
            if selection is not None:
                record = _create_startup_session_record(
                    manager,
                    cwd=cwd,
                    selection=selection,
                    inference_provider=inference_provider,
                )
            else:
                if provider_name is None or model is None:
                    raise ProviderConfigError(
                        "An explicit provider and model are required for this startup."
                    )
                record = manager.prepare_session(
                    cwd=cwd,
                    model=model,
                    provider_name=provider_name,
                )
            index_on_first_persist = manager.get_session(record.id) is None

        prepared = await prepare_coding_session(
            CodingSessionConfig(
                provider=initial_provider,
                model=record.model or selected_model,
                cwd=record.cwd,
                storage=jsonl_session_storage(record.path),
                session_id=record.id,
                session_manager=manager,
                provider_name=selected_provider_name,
                inference_provider=inference_provider,
                inference_provider_mode=inference_provider_mode,
                requested_provider=provider_name if explicit_selection else None,
                requested_model=model if explicit_selection else None,
                session_provider_name=record.provider_name,
                provider_settings=provider_settings,
                runtime_provider_config=runtime_provider_config,
                auto_compact_token_threshold=auto_compact_token_threshold,
                index_on_first_persist=index_on_first_persist,
                shell_command_prefix=shell_settings.shell_command_prefix,
                extension_paths=extension_paths,
                extensions_enabled=extensions_enabled,
                project_extensions_enabled=project_extensions_enabled,
                custom_system_prompt=custom_system_prompt,
                append_system_prompt=append_system_prompt,
                thinking_level_override=thinking_level_override,
                trust_override=trust_override,
                trust_default=shell_settings.default_project_trust,
                trust_interactive=True,
                trust_prompt=prompt_project_trust,
                defer_authoritative_writes=True,
                owns_initial_provider=initial_provider is not None,
            ),
            session_loader=CodingSession,
        )
        try:
            session = await prepared.adopt()
        except ValueError:
            candidate = prepared.session
            trust_resolution = getattr(candidate, "project_trust_resolution", None)
            if trust_resolution is None or not trust_resolution.cancelled:
                raise
            # The preparation object already closed the unpublished candidate.
            # Do not close that candidate again from the outer finally block.
            # 准备对象已经关闭尚未发布的候选提供者；不要再从外层 finally 块重复关闭。
            del candidate
            return None
        trust_resolution = getattr(session, "project_trust_resolution", None)
        if trust_resolution is not None and trust_resolution.cancelled:
            return None

        theme_dirs = getattr(session, "theme_dirs", None)
        if theme_dirs is None:
            trusted = trust_resolution is None or trust_resolution.trusted
            theme_dirs = TauResourcePaths(
                cwd=record.cwd,
                project_resources_enabled=trusted,
            ).themes_dirs
        custom_themes, theme_diagnostics = load_custom_tui_themes(theme_dirs)
        set_custom_tui_themes(custom_themes)
        legacy_notices = (startup_notice,) if startup_notice else ()
        error_notices = (startup_error_notice,) if startup_error_notice else ()
        theme_notices = tuple(diagnostic.format() for diagnostic in theme_diagnostics)
        all_startup_notices = tuple(
            (*error_notices, *startup_notices, *legacy_notices, *theme_notices)
        )
        resource_conflict_alert = _resource_conflict_alert(
            getattr(session, "resource_diagnostics", ())
        )
        startup_alerts = (resource_conflict_alert,) if resource_conflict_alert is not None else ()
        app = TauTuiApp(
            session,
            tui_settings=load_tui_settings(),
            startup_message=startup_message,
            startup_update_notice=startup_update_notice,
            startup_alerts=startup_alerts,
            startup_notices=all_startup_notices,
            initial_prompt=initial_prompt,
        )
        set_trust_prompt = getattr(session, "set_project_trust_prompt", None)
        if set_trust_prompt is not None:
            prompt_trust = getattr(app, "prompt_project_trust", None)
            if prompt_trust is not None:
                set_trust_prompt(prompt_trust)
        await app.run_async()
    finally:
        if session is not None:
            close_session = getattr(session, "aclose", None)
            if close_session is not None:
                await close_session()
        # Compatibility for lightweight test/embedded session loaders that do
        # not expose ownership. A real CodingSession owns the exact candidate,
        # so this branch does not double-close it.
        # 为未暴露资源所有权的轻量测试或嵌入式会话加载器提供兼容处理。真实的
        # CodingSession 拥有该候选对象，因此走此分支不会重复关闭它。
        if (
            initial_provider is not None
            and getattr(session, "provider", None) is not initial_provider
        ):
            with suppress(Exception):
                await initial_provider.aclose()

    active_session_id: str | None = getattr(session, "session_id", None)
    if active_session_id is None or manager.get_session(active_session_id) is None:
        return None
    return active_session_id
