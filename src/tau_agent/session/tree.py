"""Session tree traversal helpers."""

# 会话树遍历辅助函数。

from __future__ import annotations

from tau_agent.session.entries import SessionEntry


class SessionTreeError(ValueError):
    """Raised when session entries do not form a valid traversable tree."""

    # 当会话条目无法构成有效的可遍历树时抛出。


def entries_by_id(entries: list[SessionEntry]) -> dict[str, SessionEntry]:
    """Return entries keyed by id, rejecting duplicates."""

    # 返回按标识符索引的条目，并拒绝重复标识符。
    result: dict[str, SessionEntry] = {}
    for entry in entries:
        if entry.id in result:
            raise SessionTreeError(f"Duplicate session entry id: {entry.id}")
        result[entry.id] = entry
    return result


def path_to_entry(entries: list[SessionEntry], leaf_id: str) -> list[SessionEntry]:
    """Return the root-to-leaf path for `leaf_id`."""

    # 返回指定 `leaf_id` 从根到叶的路径。

    # Build an index once, then follow parent pointers from the leaf upward.
    #
    # 先构建一次索引，再从叶节点沿父指针向上追溯。
    by_id = entries_by_id(entries)
    path: list[SessionEntry] = []
    seen: set[str] = set()
    current_id: str | None = leaf_id

    while current_id is not None:
        if current_id in seen:
            raise SessionTreeError(f"Cycle detected at session entry: {current_id}")
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            raise SessionTreeError(f"Missing session entry: {current_id}")
        path.append(entry)
        current_id = entry.parent_id

    # Reverse the collected leaf-to-root chain into traversal order.
    #
    # 将收集到的叶到根链反转为遍历顺序。
    path.reverse()
    return path
