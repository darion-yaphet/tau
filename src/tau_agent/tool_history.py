"""Provider-safe repair of malformed tool-call message history.

以模型提供者安全的方式修复格式异常的工具调用消息历史。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from tau_agent.messages import (
    AgentMessage,
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
)

_INTERRUPTED_TOOL_RESULT = "Tool call interrupted by user"


@dataclass(frozen=True, slots=True)
class ToolHistoryRepair:
    """A provider-safe transcript plus a summary of deterministic repairs.

    一份对模型提供者安全的记录，以及确定性修复的摘要。
    """

    messages: tuple[AgentMessage, ...]
    changed: bool = False
    synthesized_results: int = 0
    dropped_orphan_results: int = 0
    dropped_duplicate_results: int = 0
    reordered_results: int = 0

    def diagnostic_data(self) -> dict[str, int]:
        """Return JSON-safe counters for durable session diagnostics.

        返回可安全序列化为 JSON 的计数器，用于持久会话诊断。
        """
        return {
            "synthesizedResults": self.synthesized_results,
            "droppedOrphanResults": self.dropped_orphan_results,
            "droppedDuplicateResults": self.dropped_duplicate_results,
            "reorderedResults": self.reordered_results,
        }


def repair_tool_history(messages: tuple[AgentMessage, ...]) -> ToolHistoryRepair:
    """Return history where every tool call has exactly one adjacent result.

    返回修复后的历史，使每个工具调用都恰好有一个相邻结果。

    Existing result messages are moved beside their calls. Missing results get a
    deterministic interruption error. Results with no call are omitted because
    a missing call's arguments cannot be reconstructed safely. When duplicate
    results exist, a real result is preferred over Tau's synthetic interruption.

    现有结果消息会被移到对应调用旁边。缺失的结果会获得确定性中断错误。
    由于无法安全重建缺失调用的参数，没有对应调用的结果会被忽略。
    存在重复结果时，优先使用真实结果，而非 Tau 合成的中断结果。
    """
    # Index every tool-call occurrence and every available result by position.

    # 按位置为每个工具调用出现项和每个可用结果建立索引。
    call_occurrences: list[tuple[tuple[int, int], ToolCall, int]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, AssistantMessage):
            continue
        for call_offset, call in enumerate(message.tool_calls, start=1):
            call_occurrences.append(
                ((message_index, call_offset), call, message_index + call_offset)
            )

    results_by_id: dict[str, list[tuple[int, ToolResultMessage]]] = defaultdict(list)
    for message_index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage):
            results_by_id[message.tool_call_id].append((message_index, message))

    selected_results: dict[tuple[int, int], tuple[int | None, ToolResultMessage]] = {}
    used_result_positions: set[int] = set()

    # Reserve already-adjacent pairs first. This keeps valid repeated IDs paired
    # with their own turn rather than letting an earlier occurrence consume them.

    # 先保留已相邻的配对。这会使有效的重复 ID 与各自轮次保持配对，
    # 避免它们被更早的出现项占用。
    for occurrence, call, expected_position in call_occurrences:
        if expected_position >= len(messages):
            continue
        candidate = messages[expected_position]
        if (
            isinstance(candidate, ToolResultMessage)
            and candidate.tool_call_id == call.id
            and expected_position not in used_result_positions
        ):
            selected_results[occurrence] = (expected_position, candidate)
            used_result_positions.add(expected_position)

    # Match remaining calls to real results, or synthesize interruption results.

    # 将剩余调用与真实结果匹配，或合成中断结果。
    synthesized_results = 0
    for occurrence, call, _expected_position in call_occurrences:
        if occurrence in selected_results:
            continue
        candidates = [
            candidate
            for candidate in results_by_id.get(call.id, [])
            if candidate[0] not in used_result_positions
        ]
        if candidates:
            after_call = [candidate for candidate in candidates if candidate[0] > occurrence[0]]
            candidate_pool = after_call or candidates
            selected = next(
                (
                    candidate
                    for candidate in candidate_pool
                    if not _is_interruption_result(candidate[1])
                ),
                candidate_pool[0],
            )
            selected_results[occurrence] = selected
            used_result_positions.add(selected[0])
            continue
        selected_results[occurrence] = (
            None,
            ToolResultMessage(
                tool_call_id=call.id,
                tool_name=call.name,
                content=[TextContent(text=_INTERRUPTED_TOOL_RESULT)],
                is_error=True,
            ),
        )
        synthesized_results += 1

    # If every call occurrence is already paired and a real extra result remains,
    # prefer it over a selected synthetic interruption for the same ID.

    # 如果每个调用出现项都已配对，但仍有额外真实结果，
    # 则对同一 ID 优先使用该真实结果，而非已选中的合成中断结果。
    for occurrence, call, _expected_position in call_occurrences:
        selected_position, selected_result = selected_results[occurrence]
        if selected_position is None or not _is_interruption_result(selected_result):
            continue
        replacement = next(
            (
                candidate
                for candidate in results_by_id.get(call.id, [])
                if candidate[0] not in used_result_positions
                and not _is_interruption_result(candidate[1])
            ),
            None,
        )
        if replacement is None:
            continue
        used_result_positions.remove(selected_position)
        used_result_positions.add(replacement[0])
        selected_results[occurrence] = replacement

    # Rebuild the transcript with one selected result immediately after each call.

    # 重建记录，使每个调用后立即跟随一个选定结果。
    repaired: list[AgentMessage] = []
    reordered_results = 0
    for message_index, message in enumerate(messages):
        if isinstance(message, ToolResultMessage):
            continue
        repaired.append(message)
        if not isinstance(message, AssistantMessage):
            continue
        for call_offset, _call in enumerate(message.tool_calls, start=1):
            result_position, result = selected_results[(message_index, call_offset)]
            repaired.append(result)
            if result_position is not None and result_position != message_index + call_offset:
                reordered_results += 1

    call_ids = {call.id for _occurrence, call, _expected in call_occurrences}
    unused_results = [
        result
        for results in results_by_id.values()
        for position, result in results
        if position not in used_result_positions
    ]
    orphan_results = sum(result.tool_call_id not in call_ids for result in unused_results)
    dropped_duplicate_results = sum(result.tool_call_id in call_ids for result in unused_results)
    repaired_messages = tuple(repaired)
    return ToolHistoryRepair(
        messages=repaired_messages,
        changed=repaired_messages != messages,
        synthesized_results=synthesized_results,
        dropped_orphan_results=orphan_results,
        dropped_duplicate_results=dropped_duplicate_results,
        reordered_results=reordered_results,
    )


def _is_interruption_result(message: ToolResultMessage) -> bool:
    """Return whether a result is Tau's synthetic interruption marker.

    返回结果是否为 Tau 合成的中断标记。
    """
    return message.is_error and message.text == _INTERRUPTED_TOOL_RESULT
