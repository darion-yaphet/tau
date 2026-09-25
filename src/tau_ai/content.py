"""Shared helpers for provider serialization of multimodal message content.

为模型提供者序列化多模态消息内容提供的共享辅助函数。
"""

from __future__ import annotations

from collections.abc import Sequence

from tau_agent.messages import ImageContent, TextContent, ToolResultMessage, UserMessage

NON_VISION_USER_IMAGE_PLACEHOLDER = (
    "(image omitted: current model does not support image input; image contents are "
    "unavailable—do not infer or describe them)"
)
NON_VISION_TOOL_IMAGE_PLACEHOLDER = (
    "(tool image omitted: current model does not support image input; image contents are "
    "unavailable—do not infer or describe them; ask the user to switch to a vision-capable model)"
)


def messages_have_images(messages: Sequence[object]) -> bool:
    """Return whether user or tool-result context contains image blocks.

    返回用户或工具结果上下文中是否包含图像块。
    """
    return any(
        isinstance(message, (UserMessage, ToolResultMessage))
        and not isinstance(message.content, str)
        and any(isinstance(block, ImageContent) for block in message.content)
        for message in messages
    )


def text_and_images(
    content: str | Sequence[TextContent | ImageContent],
    *,
    supports_images: bool,
    image_placeholder: str,
) -> tuple[str, list[ImageContent]]:
    """Return visible text and sendable images, downgrading unsupported images.

    返回可见文本与可发送的图像；不支持的图像会降级为占位文本。
    """
    if isinstance(content, str):
        return content, []

    text = "".join(block.text for block in content if isinstance(block, TextContent))
    images = [block for block in content if isinstance(block, ImageContent)]
    if supports_images:
        return text, images
    if images:
        text = f"{text}\n{image_placeholder}" if text else image_placeholder
    return text, []
