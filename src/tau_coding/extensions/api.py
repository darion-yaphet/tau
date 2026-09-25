"""Extension-facing API types and hook payloads.

面向扩展的 API 类型与钩子载荷。
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast
from uuid import uuid4

from tau_agent.messages import AgentMessage, ToolResultMessage
from tau_agent.tools import AgentTool, AgentToolResult
from tau_agent.types import JSONValue

if TYPE_CHECKING:
    from textual import events
    from textual.widget import Widget

    from tau_coding.extensions.providers import DynamicProvider
    from tau_coding.extensions.runtime import ExtensionRuntime
    from tau_coding.local_backends import LocalBackend
    from tau_coding.paths import TauPaths
    from tau_coding.tui.config import TuiTheme

AGENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent_start",
        "agent_end",
        "agent_settled",
        "turn_start",
        "turn_end",
        "queue_update",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "compaction_start",
        "compaction_end",
        "entry_appended",
        "session_info_changed",
        "thinking_level_changed",
        "auto_retry_start",
        "auto_retry_end",
    }
)
AGENT_EVENT_WILDCARD = "agent_event"

LIFECYCLE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session_start",
        "session_shutdown",
        "input",
        "tool_call",
        "tool_result",
        "project_trust",
    }
)

SessionLifecycleReason = Literal["startup", "reload", "new", "resume", "branch", "quit"]
DeliverAs = Literal["steer", "follow_up"]
NotifyLevel = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class CustomMessageView:
    """Read-only view of a custom message handed to a message renderer.

    传递给消息渲染器的自定义消息只读视图。

    Ports Pi's ``CustomMessage``: ``custom_type`` selects the renderer,
    ``content`` is the LLM-context text, and ``details`` carries arbitrary
    structured data the renderer formats.

    此类型移植 Pi 的 ``CustomMessage``：``custom_type`` 选择渲染器，``content`` 是
    LLM 上下文文本，``details`` 携带由渲染器格式化的任意结构化数据。
    """

    custom_type: str
    content: str
    details: Mapping[str, JSONValue] | None = None


@dataclass(frozen=True, slots=True)
class MessageRenderOptions:
    """Options passed to a message renderer (ports Pi's ``MessageRenderOptions``).

    传递给消息渲染器的选项（移植自 Pi 的 ``MessageRenderOptions``）。
    """

    expanded: bool = False


# A message renderer returns a Rich-markup (or plain) string, NOT a Textual
# widget, so extensions never import the TUI toolkit (deviation from Pi's
# ``Component`` return; see the phase-21 custom-renderer Ruling).
# 消息渲染器返回 Rich 标记或纯文本字符串，而不是 Textual 控件，因此扩展无需导入
# TUI 工具包。
MessageRenderer = Callable[[CustomMessageView, MessageRenderOptions], str]

# Host-side resolver installed into render paths: given a custom message's
# fields and whether it is expanded, return rendered markup or ``None`` to fall
# back to the raw content. Errors are swallowed by the resolver, never raised
# into the frontend.
# 宿主侧解析器安装到渲染路径中：根据自定义消息字段和展开状态返回渲染标记，或返回
# ``None`` 回退到原始内容。解析器会吞掉错误，不会将其抛入前端。
CustomMessageMarkup = Callable[[str, str, "Mapping[str, JSONValue] | None", bool], "str | None"]

# Host-side resolver installed into render paths: given a tool call's name and
# arguments, return the friendly invocation line from the tool's `render_call`
# or ``None`` to fall back to generic formatting. Errors are swallowed by the
# resolver, never raised into the frontend.
# 宿主侧解析器根据工具名和参数返回工具 `render_call` 生成的友好调用行，或返回
# ``None`` 回退到通用格式。错误不会抛入前端。
ToolCallMarkup = Callable[[str, "Mapping[str, JSONValue]"], "str | None"]

# Host-side resolver installed into render paths: given the tool name, its
# result, and whether the row is expanded, return the display markup from the tool's
# `render_result` or ``None`` to fall back to the generic result block. Errors
# are swallowed by the resolver, never raised into the frontend.
# 宿主侧解析器根据工具名、结果和展开状态返回工具 `render_result` 的显示标记，或返回
# ``None`` 回退到通用结果块。错误不会抛入前端。
ToolResultMarkup = Callable[[str, AgentToolResult, bool], "str | None"]

# --- component seam ---------------------------------------------------------
# Widget-hosting capability that lets an extension mount its own Textual widgets
# into host-owned slots and a main-area view, and intercept keys pre-dispatch
# (before the host's priority bindings and the focused widget). The "component"
# type is Textual's own ``Widget`` (referenced only under TYPE_CHECKING so
# print-mode stays import-clean): Textual is deliberately part of the public
# extension contract. Extensions build against the Textual version tau pins; a
# Textual major bump is a coordinated break for core and extensions together.
# History and measured trade-offs: dev-notes/design/component-seam-experiment.md.
# It replaced the older transcript-source seam, which Step 3 removed from core.
# 组件接缝允许扩展把 Textual 控件挂载到宿主槽位和主区域，并在宿主优先绑定及焦点控件前
# 拦截按键。Textual 的 Widget 是公共扩展契约的一部分；Textual 主版本升级需要核心与
# 扩展协调变更。此接缝取代了旧的记录来源接缝。

Placement = Literal["above_prompt", "below_prompt"]

# Factories run on the UI thread and receive the live theme (theme handoff,
# mirrors Pi's ``(tui, theme) => Component``).
# 工厂在 UI 线程运行并接收实时主题，对应 Pi 的 ``(tui, theme) => Component``。
SlotWidgetFactory = Callable[["TuiTheme"], "Widget"]
# A slot widget may be given as a factory or, for the simple case, as a plain
# list of display lines the HOST turns into a widget — this lets an extension
# mount text without importing Textual at all (ports Pi's ``string[]`` form of
# ``setWidget``). Strings are rendered as Rich markup, with a literal-text
# fallback if the markup is malformed.
# 槽位控件可由工厂提供，也可使用由宿主转成控件的纯显示行列表，使扩展无需导入 Textual
# 即可挂载文本。字符串按 Rich 标记渲染，格式错误时回退到字面文本。
SlotWidgetContent = Sequence[str] | SlotWidgetFactory
# Sidebar bodies use the same data-first shape: simple sections provide Rich
# display lines without importing Textual; advanced sections provide a widget
# factory receiving the live theme.
# 侧边栏正文采用同样的数据优先结构：简单区块提供无需导入 Textual 的 Rich 显示行；
# 高级区块提供接收实时主题的控件工厂。
SidebarWidgetFactory = Callable[["TuiTheme"], "Widget"]
SidebarContent = Sequence[str] | SidebarWidgetFactory
# The main-view factory also receives the handle so the widget can close itself.
# 主视图工厂也会接收句柄，使控件可以自行关闭。
MainViewFactory = Callable[["MainViewHandle", "TuiTheme"], "Widget"]

# Pre-dispatch key hook (ports Pi's ``onTerminalInput``). Returns True to
# consume the key. Fires for every main-screen key regardless of focus; the host
# passes the Textual ``Key`` event and the current prompt text so the handler
# can self-gate (Pi gates on ``getEditorText() === ""``).
# 预分发按键钩子移植自 Pi 的 ``onTerminalInput``。返回 True 会消费按键。无论焦点在哪，
# 它都会接收每个主屏幕按键；宿主传入 Textual ``Key`` 事件和当前提示词文本，使处理器
# 可以自行限制触发条件。
KeyInterceptor = Callable[["events.Key", str], bool]


_DEFAULT_THEME: TuiTheme | None = None


def _default_theme() -> TuiTheme:
    """Return a shared default theme without importing the TUI at module load.

    返回共享默认主题，而不在模块加载时导入 TUI。

    The import is deferred (and cached) so merely importing the extensions API
    stays free of the Textual/TUI dependency graph; only an extension that
    actually reads ``theme`` in print mode pays for it, and it never raises.

    导入会延迟并缓存，因此仅导入扩展 API 不会加载 Textual/TUI 依赖图；只有扩展在打印
    模式中实际读取 ``theme`` 时才会付出成本，并且此函数绝不抛出异常。
    """
    global _DEFAULT_THEME
    if _DEFAULT_THEME is None:
        from tau_coding.tui.config import TAU_DARK_THEME

        _DEFAULT_THEME = TAU_DARK_THEME
    return _DEFAULT_THEME


class MainViewHandle(Protocol):
    """Handle to an open main-area view (ports Pi's ``OverlayHandle``, trimmed).

    已打开主区域视图的句柄，是 Pi ``OverlayHandle`` 的精简移植。

    Carries Pi's ``done(result)`` semantics: the factory (or a key interceptor)
    calls ``close(result)`` to tear the view down *and* hand a value back to
    whoever opened it, and the opener awaits :meth:`wait` for that value. This
    is the result-resolution half of Pi's ``ctx.ui.custom<T>``, kept on the
    synchronous open/handle model rather than an ``async`` open.

    ``close()`` unmounts the view and restores the main transcript. It is safe
    to call more than once; the first close wins and later closes are no-ops.

    它承载 Pi 的 ``done(result)`` 语义：工厂或按键拦截器调用 ``close(result)`` 关闭视图，
    并把值传回打开视图的一方；打开方通过等待 :meth:`wait` 获取该值。``close()`` 会卸载
    视图并恢复主记录，可多次安全调用；第一次关闭生效，后续调用不执行操作。
    """

    def close(self, result: object | None = None) -> None:
        """Close the view, resolving :meth:`wait` with ``result`` (Pi's ``done``).

        关闭视图，并使用 ``result`` 解析 :meth:`wait`，对应 Pi 的 ``done``。

        The first close wins: its ``result`` is what :meth:`wait` returns, and
        any later ``close(...)`` is a no-op. Safe to call more than once.

        第一次关闭生效：其 ``result`` 即 :meth:`wait` 的返回值，后续 ``close(...)`` 不
        执行操作。可安全多次调用。
        """
        ...

    async def wait(self) -> object | None:
        """Await the view's teardown and return the result passed to ``close``.

        等待视图拆除，并返回传给 ``close`` 的结果。

        Resolves with the value handed to :meth:`close` (``None`` when closed
        with no result). Also resolves with ``None`` — never hangs — when the
        view is force-cleared on a session rebind, quarantined after a widget
        crash, or superseded by a later ``open_main_view``. Returns immediately
        if the view was already closed before ``wait`` is awaited.

        返回传给 :meth:`close` 的值；无结果关闭时返回 ``None``。会话重新绑定时强制清除、
        控件崩溃后隔离或被后续 ``open_main_view`` 取代时，也会以 ``None`` 完成而不会永久
        等待。如果等待前视图已关闭，则立即返回。
        """
        ...

    @property
    def is_open(self) -> bool:
        """Return whether the main-area view is still open.

        返回主区域视图是否仍处于打开状态。
        """
        ...


class ComponentBridge(Protocol):
    """Host widget-hosting capability, exposed via ``context.ui.components``.

    通过 ``context.ui.components`` 暴露的宿主控件承载能力。

    Part of what a :class:`UiBridge` provides when a TUI is attached;
    :class:`NullUiBridge`/:class:`StderrUiBridge` implement it as no-ops so an
    extension stays fully functional (just widget-less) in print mode. Check
    :attr:`supports_components` before building widgets.

    当附加 TUI 时，这是 :class:`UiBridge` 提供的能力之一；Null 或 Stderr 桥接器在打印
    模式中以无操作方式实现它，使扩展仍完整可用。构建控件前应检查
    :attr:`supports_components`。
    """

    @property
    def supports_components(self) -> bool:
        """Return whether the frontend can host extension widgets.

        返回前端是否可承载扩展控件。
        """
        ...

    @property
    def theme(self) -> TuiTheme:
        """Return the live TUI theme handed to widget factories.

        返回传递给控件工厂的实时 TUI 主题。
        """
        ...

    def get_prompt_text(self) -> str:
        """Return the current prompt-editor text (Pi's getEditorText).

        返回当前提示词编辑器文本，对应 Pi 的 getEditorText。

        Key interceptors receive the prompt text as their second argument;
        this is for reads outside the key path.

        按键拦截器会把提示词文本作为第二个参数接收；此方法用于按键路径之外的读取。
        """
        ...

    def request_render(self) -> None:
        """Ask the host to re-render mounted extension widgets (Pi's requestRender).

        请求宿主重新渲染已挂载的扩展控件，对应 Pi 的 requestRender。
        """
        ...

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:
        """Mount an extension widget into a prompt-adjacent slot under ``key``.

        ``content`` is either a ``factory(theme) -> Widget`` callable or a plain
        list of display lines (``Sequence[str]``) the host renders as Rich
        markup — the string form lets simple extensions avoid importing Textual.
        Passing ``content=None`` unmounts and forgets that key. Re-setting a key
        replaces its content. Multiple keys per placement mount in call order.
        Placement defaults to ``"above_prompt"`` (Pi's ``aboveEditor``).

        ``content`` 可以是控件工厂，也可以是由宿主渲染为 Rich 标记的显示行。传入 None
        会卸载并忘记该键；重复设置会替换内容。同一位置的多个键按调用顺序挂载，默认位置
        对应 Pi 的 aboveEditor。
        """
        ...

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:
        """Mount ``factory(handle, theme)`` as a full main-area view.

        The widget replaces the main transcript in place (a display-toggled
        sibling, not a modal screen), so prompt-adjacent widgets such as slot
        widgets stay visible. ``handle.close(result)`` restores the transcript
        and resolves ``await handle.wait()`` with ``result`` (Pi's ``done``),
        so the opener can show a view and get an answer back.

        控件会在原位替换主记录，而不是模态屏幕，因此提示词附近的控件仍可见。
        ``handle.close(result)`` 会恢复记录，并使 ``await handle.wait()`` 返回结果。
        """
        ...

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:
        """Register a pre-dispatch key hook; return an unsubscribe callable.

        Ports Pi's ``onTerminalInput``. The handler sees a key before the
        host's app-level priority bindings and before the focused widget, so it
        can own navigation keys the host otherwise reserves. It fires for EVERY
        main-screen key regardless of focus (never while a modal screen is on
        top), so the handler MUST self-gate and return ``True`` only for keys it
        consumes.

        The host's hard interrupt/exit keys (``ctrl+c`` and ``ctrl+d``) are
        reserved: the interceptor is never consulted for them and cannot
        consume them, so a bug in the handler can never swallow the session's
        escape hatches. All other keys — escape, enter, arrows, tab — remain
        interceptable.

        此钩子移植自 Pi 的 onTerminalInput，在宿主优先绑定和焦点控件之前接收按键。处理器
        必须自行限制触发条件。ctrl+c 和 ctrl+d 保留给宿主，其他按键均可拦截。
        """
        ...


class ExtensionSidebar:
    """Extension-owned facade for host-framed TUI sidebar sections.

    扩展拥有的宿主框定 TUI 侧边栏区块门面。

    Sections are isolated by extension name and stable key. Re-setting a key
    updates it without changing its position; removing and re-adding it appends
    it after the remaining extension sections. The host owns headings,
    separators, width, scrolling, sidebar placement, and lifecycle cleanup.

    区块按扩展名称和稳定键隔离。重复设置同一键会原位更新；移除后重新添加会追加到剩余
    区块之后。宿主负责标题、分隔符、宽度、滚动、侧边栏位置和生命周期清理。
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        extension_name: str,
        generation: ExtensionGeneration,
    ) -> None:
        """Bind the sidebar facade to one extension generation.

        将侧边栏门面绑定到一个扩展代际。
        """
        self._runtime = runtime
        self._extension_name = extension_name
        self._generation = generation

    @property
    def supported(self) -> bool:
        """Return whether the active frontend can display sidebar sections.

        返回活动前端是否可显示侧边栏区块。
        """
        self._generation.assert_active()
        return bool(getattr(self._runtime.ui, "supports_sidebar", False))

    def set_section(self, key: str, *, title: str, content: SidebarContent) -> None:
        """Add or replace a host-framed sidebar section under stable ``key``.

        在稳定 ``key`` 下添加或替换宿主框定的侧边栏区块。
        """
        self._generation.assert_active()
        normalized_key = key.strip()
        normalized_title = title.strip()
        if not normalized_key:
            raise ExtensionError("sidebar section key must not be empty")
        if not normalized_title:
            raise ExtensionError("sidebar section title must not be empty")
        if not self.supported:
            return
        setter = getattr(self._runtime.ui, "set_sidebar_section", None)
        if setter is not None:
            setter(
                self._extension_name,
                normalized_key,
                title=normalized_title,
                content=content,
            )

    def remove_section(self, key: str) -> None:
        """Remove this extension's sidebar section under ``key``, if present.

        移除此扩展在 ``key`` 下的侧边栏区块（若存在）。
        """
        self._generation.assert_active()
        normalized_key = key.strip()
        if not normalized_key:
            raise ExtensionError("sidebar section key must not be empty")
        if not self.supported:
            return
        remover = getattr(self._runtime.ui, "remove_sidebar_section", None)
        if remover is not None:
            remover(self._extension_name, normalized_key)


class ExtensionError(RuntimeError):
    """Raised when an extension misuses the API (e.g. actions before binding).

    扩展误用 API 时抛出的异常，例如在绑定前执行操作。
    """


_STALE_MESSAGE = (
    "extension instance is stale after reload: state captured before /reload"
    " (a saved `tau` API object, context, or ui handle) must not be reused;"
    " the reloaded extension received a fresh API in its new setup()"
)


class ExtensionGeneration:
    """Liveness token for one extension load generation.

    一个扩展加载代际的存活令牌。

    Ports Pi's ``assertActive``/``invalidate`` staleness guard: every
    :class:`ExtensionAPI` method and every :class:`ExtensionContext`/
    :class:`ExtensionUi` read checks this token before touching the runtime,
    so state captured before a `/reload` fails loudly instead of silently
    acting against the new registration set. Only reload invalidates; session
    rebinding (resume/new/branch) keeps the generation alive by design (see
    the phase-21 lifecycle Ruling).

    此类型移植 Pi 的过期保护：每个 ExtensionAPI 方法和 Context/UI 读取都会在接触运行时
    前检查令牌，使重载前捕获的状态明确失败。只有重载会使代际失效；恢复、新建或分支等
    会话重新绑定会按设计保持代际活动。
    """

    __slots__ = ("_id", "_stale_message")

    def __init__(self) -> None:
        """Create a fresh active generation with a unique process-local ID.

        创建带有唯一进程本地 ID 的全新活动代际。
        """
        self._id = uuid4().hex
        self._stale_message: str | None = None

    @property
    def id(self) -> str:
        """Return this generation's stable process-local identity.

        返回此代际稳定的进程本地标识。
        """
        return self._id

    @property
    def active(self) -> bool:
        """Return whether this generation is still the live one.

        返回此代际是否仍为活动代际。
        """
        return self._stale_message is None

    def invalidate(self, message: str | None = None) -> None:
        """Mark this generation stale; the first message wins (Pi parity).

        将此代际标记为过期；第一条消息生效，与 Pi 保持一致。
        """
        if self._stale_message is None:
            self._stale_message = message or _STALE_MESSAGE

    def assert_active(self) -> None:
        """Raise :class:`ExtensionError` when this generation is stale.

        此代际过期时抛出 :class:`ExtensionError`。
        """
        if self._stale_message is not None:
            raise ExtensionError(self._stale_message)


@dataclass(frozen=True, slots=True)
class TurnStartEvent:
    """Pi-shaped extension event fired at the start of a turn.

    每轮开始时触发的 Pi 风格扩展事件。

    The portable agent event intentionally has no session metadata. The coding
    session adapter adds the zero-based turn index and millisecond timestamp
    before dispatching the event to extensions.

    可移植代理事件刻意不含会话元数据。编码会话适配器会在分发给扩展前添加从零开始的
    轮次索引和毫秒时间戳。
    """

    turn_index: int
    timestamp: int
    type: Literal["turn_start"] = field(default="turn_start", init=False)


@dataclass(frozen=True, slots=True)
class TurnEndEvent:
    """Pi-shaped extension event fired after one assistant/tool turn.

    一次助手或工具轮次结束后触发的 Pi 风格扩展事件。
    """

    turn_index: int
    message: AgentMessage
    tool_results: list[ToolResultMessage]
    type: Literal["turn_end"] = field(default="turn_end", init=False)


@dataclass(frozen=True, slots=True)
class SessionStartEvent:
    """Payload for the `session_start` lifecycle event.

    `session_start` 生命周期事件的载荷。
    """

    reason: SessionLifecycleReason


@dataclass(frozen=True, slots=True)
class SessionShutdownEvent:
    """Payload for the `session_shutdown` lifecycle event.

    `session_shutdown` 生命周期事件的载荷。
    """

    reason: SessionLifecycleReason


@dataclass(frozen=True, slots=True)
class InputEvent:
    """Payload for the `input` hook: raw user prompt text, before expansion.

    `input` 钩子的载荷：展开前的原始用户提示词文本。

    Mirrors Pi's `InputEvent`. `source` says where the input came from:
    ``"interactive"`` for TUI/print-mode user input, ``"extension"`` for a turn
    an extension started via ``send_user_message``/``send_custom_message``.
    `streaming_behavior` says how the input will be queued when the agent is
    mid-run (``"steer"``/``"follow_up"``), and is ``None`` on the idle prompt
    path.

    Pi's `images` field is omitted (Tau has no image input yet) and Pi's
    ``"rpc"`` source is omitted (Tau has no RPC mode). Both defaults keep
    existing handlers that read only ``.text`` working unchanged.

    此类型与 Pi 的 InputEvent 对齐；``source`` 表示输入来源，``streaming_behavior`` 表示
    代理运行中输入的排队方式。Tau 尚无图像输入或 RPC 模式，因此省略对应字段和来源，
    只读取 ``.text`` 的现有处理器仍可原样工作。
    """

    text: str
    source: Literal["interactive", "extension"] = "interactive"
    streaming_behavior: Literal["steer", "follow_up"] | None = None


@dataclass(frozen=True, slots=True)
class InputHookResult:
    """Result of an `input` hook handler.

    `input` 钩子处理器的结果。

    `action="continue"` leaves the text unchanged, `"transform"` replaces it
    with `text` (transforms chain across handlers), and `"handled"` consumes
    the input entirely, optionally showing `message` to the user.

    `action="continue"` 保持文本不变，`"transform"` 使用 `text` 替换文本，转换会跨处理器
    串联；`"handled"` 完全消费输入，并可向用户显示 `message`。
    """

    action: Literal["continue", "transform", "handled"] = "continue"
    text: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ToolCallHookEvent:
    """Payload for the `tool_call` hook, before a tool executes.

    工具执行前 `tool_call` 钩子的载荷。

    Carries no tool-call id: the hook runs inside the tool executor seam,
    which the agent loop invokes without the id. Use the observation events
    (`tool_execution_start`/`tool_execution_end`) for id correlation.

    此载荷不含工具调用 ID，因为钩子运行在代理循环不会传入 ID 的工具执行器接缝内。需要
    关联 ID 时使用观察事件。
    """

    tool_name: str
    arguments: Mapping[str, JSONValue]


@dataclass(frozen=True, slots=True)
class ToolCallHookResult:
    """Result of a `tool_call` hook handler.

    `tool_call` 钩子处理器的结果。

    Set `block=True` (with an optional `reason`) to prevent execution, or
    return replacement `arguments` to rewrite the call. Blocking wins over
    argument rewrites and short-circuits remaining handlers.

    设置 `block=True` 可阻止执行，并可附带原因；也可返回替换参数重写调用。阻止优先于
    参数重写，并会短路剩余处理器。
    """

    block: bool = False
    reason: str | None = None
    arguments: Mapping[str, JSONValue] | None = None


@dataclass(frozen=True, slots=True)
class ToolResultHookEvent:
    """Payload for the `tool_result` hook, after a tool executes.

    工具执行后 `tool_result` 钩子的载荷。
    """

    tool_name: str
    arguments: Mapping[str, JSONValue]
    result: AgentToolResult


@dataclass(frozen=True, slots=True)
class ToolResultHookResult:
    """Result of a `tool_result` hook handler; set fields to override.

    `tool_result` 钩子处理器的结果；设置字段即可覆盖原值。
    """

    content: str | None = None
    details: dict[str, JSONValue] | None = None


ExtensionHandler = Callable[[object, "ExtensionContext"], object | Awaitable[object]]
# Command handlers are sync-only: the slash-command path (CommandRegistry ->
# CodingSession.handle_command -> TUI submit) is synchronous end to end.
# 命令处理器仅支持同步：斜杠命令路径从 CommandRegistry 到 CodingSession.handle_command
# 再到 TUI 提交，全程同步。
ExtensionCommandHandler = Callable[["str", "ExtensionCommandContext"], "str | None"]


@dataclass(frozen=True, slots=True)
class ExtensionRuntimeDiagnostic:
    """A runtime failure raised by an extension handler.

    扩展处理器抛出的运行时故障。
    """

    extension: str
    event: str
    message: str


class UiBridge(Protocol):
    """Host-provided UI capabilities available to extensions.

    宿主提供给扩展的 UI 能力。

    Dialog methods (`select`/`confirm`/`input`) are async and mirror Pi's
    `ctx.ui`. Without an interactive frontend they return the Pi no-op
    defaults (`None`/`False`/`None`). `timeout` (seconds) auto-dismisses a
    dialog with the no-op default; `None` waits indefinitely.

    对话框方法为异步并与 Pi 的 ctx.ui 对齐。无交互式前端时返回无操作默认值；timeout
    会以默认值自动关闭对话框，None 表示无限等待。
    """

    @property
    def has_ui(self) -> bool:
        """Return whether an interactive UI is attached.

        返回是否附加了交互式 UI。
        """
        ...

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Show a notification to the user (no-op without a UI).

        向用户显示通知；没有 UI 时不执行操作。
        """
        ...

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Show a picker; return the chosen option, or None on cancel.

        显示选择器；返回所选选项，取消时返回 None。
        """
        ...

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Show a confirmation; return True only if confirmed.

        显示确认提示；仅确认时返回 True。
        """
        ...

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Show a text prompt; return the entered text, or None on cancel.

        显示文本提示；返回输入文本，取消时返回 None。
        """
        ...

    # -- component seam -- see ComponentBridge for docs -----------------------
    # -- 组件接缝；文档见 ComponentBridge ------------------------------------

    @property
    def supports_components(self) -> bool:
        """Return whether the frontend can host extension widgets.

        返回前端是否可承载扩展控件。
        """
        ...

    @property
    def theme(self) -> TuiTheme:
        """Return the live TUI theme handed to widget factories.

        返回传递给控件工厂的实时 TUI 主题。
        """
        ...

    def get_prompt_text(self) -> str:
        """Return the current prompt-editor text.

        返回当前提示词编辑器文本。
        """
        ...

    def request_render(self) -> None:
        """Ask the host to re-render mounted extension widgets.

        请求宿主重新渲染已挂载的扩展控件。
        """
        ...

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:
        """Mount or remove an extension slot widget (factory or string lines).

        挂载或移除扩展槽位控件，可使用工厂或字符串行。
        """
        ...

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:
        """Open a full main-area extension view.

        打开完整的主区域扩展视图。
        """
        ...

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:
        """Register a pre-dispatch key hook; return an unsubscribe callable.

        See the base bridge protocol: the handler is consulted before the
        host's priority bindings and the focused widget, fires for every
        main-screen key regardless of focus, and must self-gate. The hard
        interrupt/exit keys (``ctrl+c`` and ``ctrl+d``) are reserved and never
        reach the interceptor.

        处理器在宿主优先绑定和焦点控件之前接收按键，对所有主屏幕按键触发并必须自行
        限制。硬中断和退出键由宿主保留，不会传给拦截器。
        """
        ...

    @property
    def supports_sidebar(self) -> bool:
        """Return whether this frontend can host extension sidebar sections.

        返回此前端是否可承载扩展侧边栏区块。
        """
        ...

    def set_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:
        """Add or replace one extension-owned, host-framed sidebar section.

        添加或替换一个扩展拥有、由宿主框定的侧边栏区块。
        """
        ...

    def remove_sidebar_section(self, extension_name: str, key: str) -> None:
        """Remove one extension-owned sidebar section, if present.

        移除一个扩展拥有的侧边栏区块（若存在）。
        """
        ...

    def clear_components(self) -> None:
        """Tear down all extension-owned UI (host-driven, not for extensions).

        The runtime drives this on `/reload` (the stale generation's widgets
        and interceptors must not outlive its registrations) and on session
        rebinds (resume/new), before ``session_start`` fires so handlers can
        re-mount. Slot widgets and any main view are unmounted (a pending
        ``wait()`` resolves with ``None``) and key interceptors are dropped.

        运行时会在重载和会话重新绑定时调用此方法。它会卸载槽位控件和主视图，使等待以
        None 完成，并移除按键拦截器。
        """
        ...


class _DeadMainViewHandle:
    """A no-op main-view handle returned when no UI can host a view.

    没有 UI 可承载视图时返回的无操作主视图句柄。
    """

    def close(self, result: object | None = None) -> None:
        """Do nothing: there is no view to close (``result`` is ignored).

        不执行操作：没有可关闭的视图，``result`` 会被忽略。
        """

    async def wait(self) -> object | None:
        """Return None immediately: a dead handle never opens a view.

        立即返回 None：失效句柄从未打开视图。
        """
        return None

    @property
    def is_open(self) -> bool:
        """Return False: a dead handle is never open.

        返回 False：失效句柄永远未打开。
        """
        return False


class NullUiBridge:
    """UI bridge used when no interactive frontend is attached.

    未附加交互式前端时使用的 UI 桥接器。
    """

    @property
    def has_ui(self) -> bool:
        """Return False: print mode has no interactive UI.

        返回 False：打印模式没有交互式 UI。
        """
        return False

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Ignore notifications without a UI.

        没有 UI 时忽略通知。
        """

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Return None: no UI to pick from (Pi no-op default).

        返回 None：没有用于选择的 UI，这是 Pi 的无操作默认行为。
        """
        return None

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Return False: no UI to confirm with (Pi no-op default).

        返回 False：没有用于确认的 UI，这是 Pi 的无操作默认行为。
        """
        return False

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Return None: no UI to enter text into (Pi no-op default).

        返回 None：没有用于输入文本的 UI，这是 Pi 的无操作默认行为。
        """
        return None

    # -- component seam -------------------------------------------------------
    # -- 组件接缝 -------------------------------------------------------------

    @property
    def supports_components(self) -> bool:
        """Return False: print mode cannot host widgets.

        返回 False：打印模式无法承载控件。
        """
        return False

    @property
    def theme(self) -> TuiTheme:
        """Return a usable default theme (never raise; print-mode may read it).

        返回可用的默认主题；绝不抛出异常，打印模式可能读取它。
        """
        return _default_theme()

    def get_prompt_text(self) -> str:
        """Return an empty prompt: there is no editor in print mode.

        返回空提示词：打印模式没有编辑器。
        """
        return ""

    def request_render(self) -> None:
        """Do nothing: there is no frontend to re-render.

        不执行操作：没有可重新渲染的前端。
        """

    def set_slot_widget(
        self,
        key: str,
        content: SlotWidgetContent | None,
        *,
        placement: Placement = "above_prompt",
    ) -> None:
        """Do nothing: there is no slot to mount into.

        不执行操作：没有可挂载的槽位。
        """

    def open_main_view(self, factory: MainViewFactory) -> MainViewHandle:
        """Return a dead handle: there is no main area to host a view.

        返回失效句柄：没有可承载视图的主区域。
        """
        return _DeadMainViewHandle()

    def register_key_interceptor(self, handler: KeyInterceptor) -> Callable[[], None]:
        """Return a no-op unsubscribe: no key stream to intercept.

        返回无操作取消订阅：没有可拦截的按键流。
        """
        return lambda: None

    @property
    def supports_sidebar(self) -> bool:
        """Return False: print mode has no interactive sidebar.

        返回 False：打印模式没有交互式侧边栏。
        """
        return False

    def set_sidebar_section(
        self,
        extension_name: str,
        key: str,
        *,
        title: str,
        content: SidebarContent,
    ) -> None:
        """Do nothing: there is no sidebar in print mode.

        不执行操作：打印模式没有侧边栏。
        """

    def remove_sidebar_section(self, extension_name: str, key: str) -> None:
        """Do nothing: there is no sidebar in print mode.

        不执行操作：打印模式没有侧边栏。
        """

    def clear_components(self) -> None:
        """Do nothing: no components were ever mounted.

        不执行操作：从未挂载任何组件。
        """


class StderrUiBridge(NullUiBridge):
    """UI bridge that writes extension notifications to stderr (print mode).

    在打印模式下将扩展通知写入标准错误的 UI 桥接器。

    Inherits the Pi no-op dialog defaults from `NullUiBridge`; only
    `notify` is observable in print mode.

    它继承 NullUiBridge 的 Pi 无操作对话框默认值；打印模式中只有 `notify` 可观察。
    """

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Print the notification to stderr.

        将通知打印到标准错误。
        """
        print(f"[extension:{level}] {message}", file=sys.stderr)


@dataclass(frozen=True, slots=True)
class ExtensionCommandContext:
    """Context passed to extension slash-command handlers.

    传递给扩展斜杠命令处理器的上下文。
    """

    name: str
    args: str
    api: ExtensionAPI


class ExtensionUi:
    """Interactive UI facade exposed to extensions as `context.ui`.

    以 `context.ui` 暴露给扩展的交互式 UI 门面。

    Mirrors Pi's `ctx.ui`: async `select`/`confirm`/`input` dialogs plus a
    synchronous `notify`. Every call delegates to the host UI bridge, which
    returns the Pi no-op defaults when no interactive frontend is attached.
    Every member (trivial reads included, matching Pi) asserts the owning
    load generation is still active and raises :class:`ExtensionError` when
    the facade was captured before a `/reload`.

    此门面与 Pi 的 `ctx.ui` 对齐：提供异步选择、确认和输入对话框以及同步通知。每次调用
    都委托给宿主 UI 桥接器；没有交互式前端时返回 Pi 的无操作默认值。所有成员都会检查
    所属加载代际，重载前捕获的门面会抛出 ExtensionError。
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        generation: ExtensionGeneration | None = None,
        *,
        extension_name: str = "",
    ) -> None:
        """Bind the UI facade and sidebar to one extension generation.

        将 UI 门面和侧边栏绑定到一个扩展代际。
        """
        self._runtime = runtime
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._sidebar = ExtensionSidebar(runtime, extension_name, self._generation)

    @property
    def has_ui(self) -> bool:
        """Return whether an interactive UI is attached.

        返回是否附加了交互式 UI。
        """
        self._generation.assert_active()
        return self._runtime.ui.has_ui

    async def select(
        self,
        title: str,
        options: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Prompt the user to pick an option; None on cancel/no UI.

        提示用户选择选项；取消或没有 UI 时返回 None。
        """
        self._generation.assert_active()
        return await self._runtime.ui.select(title, options, timeout=timeout)

    async def confirm(
        self,
        title: str,
        message: str,
        *,
        timeout: float | None = None,
    ) -> bool:
        """Ask the user to confirm; True only if confirmed.

        请求用户确认；仅确认时返回 True。
        """
        self._generation.assert_active()
        return await self._runtime.ui.confirm(title, message, timeout=timeout)

    async def input(
        self,
        title: str,
        placeholder: str = "",
        *,
        timeout: float | None = None,
    ) -> str | None:
        """Prompt the user for text; None on cancel/no UI.

        提示用户输入文本；取消或没有 UI 时返回 None。
        """
        self._generation.assert_active()
        return await self._runtime.ui.input(title, placeholder, timeout=timeout)

    @property
    def sidebar(self) -> ExtensionSidebar:
        """Return this extension's host-framed sidebar capability.

        返回此扩展由宿主框定的侧边栏能力。
        """
        self._generation.assert_active()
        return self._sidebar

    @property
    def components(self) -> ComponentBridge:
        """Return the host widget-hosting capability.

        Straight pass-through to the installed UI bridge, which implements the
        :class:`ComponentBridge` members (the TUI hosts real widgets; the
        print-mode bridges are no-ops with ``supports_components == False``).
        Gate widget work on ``context.ui.components.supports_components``.
        A stale facade raises here, before the bridge is ever reachable.

        此属性直接透传已安装 UI 桥接器。TUI 承载真实控件，打印模式桥接器为无操作实现。
        应使用 supports_components 控制控件工作；过期门面会在到达桥接器前抛出异常。
        """
        self._generation.assert_active()
        return cast("ComponentBridge", self._runtime.ui)

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Show a notification in the UI, if one is attached.

        如果附加了 UI，则在其中显示通知。
        """
        self._generation.assert_active()
        self._runtime.ui.notify(message, level)


class ExtensionContext:
    """Read-only session context exposed to extensions.

    暴露给扩展的只读会话上下文。

    Every property (trivial reads included, matching Pi's context getters)
    asserts the owning load generation is still active, so a context captured
    before a `/reload` raises :class:`ExtensionError` instead of reading the
    reloaded world.

    每个属性都会检查所属加载代际；重载前捕获的上下文会抛出 ExtensionError，而不会读取
    重载后的环境。
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        generation: ExtensionGeneration | None = None,
        *,
        extension_name: str = "",
    ) -> None:
        """Bind read-only session and UI access to one extension generation.

        将只读会话与 UI 访问绑定到一个扩展代际。
        """
        self._runtime = runtime
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._ui = ExtensionUi(runtime, self._generation, extension_name=extension_name)

    @property
    def cwd(self) -> Path:
        """Return the session working directory.

        返回会话工作目录。
        """
        self._generation.assert_active()
        return self._runtime.session_view.cwd

    @property
    def paths(self) -> TauPaths:
        """Return the resolved Tau filesystem paths for this session.

        返回此会话已解析的 Tau 文件系统路径。
        """
        self._generation.assert_active()
        return self._runtime.paths

    @property
    def model(self) -> str:
        """Return the active model name.

        返回活动模型名称。
        """
        self._generation.assert_active()
        return self._runtime.session_view.model

    @property
    def provider_name(self) -> str:
        """Return the active provider name.

        返回活动提供商名称。
        """
        self._generation.assert_active()
        return self._runtime.session_view.provider_name

    @property
    def inference_provider(self) -> str | None:
        """Return the active Hugging Face inference-provider pin, if any.

        返回活动 Hugging Face 推理提供商固定值（若有）。
        """
        self._generation.assert_active()
        return self._runtime.session_view.inference_provider

    @property
    def inference_provider_mode(self) -> str:
        """Return whether Hugging Face routing is automatic or explicitly fixed.

        返回 Hugging Face 路由是自动选择还是显式固定。
        """
        self._generation.assert_active()
        return self._runtime.session_view.inference_provider_mode

    @property
    def session_id(self) -> str | None:
        """Return the current session id, if the session is indexed.

        如果会话已建立索引，则返回当前会话 ID。
        """
        self._generation.assert_active()
        return self._runtime.session_view.session_id

    @property
    def session_name(self) -> str | None:
        """Return the session's human-friendly name, if it has one.

        返回会话的易读名称（若有）。
        """
        self._generation.assert_active()
        return self._runtime.session_view.session_name

    @property
    def thinking_level(self) -> str:
        """Return the active thinking mode for future turns.

        返回后续轮次使用的活动思考模式。
        """
        self._generation.assert_active()
        return self._runtime.session_view.thinking_level

    @property
    def system_prompt(self) -> str:
        """Return the active system prompt.

        返回活动系统提示词。
        """
        self._generation.assert_active()
        return self._runtime.session_view.system_prompt

    @property
    def is_running(self) -> bool:
        """Return whether an agent run is currently active.

        返回代理运行当前是否活动。
        """
        self._generation.assert_active()
        return self._runtime.session_view.is_running

    @property
    def transcript(self) -> tuple[AgentMessage, ...]:
        """Return the active-path parent conversation as read-only copies.

        Mirrors the read access Pi extensions get via
        ``ctx.sessionManager.getBranch()``: the user/assistant/tool messages on
        the current branch, with compaction and branch summaries already folded
        in as ``UserMessage`` entries (Tau has no separate summary message
        type). Each message is deep-copied so an extension mutating a returned
        object cannot corrupt the live session transcript.

        此读取与 Pi 的分支读取能力对齐，返回当前分支上的用户、助手和工具消息；摘要已折叠
        为 UserMessage。每条消息都会深拷贝，扩展修改返回对象不会破坏活动记录。
        """
        self._generation.assert_active()
        messages = self._runtime.session_view.messages
        return tuple(message.model_copy(deep=True) for message in messages)

    @property
    def has_ui(self) -> bool:
        """Return whether an interactive UI is attached.

        返回是否附加了交互式 UI。
        """
        self._generation.assert_active()
        return self._runtime.ui.has_ui

    @property
    def ui(self) -> ExtensionUi:
        """Return the interactive UI facade (Pi's `ctx.ui`).

        Use `await context.ui.select/confirm/input(...)` to drive dialogs.
        Because command handlers are sync (see the docs), a `/command` that
        needs a dialog should spawn a loop task that awaits `context.ui`.

        使用异步 select、confirm 或 input 驱动对话框。同步命令处理器需要对话框时，应启动
        一个等待 context.ui 的事件循环任务。
        """
        self._generation.assert_active()
        return self._ui


class ExtensionAPI:
    """The object handed to each extension's `setup(tau)` entry point.

    传递给每个扩展 `setup(tau)` 入口点的对象。

    Every method and property asserts the load generation first (Pi's
    ``assertActive`` parity): after `/reload` replaces the registration set,
    a `tau` object captured by the previous instance raises
    :class:`ExtensionError` on any use instead of silently acting against
    the new world.

    每个方法和属性都会先检查加载代际；重载替换注册集合后，旧实例捕获的 `tau` 对象会在
    任何使用时抛出 ExtensionError，而不会静默作用于新环境。
    """

    def __init__(
        self,
        runtime: ExtensionRuntime,
        extension_name: str,
        generation: ExtensionGeneration | None = None,
        *,
        source_id: str | None = None,
    ) -> None:
        """Bind one extension source to its runtime, generation, and context.

        将一个扩展来源绑定到其运行时、代际和上下文。
        """
        self._runtime = runtime
        self._extension_name = extension_name
        self._source_id = source_id or f"extension-name:{extension_name}"
        self._generation = generation if generation is not None else ExtensionGeneration()
        self._context = ExtensionContext(
            runtime,
            self._generation,
            extension_name=extension_name,
        )

    @property
    def name(self) -> str:
        """Return this extension's name.

        返回此扩展的名称。
        """
        self._generation.assert_active()
        return self._extension_name

    @property
    def context(self) -> ExtensionContext:
        """Return read-only session context.

        返回只读会话上下文。
        """
        self._generation.assert_active()
        return self._context

    def set_inference_provider(self, route: str | None) -> str:
        """Select or reset the active Hugging Face session route.

        选择或重置活动 Hugging Face 会话路由。
        """
        self._generation.assert_active()
        return self._runtime.session_view.set_inference_provider(route)

    def register_tool(self, tool: AgentTool) -> None:
        """Register an agent tool (first registration per name wins).

        注册代理工具；每个名称的首次注册生效。
        """
        self._generation.assert_active()
        self._runtime.register_tool(self._source_id, self._extension_name, tool)

    def register_provider(self, provider: DynamicProvider) -> None:
        """Register or atomically replace this source's dynamic provider layer.

        注册或原子替换此来源的动态提供商层。
        """
        self._generation.assert_active()
        self._runtime.register_provider(self._source_id, provider)

    def update_provider(self, provider: DynamicProvider) -> bool:
        """Update this source's provider snapshot while preserving its layer token.

        更新此来源的提供商快照，同时保留其层令牌。
        """
        self._generation.assert_active()
        return self._runtime.update_provider(self._source_id, provider)

    def register_local_backend(self, backend: LocalBackend) -> None:
        """Register a backend paired with this source's provider layer.

        注册与此来源提供商层配对的后端。
        """
        self._generation.assert_active()
        self._runtime.register_local_backend(self._source_id, backend)

    def register_command(
        self,
        name: str,
        handler: ExtensionCommandHandler,
        *,
        description: str = "",
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        """Register a slash command backed by this extension.

        注册由此扩展支持的斜杠命令。
        """
        self._generation.assert_active()
        self._runtime.register_command(
            self._source_id,
            self._extension_name,
            name,
            handler,
            description=description,
            usage=usage,
            aliases=aliases,
        )

    def add_prompt_guideline(self, guideline: str) -> None:
        """Add a standalone guideline line to the system prompt.

        Tool-attached guidance belongs on the tool (`prompt_snippet`,
        `prompt_guidelines`); this is for behavioral guidance not tied to
        any tool. Duplicate lines are de-duplicated at prompt build time.

        工具相关指导应放在工具自身；此方法用于不绑定工具的行为指导。构建提示词时会去重。
        """
        self._generation.assert_active()
        self._runtime.register_prompt_guideline(self._source_id, self._extension_name, guideline)

    def add_prompt_section(self, title: str | None, body: str) -> None:
        """Append a free-form, optionally titled section to the system prompt.

        Use this for structured, always-on extension context such as procedures,
        paragraphs, and code blocks. Use :meth:`add_prompt_guideline` for one
        behavioral bullet instead.

        此方法用于流程、段落和代码块等始终生效的结构化扩展上下文；单条行为要点应使用
        add_prompt_guideline。
        """
        self._generation.assert_active()
        self._runtime.register_prompt_section(
            self._source_id,
            self._extension_name,
            title,
            body,
        )

    def on(
        self,
        event: str,
        handler: ExtensionHandler | None = None,
    ) -> Callable[[ExtensionHandler], ExtensionHandler] | ExtensionHandler:
        """Subscribe to an event, directly or as a decorator.

        直接或以装饰器形式订阅事件。
        """
        self._generation.assert_active()
        if handler is not None:
            self._runtime.subscribe(self._source_id, event, handler)
            return handler

        def decorator(decorated: ExtensionHandler) -> ExtensionHandler:
            """Register and return a decorated extension event handler.

            注册并返回被装饰的扩展事件处理器。
            """
            self._generation.assert_active()
            self._runtime.subscribe(self._source_id, event, decorated)
            return decorated

        return decorator

    def send_user_message(
        self,
        content: str,
        *,
        deliver_as: DeliverAs = "follow_up",
    ) -> None:
        """Queue a user message for the active or next agent run.

        为当前或下一次代理运行排队用户消息。
        """
        self._generation.assert_active()
        self._runtime.send_user_message(content, deliver_as=deliver_as)

    def register_message_renderer(
        self,
        custom_type: str,
        renderer: MessageRenderer,
    ) -> None:
        """Register a renderer for custom messages with this ``custom_type``.

        Ports Pi's ``registerMessageRenderer``: the first registration per
        ``custom_type`` wins. The renderer receives a :class:`CustomMessageView`
        and :class:`MessageRenderOptions` and returns a Rich-markup string; it
        must not return a Textual widget (that keeps extensions TUI-free).

        此方法移植 Pi 的 registerMessageRenderer；每种 custom_type 的首次注册生效。渲染器
        接收消息视图和选项并返回 Rich 标记字符串，不能返回 Textual 控件。
        """
        self._generation.assert_active()
        self._runtime.register_message_renderer(
            self._source_id, self._extension_name, custom_type, renderer
        )

    def send_custom_message(
        self,
        content: str,
        *,
        custom_type: str,
        details: dict[str, JSONValue] | None = None,
        deliver_as: DeliverAs = "follow_up",
        trigger_turn: bool = True,
    ) -> None:
        """Send a custom message that renders via a registered renderer.

        Ports Pi's ``sendMessage``: ``content`` still enters LLM context, while
        ``custom_type``/``details`` let a registered renderer format the
        transcript block. With ``trigger_turn`` (the default) the message starts
        a turn when the session is idle, mirroring ``send_user_message``; set it
        to ``False`` to only queue for the next run.

        此方法移植 Pi 的 sendMessage：content 仍进入 LLM 上下文，custom_type 和 details
        用于格式化记录块。默认会在空闲时启动轮次；设为 False 时只为下次运行排队。
        """
        self._generation.assert_active()
        self._runtime.send_custom_message(
            content,
            custom_type=custom_type,
            details=details,
            deliver_as=deliver_as,
            trigger_turn=trigger_turn,
        )

    async def append_entry(self, namespace: str, data: dict[str, JSONValue]) -> None:
        """Persist extension-owned data to the session as a custom entry.

        将扩展拥有的数据作为自定义条目持久化到会话。
        """
        self._generation.assert_active()
        await self._runtime.append_custom_entry(namespace, data)

    async def set_label(self, entry_id: str, label: str | None) -> None:
        """Set or clear a bookmark on an existing session entry.

        在现有会话条目上设置或清除书签。
        """
        self._generation.assert_active()
        await self._runtime.set_label(entry_id, label)

    def notify(self, message: str, level: NotifyLevel = "info") -> None:
        """Show a notification in the UI, if one is attached.

        如果附加了 UI，则在其中显示通知。
        """
        self._generation.assert_active()
        self._runtime.ui.notify(message, level)


@dataclass(slots=True)
class RegisteredExtension:
    """Book-keeping for one loaded extension inside the runtime.

    运行时内部一个已加载扩展的记录信息。
    """

    name: str
    source_id: str
    path: Path | None
    api: ExtensionAPI
    source: Literal["built-in", "user", "explicit", "project"] = "explicit"
    hidden: bool = False
    handlers: dict[str, list[ExtensionHandler]] = field(default_factory=dict)
