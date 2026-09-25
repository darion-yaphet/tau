"""Portable tool-call identifiers for cross-provider history replay.

用于跨提供者重放历史记录的可移植工具调用标识符。
"""

from __future__ import annotations

import re
from hashlib import sha256

# Anthropic has the strictest documented identifier alphabet among Tau's
# providers. Keeping translated IDs within this common subset also avoids
# provider-specific punctuation and length constraints elsewhere.
#
# 在 Tau 的模型提供者中，Anthropic 文档规定的标识符字符集限制最严格。
# 将转换后的 ID 限定在这一共同子集中，也可避开其他提供者特有的
# 标点符号与长度限制。
_PORTABLE_TOOL_CALL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def portable_tool_call_id(value: str) -> str:
    """Return a deterministic provider-safe correlation ID.

    返回一个确定且兼容各模型提供者的关联 ID。

    Provider-native IDs that already fit the common format remain unchanged,
    preserving same-provider cache/replay behavior. Other IDs are hashed rather
    than character-replaced so distinct native IDs cannot collapse together.

    已符合通用格式的提供者原生 ID 会保持不变，以保留同一提供者下的缓存与
    重放行为。其他 ID 会被哈希处理，而不是替换字符，避免不同的原生 ID
    被转换成同一个值。
    """
    if _PORTABLE_TOOL_CALL_ID.fullmatch(value):
        return value
    digest = sha256(value.encode("utf-8")).hexdigest()
    return f"tc_{digest[:40]}"
