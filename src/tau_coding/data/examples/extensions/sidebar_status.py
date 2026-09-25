"""Add a small, host-framed status section to Tau's TUI sidebar.

向 Tau 的 TUI 侧边栏添加一个由宿主框定的小型状态区块。
"""

from tau_coding.extensions import ExtensionAPI, ExtensionContext


def setup(tau: ExtensionAPI) -> None:
    """Show and update a turn counter when the active frontend has a sidebar.

    当活动前端带有侧边栏时，显示并更新轮次计数器。
    """
    turn_count = 0

    def show(context: ExtensionContext) -> None:
        """Render the current turn count in the extension sidebar section.

        在扩展侧边栏区块中渲染当前轮次计数。
        """
        sidebar = getattr(context.ui, "sidebar", None)
        if sidebar is not None and sidebar.supported:
            sidebar.set_section(
                "turns",
                title="extension status",
                content=[f"[green]{turn_count}[/green] completed turns"],
            )

    def on_session_start(event: object, context: ExtensionContext) -> None:
        """Reset and display the counter when a session starts.

        会话开始时重置并显示计数器。
        """
        nonlocal turn_count
        del event
        turn_count = 0
        show(context)

    def on_turn_end(event: object, context: ExtensionContext) -> None:
        """Increment and redisplay the counter after each turn.

        每轮结束后递增并重新显示计数器。
        """
        nonlocal turn_count
        del event
        turn_count += 1
        show(context)

    def on_session_shutdown(event: object, context: ExtensionContext) -> None:
        """Remove the extension sidebar section during shutdown.

        关闭期间移除扩展侧边栏区块。
        """
        del event
        sidebar = getattr(context.ui, "sidebar", None)
        if sidebar is not None:
            sidebar.remove_section("turns")

    tau.on("session_start", on_session_start)
    tau.on("turn_end", on_turn_end)
    tau.on("session_shutdown", on_session_shutdown)
