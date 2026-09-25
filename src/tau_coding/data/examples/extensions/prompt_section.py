"""Append structured, always-on instructions to Tau's system prompt.

向 Tau 的系统提示词追加结构化且始终生效的说明。
"""

from tau_coding.extensions import ExtensionAPI


def setup(tau: ExtensionAPI) -> None:
    """Add a labeled procedure while this extension generation is active.

    在此扩展代际处于活动状态时添加带标签的流程。
    """
    tau.add_prompt_section(
        "Review procedure",
        """Read the complete diff before editing.

Run the relevant checks before reporting success:

```bash
uv run pytest
uv run ruff check .
```
""",
    )
