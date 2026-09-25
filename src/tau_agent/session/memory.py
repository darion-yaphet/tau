"""In-memory session state reconstruction.

内存中的会话状态重建。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from tau_agent.messages import AgentMessage, CustomMessage, UserMessage
from tau_agent.session.entries import (
    BranchSummaryEntry,
    CompactionEntry,
    CustomEntry,
    SessionEntry,
    SessionInfoEntry,
)
from tau_agent.session.tree import path_to_entry

_UNSET_LEAF_ID: Final[object] = object()


@dataclass(frozen=True, slots=True)
class SessionState:
    """Current session state derived from append-only entries.

    由仅追加记录推导出的当前会话状态。
    """

    messages: tuple[AgentMessage, ...]
    model: str | None
    provider: str | None
    thinking_level: str | None
    labels_by_id: dict[str, str]
    label_timestamps_by_id: dict[str, float]
    active_leaf_id: str | None
    session_info: SessionInfoEntry | None
    custom_entries: tuple[CustomEntry, ...]
    compaction_entries: tuple[CompactionEntry, ...]
    context_entry_ids: tuple[str, ...]
    entries: tuple[SessionEntry, ...]

    @classmethod
    def from_entries(
        cls,
        entries: list[SessionEntry],
        *,
        leaf_id: str | None | object = _UNSET_LEAF_ID,
    ) -> SessionState:
        """Replay the branch ending at the active entry.

        重放以活动记录结尾的分支。

        By default the active entry is the last non-leaf entry in file order.
        Historical ``leaf`` records remain readable but never select the tip.
        An explicit `leaf_id` supports in-memory tree navigation; passing
        ``None`` selects the empty path before the first root entry.

        默认情况下，活动记录是按文件顺序排列的最后一条非叶子记录。
        历史 ``leaf`` 记录仍可读取，但永远不会选择末端。显式传入 `leaf_id`
        可支持内存中的树导航；传入 ``None`` 会选择第一条根记录之前的空路径。
        """
        # Resolve the active tip, then derive the single branch to replay.
        #
        # 先解析活动末端，再推导出要重放的单一分支。
        resolved_leaf_id = (
            _last_non_leaf_id(entries) if leaf_id is _UNSET_LEAF_ID else cast(str | None, leaf_id)
        )
        replay_entries = (
            path_to_entry(entries, resolved_leaf_id) if resolved_leaf_id is not None else []
        )

        # Initialize accumulated state, including labels resolved across the full tree.
        #
        # 初始化累积状态，其中包括在整棵树上解析的标签。
        message_rows: list[tuple[str, AgentMessage]] = []
        model: str | None = None
        provider: str | None = None
        thinking_level: str | None = None
        labels_by_id, label_timestamps_by_id = _resolve_labels(entries)
        active_leaf_id: str | None = resolved_leaf_id
        session_info: SessionInfoEntry | None = None
        custom_entries: list[CustomEntry] = []
        compaction_entries: list[CompactionEntry] = []

        # Fold each branch entry into messages, selections, metadata, and summaries.
        #
        # 将每条分支记录折叠到消息、选择项、元数据和摘要中。
        for entry_index, entry in enumerate(replay_entries):
            match entry.type:
                case "message":
                    message_rows.append((entry.id, entry.message))
                case "custom_message":
                    message_rows.append(
                        (
                            entry.id,
                            CustomMessage(
                                custom_type=entry.custom_type,
                                content=entry.content,
                                display=entry.display,
                                details=entry.details,
                                timestamp=round(entry.timestamp * 1000),
                            ),
                        )
                    )
                case "model_change":
                    model = entry.model
                    if entry.provider is not None:
                        provider = entry.provider
                case "thinking_level_change":
                    thinking_level = entry.thinking_level
                case "label":
                    pass  # Resolved globally above so off-path bookmarks remain visible.
                    #
                    # 已在上方全局解析，因此路径外的书签仍然可见。
                case "leaf":
                    pass  # Backward-compatible historical record; never selects the tip.
                    #
                    # 向后兼容的历史记录；永远不会选择末端。
                case "session_info":
                    session_info = entry
                case "custom":
                    custom_entries.append(entry)
                case "compaction":
                    compaction_entries.append(entry)
                    message_rows = _apply_compaction(
                        message_rows,
                        entry,
                        path_before=replay_entries[:entry_index],
                    )
                case "branch_summary":
                    message_rows.append(
                        (entry.id, UserMessage(content=_format_branch_summary(entry)))
                    )

        return cls(
            messages=tuple(message for _entry_id, message in message_rows),
            model=model,
            provider=provider,
            thinking_level=thinking_level,
            labels_by_id=labels_by_id,
            label_timestamps_by_id=label_timestamps_by_id,
            active_leaf_id=active_leaf_id,
            session_info=session_info,
            custom_entries=tuple(custom_entries),
            compaction_entries=tuple(compaction_entries),
            context_entry_ids=tuple(entry_id for entry_id, _message in message_rows),
            entries=tuple(replay_entries),
        )


def _resolve_labels(entries: list[SessionEntry]) -> tuple[dict[str, str], dict[str, float]]:
    """Resolve the latest visible label and timestamp for every target entry.

    解析每条目标记录最新的可见标签和时间戳。
    """
    labels: dict[str, str] = {}
    timestamps: dict[str, float] = {}
    for entry in entries:
        if entry.type != "label":
            continue
        label = entry.label.strip() if entry.label is not None else ""
        if label:
            labels[entry.target_id] = label
            timestamps[entry.target_id] = entry.timestamp
        else:
            labels.pop(entry.target_id, None)
            timestamps.pop(entry.target_id, None)
    return labels, timestamps


def _last_non_leaf_id(entries: list[SessionEntry]) -> str | None:
    """Return the identifier of the last entry that is not a legacy leaf.

    返回最后一条非历史叶子记录的标识符。
    """
    for entry in reversed(entries):
        if entry.type != "leaf":
            return entry.id
    return None


def _apply_compaction(
    message_rows: list[tuple[str, AgentMessage]],
    entry: CompactionEntry,
    *,
    path_before: list[SessionEntry],
) -> list[tuple[str, AgentMessage]]:
    """Replace compacted context with its summary and retain the active suffix.

    用摘要替换已压缩的上下文，并保留活动后缀。
    """
    summary_row = (entry.id, UserMessage(content=_format_compaction_summary(entry.summary)))

    # Tau originally persisted arbitrary replacement-id sets. Explicit legacy
    # fields take precedence, including an empty list, so old sessions retain
    # their exact replay behavior.
    #
    # Tau 最初会持久化任意的替换标识符集合。显式的旧字段优先，
    # 包括空列表，从而使旧会话保留其确切的重放行为。
    if "replaces_entry_ids" in entry.model_fields_set:
        replaced_ids = set(entry.replaces_entry_ids)
        retained: list[tuple[str, AgentMessage]] = []
        inserted_summary = False
        for entry_id, message in message_rows:
            if entry_id not in replaced_ids:
                retained.append((entry_id, message))
                continue
            if not inserted_summary:
                retained.append(summary_row)
                inserted_summary = True
        if not inserted_summary:
            retained.append(summary_row)
        return retained

    # Pi resolves the boundary against the complete active path, not only
    # message-producing entries. Reorder retained context by that path as well:
    # a previous compaction can itself occur after the kept boundary.
    #
    # Pi 会根据完整的活动路径解析边界，而不只是产生消息的记录。
    # 也要按该路径重新排列保留的上下文：之前的压缩本身可能发生在保留边界之后。
    first_kept_index = next(
        (
            index
            for index, path_entry in enumerate(path_before)
            if path_entry.id == entry.first_kept_entry_id
        ),
        None,
    )
    if first_kept_index is None:
        return [summary_row]

    retained_by_id = dict(message_rows)
    retained = [
        (path_entry.id, retained_by_id[path_entry.id])
        for path_entry in path_before[first_kept_index:]
        if path_entry.id in retained_by_id
    ]
    return [summary_row, *retained]


def _format_compaction_summary(summary: str) -> str:
    """Format a compaction summary as a synthetic user message.

    将压缩摘要格式化为合成的用户消息。
    """
    return f"Previous conversation summary:\n{summary}"


def _format_branch_summary(entry: BranchSummaryEntry) -> str:
    """Format a returned branch summary as a synthetic user message.

    将返回的分支摘要格式化为合成的用户消息。
    """
    return (
        "The following is a summary of a branch that this conversation came back from:\n"
        f"<summary>\n{entry.summary}\n</summary>"
    )
