"""Generic Textual host screens for local backends.

本地后端的通用 Textual 宿主屏幕。

This module intentionally knows only the local-backend contracts. Protocol
names and provider-specific management concepts stay in backend extensions.

此模块刻意只了解本地后端契约。协议名称和提供商专用管理概念保留在后端扩展中。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from typing import ClassVar, Literal, cast

from rich.console import Console, ConsoleOptions, RenderResult
from rich.style import StyleType
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.events import DescendantFocus, Key
from textual.renderables.bar import Bar as BarRenderable
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView, ProgressBar, Select, Static

from tau_coding.local_backends import (
    LocalAction,
    LocalBackendRegistry,
    LocalBackendStatus,
    LocalConfigureSpec,
    LocalConfigValues,
    LocalConfirmationChoice,
    LocalConfirmationRequest,
    LocalModel,
    LocalOperationResult,
    LocalProgress,
    LocalSearchResult,
    ProgressCallback,
)
from tau_coding.tui.config import TuiTheme

LocalUseCallback = Callable[[str, str], Awaitable[None] | None]
LocalNotifyCallback = Callable[[str, str], None]
LocalIdleCallback = Callable[[], bool]


class _BlockProgressRenderable(BarRenderable):
    """Render determinate progress as solid blocks over a thin track.

    在细轨道上使用实心块渲染确定性进度。
    """

    def __init__(
        self,
        highlight_range: tuple[float, float] = (0, 0),
        highlight_style: StyleType = "default",
        background_style: StyleType = "default",
        **_: object,
    ) -> None:
        """Initialize backend actions, callbacks, theme, and operation state.

        初始化后端操作、回调、主题和操作状态。
        """
        """Initialize block progress from a fraction and resolved colors.

        使用进度比例和已解析颜色初始化块状进度。
        """
        self.highlight_range = highlight_range
        self.highlight_style = highlight_style
        self.background_style = background_style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the progress track into the available Rich console width.

        将进度轨道渲染到可用的 Rich 控制台宽度。
        """
        del console
        width = options.max_width
        start, end = self.highlight_range
        bar = Text(end="")
        for cell in range(width):
            highlighted = cell + 0.5 >= start and cell + 0.5 < end
            bar.append(
                "█" if highlighted else "─",
                style=self.highlight_style if highlighted else self.background_style,
            )
        yield bar


class _LocalDownloadProgressBar(ProgressBar):
    """Textual progress bar using the local backend block renderer.

    使用本地后端块渲染器的 Textual 进度条。
    """
    BAR_RENDERABLE = _BlockProgressRenderable


class LocalBackendPickerScreen(ModalScreen[str | None]):
    """Explicitly confirm a backend choice, including when there is one.

    显式确认后端选择，即使只有一个候选项。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Select", show=False),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
    ]

    def __init__(self, registry: LocalBackendRegistry, *, theme: TuiTheme) -> None:
        """Initialize the picker from effective backends and the active theme.

        根据有效后端和活动主题初始化选择器。
        """
        super().__init__()
        self.registry = registry
        self.theme = theme
        self.views = registry.effective_backends()
        recommended = next((view for view in self.views if view.recommended), None)
        self.selected = (
            recommended.backend.id
            if recommended is not None
            else self.views[0].backend.id
            if self.views
            else None
        )

    def compose(self) -> ComposeResult:
        """Compose status, model, action, and progress sections.

        组合状态、模型、操作和进度区块。
        """
        """Compose the backend list and keyboard guidance.

        组合后端列表和键盘操作说明。
        """
        with Vertical(id="local-backend-picker"):
            yield Static("Local backends", id="local-backend-picker-title")
            if not self.views:
                yield Static("No local backends are available.", id="local-backend-empty")
            else:
                yield Static(
                    "Choose a backend. The recommended choice is marked.",
                    id="local-backend-picker-help",
                )
                yield ListView(
                    *[ListItem(Label(self._label(view), markup=False)) for view in self.views],
                    id="local-backend-list",
                )
                yield Static(
                    "↑/↓ navigate - Enter selects - Escape closes",
                    id="local-backend-picker-footer",
                )

    def on_mount(self) -> None:
        """Focus the initially selected backend when choices exist.

        存在选项时聚焦初始选中的后端。
        """
        if self.views:
            backend_list = self.query_one("#local-backend-list", ListView)
            backend_list.index = self._selected_index()
            backend_list.focus()

    def on_key(self, event: Key) -> None:
        """Keep picker navigation local despite application-wide bindings.

        即使应用存在全局绑定，也将选择器导航保持在本地。
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
        """Dismiss the picker with the selected backend identifier.

        使用选定的后端标识符关闭选择器。
        """
        event.stop()
        self.selected = self.views[event.index].backend.id
        self.dismiss(self.selected)

    def action_cursor_up(self) -> None:
        """Move the backend cursor upward and refresh selection state.

        向上移动后端光标并刷新选择状态。
        """
        self.query_one("#local-backend-list", ListView).action_cursor_up()
        self._sync_selected()

    def action_cursor_down(self) -> None:
        """Move the backend cursor downward and refresh selection state.

        向下移动后端光标并刷新选择状态。
        """
        self.query_one("#local-backend-list", ListView).action_cursor_down()
        self._sync_selected()

    def action_select_cursor(self) -> None:
        """Activate the backend under the cursor.

        激活光标所在的后端。
        """
        self.query_one("#local-backend-list", ListView).action_select_cursor()

    def action_confirm(self) -> None:
        """Confirm the currently highlighted backend.

        确认当前高亮的后端。
        """
        self.action_select_cursor()

    def action_cancel(self) -> None:
        """Dismiss the picker without choosing a backend.

        不选择后端并关闭选择器。
        """
        self.dismiss(None)

    def _sync_selected(self) -> None:
        """Synchronize the selected backend with the list cursor.

        将选定后端与列表光标同步。
        """
        if not self.views:
            self.selected = None
            return
        index = self.query_one("#local-backend-list", ListView).index
        if index is not None and 0 <= index < len(self.views):
            self.selected = self.views[index].backend.id

    def _selected_index(self) -> int:
        """Return a bounded index for the current backend selection.

        返回当前后端选择的有界索引。
        """
        if self.selected is None:
            return 0
        return next(
            (index for index, view in enumerate(self.views) if view.backend.id == self.selected),
            0,
        )

    @staticmethod
    def _label(view) -> str:  # type: ignore[no-untyped-def]
        """Build a display label for one effective backend view.

        为一个有效后端视图构建显示标签。
        """
        marker = " — Recommended" if view.recommended else ""
        effective = "" if view.use_available else " — unavailable"
        return f"{view.backend.display_name}{marker}{effective}"


class LocalBackendScreen(ModalScreen[None]):
    """Generic local-backend action screen.

    通用本地后端操作屏幕。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Close"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
        Binding("tab", "toggle_section", "Switch section", show=False),
        Binding("shift+tab", "toggle_section", "Switch section", show=False),
    ]

    def __init__(
        self,
        registry: LocalBackendRegistry,
        backend_id: str,
        *,
        theme: TuiTheme,
        on_use: LocalUseCallback | None = None,
        notify_callback: LocalNotifyCallback | None = None,
        is_idle: LocalIdleCallback | None = None,
    ) -> None:
        super().__init__()
        self.registry = registry
        self.backend_id = backend_id
        self.theme = theme
        self.on_use = on_use
        self._notify_callback = notify_callback or (lambda message, level: None)
        self._is_idle = is_idle or (lambda: True)
        self.status: LocalBackendStatus | None = None
        self._selected_model_id: str | None = None
        self._model_items: tuple[str, ...] = ()
        self._action_items: tuple[tuple[str, str], ...] = ()
        self._worker: asyncio.Task[None] | None = None
        self._active_action: LocalAction | None = None
        self._use_task: asyncio.Task[None] | None = None
        self._download_watch_task: asyncio.Task[None] | None = None
        self._progress_unsubscribe: Callable[[], None] | None = None
        self._progress_fraction: float | None = None
        self._closing = False

    def compose(self) -> ComposeResult:
        """Compose backend status, models, actions, diagnostics, and progress.

        组合后端状态、模型、操作、诊断和进度。
        """
        with Vertical(id="local-backend-screen"):
            yield Static("Local backend", id="local-backend-title")
            yield Static(
                "Choose a model or backend action.",
                id="local-backend-help",
            )
            yield Static("Looking for a local server…", id="local-backend-status")
            yield Static("Models", id="local-model-section-title")
            yield ListView(id="local-model-list")
            yield Static("Actions", id="local-action-section-title")
            yield ListView(id="local-action-menu")
            yield Static("", id="local-backend-progress")
            progress_bar = _LocalDownloadProgressBar(
                total=1,
                show_percentage=False,
                show_eta=False,
                id="local-backend-progress-bar",
            )
            progress_bar.styles.width = "100%"
            yield progress_bar
            yield Static(
                "↑/↓ navigate - Tab switches section - Enter selects - Escape closes",
                id="local-backend-footer",
            )

    async def on_mount(self) -> None:
        """Attach running downloads and refresh the explicit backend endpoint.

        连接正在运行的下载并刷新显式后端端点。
        """
        await self._render_sections(None)
        self.query_one("#local-action-menu", ListView).focus()
        self.query_one("#local-backend-progress", Static).styles.display = "none"
        progress_bar = self.query_one("#local-backend-progress-bar", ProgressBar)
        progress_bar.styles.display = "none"
        progress_bar.query_one("#bar").styles.width = "1fr"
        # Reattach to a server-owned download before probing so refresh output
        # cannot hide its replayed byte progress.
        # 探测前重新连接服务器拥有的下载，避免刷新输出隐藏其重放的字节进度。
        if self._attach_download_progress():
            self._download_watch_task = asyncio.create_task(self._watch_download_completion())
        # Opening a backend is an explicit user action, so probe its effective
        # saved/environment/default endpoint immediately. Backends still own
        # which endpoint that means; the generic host never scans. A detached
        # download observer keeps its progress visible during this refresh.
        # 打开后端属于显式用户操作，因此立即探测其有效的已保存、环境或默认端点。后端仍
        # 决定该端点的含义；通用宿主不会扫描。分离的下载观察器会在刷新期间保持进度可见。
        self._start_operation("refresh")

    def on_unmount(self) -> None:
        """Stop host-owned tasks before Textual detaches this modal.

        在 Textual 分离此模态框前停止宿主拥有的任务。
        """
        self._closing = True
        # A server-side download belongs to llama.cpp, not this modal. Closing
        # the UI only detaches from it; explicit cancellation remains available
        # from the Actions section after reopening /local.
        # 服务端下载属于 llama.cpp，而不属于此模态框。关闭 UI 只会与其分离；重新打开
        # /local 后，仍可从 Actions 区块显式取消。
        if self._active_action is not None and self._active_action != "download_model":
            self.registry.cancel(self.backend_id, self._active_action)
            if self._worker is not None and not self._worker.done():
                self._worker.cancel()
        if self._progress_unsubscribe is not None:
            self._progress_unsubscribe()
            self._progress_unsubscribe = None
        if self._download_watch_task is not None and not self._download_watch_task.done():
            self._download_watch_task.cancel()
        if self._use_task is not None and not self._use_task.done():
            self._use_task.cancel()

    def _attach_download_progress(self) -> bool:
        """Attach to an existing backend download and replay its latest progress.

        连接现有后端下载并重放其最新进度。
        """
        def progress(item: LocalProgress) -> None:
            """Forward observed download progress onto the Textual event loop.

            将观察到的下载进度转发到 Textual 事件循环。
            """
            self.call_after_refresh(self._render_observed_download_progress, item)

        self._progress_unsubscribe = self.registry.observe_progress(
            self.backend_id,
            "download_model",
            cast(ProgressCallback, progress),
        )
        return self._progress_unsubscribe is not None

    def _render_observed_download_progress(self, item: LocalProgress) -> None:
        """Render replayed download progress only while observation is current.

        仅在观察仍有效时渲染重放的下载进度。
        """
        # A replay queued during mount can run after the operation finishes.
        # Never let that stale callback restore downloading text over refreshed
        # available-model state.
        # 挂载期间排队的重放可能在操作完成后运行。绝不让过期回调用下载文本覆盖已刷新的
        # 可用模型状态。
        if self._progress_unsubscribe is not None and self.registry.operation_running(
            self.backend_id, "download_model"
        ):
            self._set_progress(item.message, fraction=item.fraction, show_bar=True)

    async def _watch_download_completion(self) -> None:
        """Wait for the observed download to finish and then refresh status.

        等待观察中的下载完成，然后刷新状态。
        """
        try:
            while self.registry.operation_running(self.backend_id, "download_model"):
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            return
        if self._progress_unsubscribe is not None:
            self._progress_unsubscribe()
            self._progress_unsubscribe = None
        if self._can_update_ui:
            worker = self._worker
            if worker is not None and not worker.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(worker)
            if self._can_update_ui:
                self._progress_fraction = None
                self._start_operation("refresh")

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        """Track which backend section currently owns keyboard focus.

        跟踪当前拥有键盘焦点的后端区块。
        """
        if event.widget.id in {"local-model-list", "local-action-menu"}:
            self._update_section_focus()

    def on_key(self, event: Key) -> None:
        """Route navigation keys within the active backend section.

        在活动后端区块内路由导航按键。
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

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """Synchronize model selection when the model highlight changes.

        模型高亮变化时同步模型选择。
        """
        if event.list_view.id == "local-model-list":
            self._sync_selected_model()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Activate the selected action or model list item.

        激活选定的操作或模型列表项。
        """
        event.stop()
        if event.list_view.id == "local-model-list":
            if 0 <= event.index < len(self._model_items):
                self._selected_model_id = self._model_items[event.index]
                self._activate_selected_model()
        elif event.list_view.id == "local-action-menu":
            self._activate_action(event.index)

    def action_cursor_up(self) -> None:
        """Move the cursor upward within the focused list.

        在聚焦列表内向上移动光标。
        """
        models = self.query_one("#local-model-list", ListView)
        actions = self.query_one("#local-action-menu", ListView)
        if actions.has_focus and self._model_items and actions.index in {None, 0}:
            models.index = len(self._model_items) - 1
            models.focus()
            self._sync_selected_model()
            return
        self._focused_list().action_cursor_up()
        self._sync_selected_model()

    def action_cursor_down(self) -> None:
        """Move the cursor downward within the focused list.

        在聚焦列表内向下移动光标。
        """
        models = self.query_one("#local-model-list", ListView)
        actions = self.query_one("#local-action-menu", ListView)
        if models.has_focus and self._action_items and models.index == len(self._model_items) - 1:
            actions.index = 0
            actions.focus()
            return
        self._focused_list().action_cursor_down()
        self._sync_selected_model()

    def action_select_cursor(self) -> None:
        """Activate the item under the focused list cursor.

        激活聚焦列表光标所在的项目。
        """
        self._focused_list().action_select_cursor()

    def action_toggle_section(self) -> None:
        """Move focus between model and action sections.

        在模型与操作区块之间移动焦点。
        """
        models = self.query_one("#local-model-list", ListView)
        actions = self.query_one("#local-action-menu", ListView)
        if models.has_focus and self._action_items:
            actions.focus()
        elif self._model_items:
            models.focus()
        self._update_section_focus()

    def _update_section_focus(self) -> None:
        """Apply focus styling to the currently active section.

        将焦点样式应用到当前活动区块。
        """
        models = self.query_one("#local-model-list", ListView)
        actions = self.query_one("#local-action-menu", ListView)
        models_focused = models.has_focus
        models.set_class(not models_focused, "local-section-inactive")
        actions.set_class(models_focused, "local-section-inactive")
        self.query_one("#local-model-section-title", Static).update(
            "Models — focused" if models_focused else "Models"
        )
        self.query_one("#local-action-section-title", Static).update(
            "Actions" if models_focused else "Actions — focused"
        )

    def _focused_list(self) -> ListView:
        """Return the list belonging to the active backend section.

        返回活动后端区块所属的列表。
        """
        models = self.query_one("#local-model-list", ListView)
        if models.has_focus:
            return models
        return self.query_one("#local-action-menu", ListView)

    def _activate_action(self, index: int) -> None:
        """Dispatch one host-defined backend action by list index.

        按列表索引分发一个宿主定义的后端操作。
        """
        if not 0 <= index < len(self._action_items):
            return
        token = self._action_items[index][0]
        if token == "configure":
            self._open_configure()
        elif token == "refresh":
            self._start_operation("refresh")
        elif token == "doctor":
            self._start_operation("doctor")
        elif token == "reset":
            self._confirm_reset()
        elif token in {"download_model", "search_models"}:
            self._open_model_action(cast(LocalAction, token))
        elif token == "cancel_download":
            self._confirm_cancel_download()

    def action_cancel(self) -> None:
        """Dismiss the backend screen and let unmount own task cancellation.

        关闭后端屏幕，并由卸载流程负责取消任务。
        """
        # on_unmount owns cancellation; let Textual begin the screen pop first.
        # on_unmount 负责取消；先让 Textual 开始弹出屏幕。
        self.dismiss(None)

    def _open_configure(self) -> None:
        """Open backend configuration without waiting for a failing probe.

        打开后端配置，而不等待失败的探测。
        """
        # Do not make users wait for an unavailable default probe before they
        # can enter a custom endpoint.
        # 不要让用户等待不可用的默认探测后才能输入自定义端点。
        if self._worker is not None and not self._worker.done():
            self.registry.cancel(self.backend_id, "refresh")
            self._worker.cancel()
        view = self.registry.effective(self.backend_id)
        if view is None:
            self._show_message("This backend is no longer available.", "error")
            return
        try:
            spec = view.backend.read_configure_spec()
        except Exception:  # noqa: BLE001 - a backend must not crash the host
            self._show_message("Could not read the backend configuration.", "error")
            return
        self.app.push_screen(
            LocalConfigureScreen(spec, theme=self.theme),
            callback=self._handle_configuration,
        )

    def _handle_configuration(self, values: LocalConfigValues | None) -> None:
        """Start configuration when the configuration modal returns values.

        配置模态框返回值时启动配置。
        """
        if values is None:
            return
        self._start_operation("configure", values=values)

    def _confirm_reset(self) -> None:
        """Open a destructive confirmation before resetting backend state.

        重置后端状态前打开破坏性确认提示。
        """
        if not self._is_idle():
            self._show_message(
                "Tau must be idle before resetting local backend settings.",
                "warning",
            )
            return
        self.app.push_screen(
            LocalConfirmScreen(
                "Reset local backend?",
                "Remove this backend's saved integration settings? Stored credentials "
                "require a separate confirmation.",
                theme=self.theme,
            ),
            callback=self._handle_reset_confirmation,
        )

    def _handle_reset_confirmation(self, confirmed: bool | None) -> None:
        """Start reset only after explicit confirmation.

        仅在明确确认后启动重置。
        """
        if confirmed:
            self._start_operation("reset")

    def _open_model_action(self, action: LocalAction) -> None:
        """Open the model-reference prompt for a backend model action.

        为后端模型操作打开模型引用提示。
        """
        if not self._is_idle():
            self._show_message(
                "Tau must be idle before changing local backend models.",
                "warning",
            )
            return
        labels = {
            "download_model": "Download model",
            "search_models": "Search Hugging Face models",
        }
        placeholders = {
            "download_model": "owner/repository[:quantization]",
            "search_models": "Hugging Face model ID or search query",
        }
        if action not in labels:
            self._show_message("Select a model from the model list first.", "warning")
            return
        self.app.push_screen(
            LocalModelActionScreen(
                labels[action],
                placeholder=placeholders[action],
                theme=self.theme,
            ),
            callback=lambda model_id: self._handle_model_action(action, model_id),
        )

    def _handle_model_action(self, action: LocalAction, model_id: str | None) -> None:
        """Start a model action when a non-empty model reference was entered.

        输入非空模型引用时启动模型操作。
        """
        if model_id is not None:
            self._start_operation(action, model_id=model_id)

    def _activate_selected_model(self) -> None:
        """Choose the appropriate action for the highlighted model state.

        根据高亮模型状态选择适当操作。
        """
        model = self._selected_model()
        if model is None or self.status is None:
            return
        if model.state in {"loaded", "sleeping", "available", None}:
            self._choose_loaded_model_action(model.id)
            return
        if model.state in {"loading", "downloading", "unknown"}:
            self._show_message(f"{model.id} is currently {model.state}.", "warning")
            return
        if "load_model" in self.status.actions:
            self._start_operation("load_model", model_id=model.id)
        else:
            self._show_message(f"{model.id} is not currently available to use.", "warning")

    def _selected_model(self) -> LocalModel | None:
        """Return the model under the current model-list cursor.

        返回当前模型列表光标所在的模型。
        """
        if self.status is None or self._selected_model_id is None:
            return None
        return next(
            (model for model in self.status.models if model.id == self._selected_model_id),
            None,
        )

    def _choose_loaded_model_action(self, model_id: str) -> None:
        """Ask whether a loaded model should be used or unloaded.

        询问应使用还是卸载已加载模型。
        """
        request = LocalConfirmationRequest(
            f"Choose an action for {model_id!r}.",
            (
                LocalConfirmationChoice("use", "Use model", True),
                LocalConfirmationChoice("unload", "Unload model"),
                LocalConfirmationChoice("cancel", "Cancel"),
            ),
        )
        self.app.push_screen(
            LocalChoiceConfirmScreen(request, theme=self.theme),
            callback=lambda choice: self._handle_loaded_model_action(model_id, choice),
        )

    def _handle_loaded_model_action(self, model_id: str, choice: str | None) -> None:
        """Dispatch the chosen action for a loaded model.

        分发针对已加载模型选择的操作。
        """
        if choice == "use":
            self._use_model(model_id)
        elif choice == "unload":
            self._start_operation("unload_model", model_id=model_id)

    def _sync_selected_model(self) -> None:
        """Synchronize the selected model identifier with the list cursor.

        将选定模型标识符与列表光标同步。
        """
        models = self.query_one("#local-model-list", ListView)
        index = models.index
        if index is not None and 0 <= index < len(self._model_items):
            self._selected_model_id = self._model_items[index]

    def _start_operation(
        self,
        action: LocalAction,
        *,
        values: Mapping[str, str] | LocalConfigValues | None = None,
        model_id: str | None = None,
        confirmation: str | None = None,
    ) -> None:
        """Start one supervised backend operation and track its UI state.

        启动一个受监管的后端操作并跟踪其 UI 状态。
        """
        if not self._is_idle():
            self._show_message(
                "Tau must be idle before changing local backend settings.",
                "warning",
            )
            return
        if self._worker is not None and not self._worker.done():
            self._show_message("An operation is already in progress.", "warning")
            return
        self._active_action = action
        self._worker = asyncio.create_task(
            self._run_operation(action, values, model_id, confirmation)
        )

    def _confirm_cancel_download(self) -> None:
        """Ask before cancelling a server-owned background download.

        取消服务器拥有的后台下载前请求确认。
        """
        request = LocalConfirmationRequest(
            "Cancel the active server-side download? Already transferred data may be discarded.",
            (
                LocalConfirmationChoice("keep", "Keep downloading"),
                LocalConfirmationChoice("cancel_download", "Cancel download"),
            ),
        )
        self.app.push_screen(
            LocalChoiceConfirmScreen(request, theme=self.theme),
            callback=self._cancel_download,
        )

    def _cancel_download(self, choice: str | None) -> None:
        """Cancel the active download when the confirmation choice allows it.

        确认选项允许时取消活动下载。
        """
        if choice != "cancel_download":
            return
        if self.registry.cancel(self.backend_id, "download_model"):
            self._show_message("Download cancellation requested.", "warning")
        else:
            self._show_message("No active download was found.", "warning")

    async def _run_operation(
        self,
        action: LocalAction,
        values: Mapping[str, str] | LocalConfigValues | None,
        model_id: str | None,
        confirmation: str | None = None,
    ) -> None:
        """Execute one backend operation and render progress, result, and status.

        执行一个后端操作并渲染进度、结果和状态。
        """
        self._active_action = action
        preserve_download_progress = action == "refresh" and self._progress_unsubscribe is not None
        if not preserve_download_progress:
            self._progress_fraction = None
            self._set_progress("Working…", show_bar=action == "download_model")
        if action == "download_model" and confirmation == "download":
            await self._render_sections(self.status)

        def progress(item: LocalProgress) -> None:
            """Forward operation progress to the current modal generation.

            将操作进度转发到当前模态框代际。
            """
            if not preserve_download_progress:
                self._set_progress(
                    item.message,
                    fraction=item.fraction,
                    show_bar=action == "download_model",
                )

        try:
            if action == "configure":
                assert values is not None
                result = await self.registry.configure(
                    self.backend_id,
                    values,
                    progress=cast(ProgressCallback, progress),
                )
            elif action == "refresh":
                result = await self.registry.refresh(
                    self.backend_id,
                    progress=cast(ProgressCallback, progress),
                )
            elif action == "doctor":
                result = await self.registry.doctor(
                    self.backend_id,
                    progress=cast(ProgressCallback, progress),
                )
            elif action == "reset":
                result = await self.registry.reset(
                    self.backend_id,
                    progress=cast(ProgressCallback, progress),
                )
            elif action in {"load_model", "unload_model", "download_model"}:
                assert model_id is not None
                manage_action = cast(
                    Literal["load_model", "unload_model", "download_model"], action
                )
                result = await self.registry.manage_model(
                    self.backend_id,
                    manage_action,
                    model_id,
                    progress=cast(ProgressCallback, progress),
                    confirmation=confirmation,
                )
            elif action == "search_models":
                assert model_id is not None
                result = await self.registry.search_models(
                    self.backend_id,
                    model_id,
                    progress=cast(ProgressCallback, progress),
                )
            else:
                result = LocalOperationResult(message="Unsupported action.")
        except asyncio.CancelledError:
            self._active_action = None
            return
        except Exception as exc:  # noqa: BLE001 - host keeps modal alive
            self._active_action = None
            if self._can_update_ui:
                self._show_message(f"Could not complete action: {type(exc).__name__}", "error")
            return
        self._active_action = None
        if not self._can_update_ui:
            return
        if result.stale:
            self._show_message("The backend changed while this action was running.", "warning")
            return
        if result.cancelled:
            self._show_message("Action cancelled.", "warning")
            return
        if result.backend_status is not None:
            self.status = result.backend_status
            await self._render_status(result.backend_status)
        if result.confirmation is not None:
            self._open_operation_confirmation(result.confirmation, action, values, model_id)
            self._set_progress("")
            return
        if result.field_errors:
            self._show_message(
                "Configuration was not saved. Review the fields and try again.",
                "error",
            )
        elif result.message:
            self._show_message(result.message, "info")
        elif action == "refresh" and not preserve_download_progress:
            self._set_progress("")
        if result.search_results:
            self._set_progress("Select a model and quantization to download.")
            self.app.push_screen(
                LocalSearchResultsScreen(result.search_results, theme=self.theme),
                callback=self._download_search_result,
            )
        for diagnostic in result.diagnostics:
            message = (
                f"{diagnostic.stage}: {diagnostic.message}"
                if diagnostic.stage
                else diagnostic.message
            )
            self._show_message(message, diagnostic.severity)
        if result.credential_orphaned:
            self._set_progress("Credential cleanup needs attention.")

    def _download_search_result(self, model_id: str | None) -> None:
        """Start downloading the selected search-result variant.

        开始下载选定的搜索结果变体。
        """
        if model_id is not None:
            self._start_operation("download_model", model_id=model_id)

    def _open_operation_confirmation(
        self,
        request: LocalConfirmationRequest,
        action: LocalAction,
        values: Mapping[str, str] | LocalConfigValues | None,
        model_id: str | None,
    ) -> None:
        """Open a backend-defined opaque confirmation request.

        打开后端定义的不透明确认请求。
        """
        self.app.push_screen(
            LocalChoiceConfirmScreen(request, theme=self.theme),
            callback=lambda choice: self._resume_confirmed_operation(
                choice, action, values, model_id
            ),
        )

    def _resume_confirmed_operation(
        self,
        choice: str | None,
        action: LocalAction,
        values: Mapping[str, str] | LocalConfigValues | None,
        model_id: str | None,
    ) -> None:
        """Resume a paused operation with the selected confirmation value.

        使用选定确认值恢复暂停的操作。
        """
        if choice is not None:
            self._start_operation(action, values=values, model_id=model_id, confirmation=choice)

    def _use_selected(self) -> None:
        """Activate the currently selected usable model.

        激活当前选定且可用的模型。
        """
        model = self._selected_model()
        model_id = (
            model.id if model is not None else self.status.selected_model if self.status else None
        )
        if model_id is None:
            self._show_message(
                "Refresh this backend and select an available model first.",
                "warning",
            )
            return
        if model is not None and model.state not in {"loaded", "sleeping", "available", None}:
            self._show_message(f"{model.id} must be loaded before it can be used.", "warning")
            return
        self._use_model(model_id)

    def _use_model(self, model_id: str) -> None:
        """Invoke the host model-use callback and handle asynchronous completion.

        调用宿主模型使用回调并处理异步完成。
        """
        if not self._is_idle():
            self._show_message("Tau must be idle before switching models.", "warning")
            return
        if self._worker is not None and not self._worker.done():
            self._show_message("An operation is already in progress.", "warning")
            return
        if self._use_task is not None and not self._use_task.done():
            self._show_message("A model switch is already in progress.", "warning")
            return
        if self.on_use is None:
            self._show_message("Model selection is unavailable in this host.", "warning")
            return
        view = self.registry.effective(self.backend_id)
        if view is None or not view.use_available:
            self._show_message(
                "Using this backend is unavailable while it is shadowed.",
                "warning",
            )
            return
        result = self.on_use(view.backend.provider_id, model_id)
        if result is not None:
            self._use_task = asyncio.create_task(self._await_use(result))

    async def _await_use(self, result: Awaitable[None]) -> None:
        """Await asynchronous model activation and report failures safely.

        等待异步模型激活并安全报告故障。
        """
        try:
            await result
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - keep host modal alive
            if self._can_update_ui:
                self._show_message("Could not switch to the selected model.", "error")

    async def _render_status(self, status: LocalBackendStatus) -> None:
        """Store backend status and render all dependent UI sections.

        存储后端状态并渲染所有依赖的 UI 区块。
        """
        lines = [f"State: {status.state}"]
        if status.endpoint_display:
            lines.append(f"Endpoint: {status.endpoint_display}")
        lines.append(f"Authentication: {status.authentication_source}")
        if not status.models:
            lines.append("Models: none discovered")
        if status.selected_model:
            lines.append(f"Selected: {status.selected_model}")
        if status.cached:
            lines.append("Using cached results.")
        if status.stale:
            lines.append("Results may be stale.")
        for diagnostic in status.diagnostics:
            lines.append(
                f"{diagnostic.stage}: {diagnostic.message}"
                if diagnostic.stage
                else diagnostic.message
            )
        self.query_one("#local-backend-status", Static).update("\n".join(lines))
        await self._render_sections(status)

    async def _render_sections(self, status: LocalBackendStatus | None) -> None:
        """Render status, models, actions, diagnostics, and progress sections.

        渲染状态、模型、操作、诊断和进度区块。
        """
        model_list = self.query_one("#local-model-list", ListView)
        action_menu = self.query_one("#local-action-menu", ListView)
        action_focused = action_menu.has_focus
        had_models = bool(self._model_items)
        prior_action = None
        if action_menu.index is not None and 0 <= action_menu.index < len(self._action_items):
            prior_action = self._action_items[action_menu.index][0]

        actions = set(status.actions) if status is not None else {"configure", "refresh"}
        models = status.models if status is not None else ()
        selected_model = status.selected_model if status is not None else None
        self._model_items = tuple(model.id for model in models)
        await model_list.clear()
        await model_list.extend(
            ListItem(Label(_model_label(model, selected_model), markup=False)) for model in models
        )
        model_list.styles.display = "block" if models else "none"
        if models:
            preferred_model = self._selected_model_id or selected_model or models[0].id
            model_list.index = next(
                (index for index, model in enumerate(models) if model.id == preferred_model),
                0,
            )
            self._sync_selected_model()
        else:
            model_list.index = None
            self._selected_model_id = None

        action_labels = (
            ("search_models", "Search Hugging Face models…"),
            ("download_model", "Download an exact Hugging Face model…"),
            ("configure", "Configure connection…"),
            ("refresh", "Refresh server state"),
            ("doctor", "Run Doctor"),
            ("reset", "Reset integration settings…"),
        )
        self._action_items = tuple(
            (action, label) for action, label in action_labels if action in actions
        )
        if self._active_action == "download_model" or self.registry.operation_running(
            self.backend_id, "download_model"
        ):
            self._action_items = (
                ("cancel_download", "Cancel active download…"),
                *self._action_items,
            )
        await action_menu.clear()
        await action_menu.extend(
            ListItem(Label(label, markup=False)) for _, label in self._action_items
        )
        action_menu.styles.display = "block" if self._action_items else "none"
        if self._action_items:
            action_menu.index = next(
                (
                    index
                    for index, (token, _) in enumerate(self._action_items)
                    if token == prior_action
                ),
                0,
            )
        else:
            action_menu.index = None

        if models and (not had_models or not action_focused):
            model_list.focus()
        elif self._action_items:
            action_menu.focus()
        elif models:
            model_list.focus()

    @property
    def _can_update_ui(self) -> bool:
        """Return whether this modal is still mounted and current.

        返回此模态框是否仍已挂载且有效。
        """
        return not self._closing and self.is_mounted and self.is_attached and self.is_current

    def _set_progress(
        self,
        message: str,
        *,
        fraction: float | None = None,
        show_bar: bool = False,
    ) -> None:
        """Update textual and determinate progress without losing finer data.

        更新文本和确定性进度，同时避免丢失更精细的数据。
        """
        if not self._can_update_ui:
            return
        if show_bar and fraction is None and self._progress_fraction is not None:
            # Catalog polling only knows that a download is active. Do not let
            # that coarser update erase newer byte progress from the SSE stream.
            # 目录轮询只知道下载处于活动状态。不要让这种较粗的更新擦除 SSE 流中更新的
            # 字节进度。
            return
        if fraction is not None:
            self._progress_fraction = fraction
        elif not show_bar:
            self._progress_fraction = None
        with suppress(NoMatches):
            progress_message = self.query_one("#local-backend-progress", Static)
            progress_message.update(message)
            progress_message.styles.display = "block" if message else "none"
            progress_bar = self.query_one("#local-backend-progress-bar", ProgressBar)
            progress_bar.styles.display = "block" if show_bar or fraction is not None else "none"
            if fraction is None:
                progress_bar.update(total=None, progress=0)
            else:
                progress_bar.update(total=1, progress=fraction)

    def _show_message(self, message: str, level: str) -> None:
        """Display a local message and forward it to the host notifier.

        显示本地消息并将其转发给宿主通知器。
        """
        if not self._can_update_ui:
            return
        self._notify_callback(message, level)
        self._set_progress(message)


class LocalConfigureScreen(ModalScreen[LocalConfigValues | None]):
    """Render arbitrary text, secret, and choice fields without backend UI code.

    在无需后端 UI 代码的情况下渲染任意文本、敏感和选择字段。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(self, spec: LocalConfigureSpec, *, theme: TuiTheme) -> None:
        """Initialize a configuration form from a backend-neutral specification.

        根据后端无关规范初始化配置表单。
        """
        super().__init__()
        self.spec = spec
        self.theme = theme
        self._field_ids = {
            field.key: f"local-config-input-{index}" for index, field in enumerate(spec.fields)
        }

    def compose(self) -> ComposeResult:
        """Compose widgets for every declared configuration field.

        为每个声明的配置字段组合控件。
        """
        with Vertical(id="local-configure-screen"):
            yield Static("Configure local backend", id="local-configure-title")
            for field in self.spec.fields:
                field_id = self._field_ids[field.key]
                yield Label(field.label, id=f"local-config-label-{field_id}")
                if field.kind == "choice":
                    yield Select(
                        [(choice, choice) for choice in field.choices],
                        allow_blank=not field.required,
                        id=field_id,
                    )
                else:
                    yield Input(
                        placeholder=field.placeholder or "",
                        password=field.kind == "secret",
                        id=field_id,
                    )
            yield Static(
                "Enter advances/saves - Ctrl+S saves - Escape cancels",
                id="local-configure-footer",
            )

    def on_mount(self) -> None:
        """Focus the first configuration input when the form mounts.

        表单挂载时聚焦第一个配置输入。
        """
        if self.spec.fields:
            self.query_one(f"#{self._field_ids[self.spec.fields[0].key]}").focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Save the form when its final text input is submitted.

        提交最后一个文本输入时保存表单。
        """
        field_ids = tuple(self._field_ids[field.key] for field in self.spec.fields)
        if event.input.id not in field_ids:
            return
        event.stop()
        index = field_ids.index(event.input.id)
        if index == len(field_ids) - 1:
            self.action_save()
        else:
            self.query_one(f"#{field_ids[index + 1]}").focus()

    def action_cancel(self) -> None:
        """Dismiss configuration without returning values.

        不返回值并关闭配置。
        """
        self.dismiss(None)

    def action_save(self) -> None:
        """Collect configured fields and dismiss with validated values.

        收集配置字段并使用已校验值关闭表单。
        """
        values: dict[str, str] = {}
        secret_keys: set[str] = set()
        for field in self.spec.fields:
            widget = self.query_one(f"#{self._field_ids[field.key]}")
            if field.kind == "choice":
                selected = cast(Select[str], widget).value
                value = selected if isinstance(selected, str) else ""
            else:
                value = cast(Input, widget).value
            values[field.key] = value
            if field.kind == "secret":
                secret_keys.add(field.key)
        self.dismiss(LocalConfigValues(values, secret_keys=frozenset(secret_keys)))


class LocalConfirmScreen(ModalScreen[bool | None]):
    """Small generic confirmation used for destructive backend actions.

    用于破坏性后端操作的小型通用确认框。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    def __init__(self, title: str, message: str, *, theme: TuiTheme) -> None:
        """Initialize confirmation text and theme.

        初始化确认文本和主题。
        """
        super().__init__()
        self.title_text = title
        self.message = message
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose confirmation choices and keyboard guidance.

        组合确认选项和键盘操作说明。
        """
        with Vertical(id="local-confirm-screen"):
            yield Static(self.title_text, id="local-confirm-title", markup=False)
            yield Static(self.message, id="local-confirm-message", markup=False)
            yield ListView(
                ListItem(Label("Yes", markup=False)),
                ListItem(Label("No", markup=False)),
                id="local-confirm-list",
            )
            yield Static(
                "↑/↓ navigate - Enter selects - Escape cancels",
                id="local-confirm-footer",
            )

    def on_mount(self) -> None:
        """Focus the safe cancel choice initially.

        初始聚焦安全的取消选项。
        """
        choices = self.query_one("#local-confirm-list", ListView)
        choices.index = 1
        choices.focus()

    def on_key(self, event: Key) -> None:
        """Route confirmation navigation keys locally.

        在本地路由确认导航按键。
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
        """Dismiss with the boolean represented by the selected row.

        使用选中行表示的布尔值关闭确认框。
        """
        event.stop()
        self.dismiss(event.index == 0)

    def action_cursor_up(self) -> None:
        """Move the confirmation cursor upward.

        向上移动确认光标。
        """
        self.query_one("#local-confirm-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move the confirmation cursor downward.

        向下移动确认光标。
        """
        self.query_one("#local-confirm-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Activate the confirmation choice under the cursor.

        激活光标所在的确认选项。
        """
        self.query_one("#local-confirm-list", ListView).action_select_cursor()

    def action_confirm(self) -> None:
        """Confirm the destructive action directly.

        直接确认破坏性操作。
        """
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Dismiss without confirming the destructive action.

        不确认破坏性操作并关闭。
        """
        self.dismiss(None)


class LocalChoiceConfirmScreen(ModalScreen[str | None]):
    """Render an arbitrary backend confirmation without protocol knowledge.

    在不了解协议的情况下渲染任意后端确认请求。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_cursor", "Select", show=False),
    ]

    def __init__(self, request: LocalConfirmationRequest, *, theme: TuiTheme) -> None:
        """Initialize an opaque confirmation request and theme.

        初始化不透明的确认请求和主题。
        """
        super().__init__()
        self.request = request
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose backend-provided confirmation choices.

        组合后端提供的确认选项。
        """
        with Vertical(id="local-confirm-screen"):
            yield Static("Confirm backend action", id="local-confirm-title", markup=False)
            yield Static(self.request.message, id="local-confirm-message", markup=False)
            yield ListView(
                *[
                    ListItem(
                        Label(
                            f"{choice.label} — recommended" if choice.recommended else choice.label,
                            markup=False,
                        )
                    )
                    for choice in self.request.choices
                ],
                id="local-choice-list",
            )
            yield Static(
                "↑/↓ navigate - Enter selects - Escape cancels",
                id="local-confirm-footer",
            )

    def on_mount(self) -> None:
        """Focus the default or safest backend confirmation choice.

        聚焦默认或最安全的后端确认选项。
        """
        choices = self.query_one("#local-choice-list", ListView)
        choices.index = next(
            (index for index, choice in enumerate(self.request.choices) if choice.recommended),
            next(
                (
                    index
                    for index, choice in enumerate(self.request.choices)
                    if choice.value == "cancel"
                ),
                0,
            ),
        )
        choices.focus()

    def on_key(self, event: Key) -> None:
        """Route opaque confirmation navigation keys locally.

        在本地路由不透明确认导航按键。
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
        """Dismiss with the opaque value represented by the selected row.

        使用选中行表示的不透明值关闭确认框。
        """
        event.stop()
        self.dismiss(self.request.choices[event.index].value)

    def action_cursor_up(self) -> None:
        """Move the choice cursor upward.

        向上移动选项光标。
        """
        self.query_one("#local-choice-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move the choice cursor downward.

        向下移动选项光标。
        """
        self.query_one("#local-choice-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Activate the backend confirmation choice under the cursor.

        激活光标所在的后端确认选项。
        """
        self.query_one("#local-choice-list", ListView).action_select_cursor()

    def action_cancel(self) -> None:
        """Dismiss without selecting a backend confirmation value.

        不选择后端确认值并关闭。
        """
        self.dismiss(None)


class LocalSearchResultsScreen(ModalScreen[str | None]):
    """Choose one backend-provided artifact variant for download.

    选择一个后端提供的产物变体进行下载。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "confirm", "Download", show=False),
    ]

    def __init__(
        self,
        results: tuple[LocalSearchResult, ...],
        *,
        theme: TuiTheme,
    ) -> None:
        """Initialize search results, flattened variants, and theme.

        初始化搜索结果、扁平化变体和主题。
        """
        super().__init__()
        self.results = results
        self.theme = theme
        self.options = tuple(
            option for result in results for option in _search_result_options(result)
        )

    def compose(self) -> ComposeResult:
        """Compose selectable artifact variants and search diagnostics.

        组合可选择的产物变体和搜索诊断。
        """
        with Vertical(id="local-search-results-screen"):
            yield Static("Download model", id="local-search-results-title", markup=False)
            yield Static(
                "Choose a Hugging Face model variant. llama.cpp performs the download.",
                id="local-search-results-help",
                markup=False,
            )
            yield ListView(
                *[ListItem(Label(label, markup=False)) for _, label, _ in self.options],
                id="local-search-results-list",
            )
            yield Static(
                "↑/↓ navigate - Enter continues - Escape cancels",
                id="local-search-results-footer",
            )

    def on_mount(self) -> None:
        """Focus the first recommended or available artifact variant.

        聚焦第一个推荐或可用的产物变体。
        """
        if not self.options:
            return
        index = next(
            (index for index, (_, _, recommended) in enumerate(self.options) if recommended),
            0,
        )
        model_list = self.query_one("#local-search-results-list", ListView)
        model_list.index = index
        model_list.focus()

    def on_key(self, event: Key) -> None:
        """Route search-result navigation keys locally.

        在本地路由搜索结果导航按键。
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
        """Dismiss with the selected artifact option identifier.

        使用选定的产物选项标识符关闭。
        """
        event.stop()
        self.action_confirm()

    def action_cursor_up(self) -> None:
        """Move the artifact cursor upward.

        向上移动产物光标。
        """
        self.query_one("#local-search-results-list", ListView).action_cursor_up()

    def action_cursor_down(self) -> None:
        """Move the artifact cursor downward.

        向下移动产物光标。
        """
        self.query_one("#local-search-results-list", ListView).action_cursor_down()

    def action_select_cursor(self) -> None:
        """Activate the artifact variant under the cursor.

        激活光标所在的产物变体。
        """
        self.query_one("#local-search-results-list", ListView).action_select_cursor()

    def action_confirm(self) -> None:
        """Confirm the highlighted artifact variant.

        确认高亮的产物变体。
        """
        if not self.options:
            return
        index = self.query_one("#local-search-results-list", ListView).index
        if index is not None and 0 <= index < len(self.options):
            self.dismiss(self.options[index][0])

    def action_cancel(self) -> None:
        """Dismiss without selecting an artifact variant.

        不选择产物变体并关闭。
        """
        self.dismiss(None)


class LocalModelActionScreen(ModalScreen[str | None]):
    """Collect one opaque model reference for a backend-provided action.

    为后端提供的操作收集一个不透明模型引用。
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        title: str,
        *,
        placeholder: str = "Model identifier",
        theme: TuiTheme,
    ) -> None:
        """Initialize the model action prompt and optional placeholder.

        初始化模型操作提示和可选占位符。
        """
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.theme = theme

    def compose(self) -> ComposeResult:
        """Compose the model-reference input and action guidance.

        组合模型引用输入和操作说明。
        """
        with Vertical(id="local-model-action-screen"):
            yield Static(self.title_text, id="local-model-action-title", markup=False)
            yield Input(placeholder=self.placeholder, id="local-model-action-input")
            yield Static(
                "Enter continues - Escape cancels",
                id="local-model-action-footer",
            )

    def on_mount(self) -> None:
        """Focus the model-reference input.

        聚焦模型引用输入框。
        """
        self.query_one("#local-model-action-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit the current model reference from the input event.

        从输入事件提交当前模型引用。
        """
        event.stop()
        self._submit()

    def _submit(self) -> None:
        """Normalize and return the entered model reference when non-empty.

        输入非空时规范化并返回模型引用。
        """
        value = self.query_one("#local-model-action-input", Input).value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        """Dismiss without returning a model reference.

        不返回模型引用并关闭。
        """
        self.dismiss(None)


def _model_label(model: LocalModel, selected_model: str | None) -> str:
    """Build a model-row label with state and selection markers.

    构建包含状态和选择标记的模型行标签。
    """
    label = model.display_name or model.id
    if label != model.id:
        label = f"{label} ({model.id})"
    details = []
    if model.state:
        details.append("available to load" if model.state == "unloaded" else model.state)
    if model.id == selected_model:
        details.append("active")
    return label + (" — " + " · ".join(details) if details else "")


def _search_result_options(
    result: LocalSearchResult,
) -> tuple[tuple[str, str, bool], ...]:
    """Flatten one backend search result into selectable artifact variants.

    将一个后端搜索结果扁平化为可选择的产物变体。
    """
    if not result.options:
        restricted = " — restricted" if result.restricted else ""
        return ((result.id, result.label + restricted, False),)
    options: list[tuple[str, str, bool]] = []
    for option in result.options:
        details = [option.label]
        if option.size_bytes is not None:
            details.append(_format_bytes(option.size_bytes))
        if option.recommended:
            details.append("recommended")
        if result.restricted:
            details.append("restricted")
        options.append((option.id, f"{result.label} — " + " · ".join(details), option.recommended))
    return tuple(options)


def _format_bytes(value: int) -> str:
    """Format an artifact size as a concise binary byte value.

    将产物大小格式化为简洁的二进制字节值。
    """
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


__all__ = [
    "LocalBackendPickerScreen",
    "LocalBackendScreen",
    "LocalConfigureScreen",
    "LocalChoiceConfirmScreen",
    "LocalConfirmScreen",
    "LocalModelActionScreen",
    "LocalSearchResultsScreen",
]
