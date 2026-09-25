"""Append-only session entry models."""

# 仅追加的会话条目模型。

from __future__ import annotations

from time import time
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from tau_agent.messages import AgentMessage, Usage, UserContent
from tau_agent.types import JSONValue


def new_entry_id() -> str:
    """Return a unique session entry id."""

    # 返回唯一的会话条目标识符。
    return uuid4().hex


def current_timestamp() -> float:
    """Return the current Unix timestamp."""

    # 返回当前 Unix 时间戳。
    return time()


class BaseSessionEntry(BaseModel):
    """Common fields shared by all append-only session entries."""

    # 所有仅追加会话条目共享的通用字段。

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_entry_id)
    parent_id: str | None = None
    timestamp: float = Field(default_factory=current_timestamp)


class MessageEntry(BaseSessionEntry):
    """A transcript message entry."""

    # 一条对话记录消息条目。

    type: Literal["message"] = "message"
    message: AgentMessage


class CustomMessageEntry(BaseSessionEntry):
    """An extension-injected message that participates in model context."""

    # 由扩展注入并参与模型上下文的消息。

    type: Literal["custom_message"] = "custom_message"
    custom_type: str
    content: UserContent
    display: bool = True
    details: JSONValue = None


class ModelChangeEntry(BaseSessionEntry):
    """A model selection change entry.

    模型选择变更条目。

    ``provider`` was added after Tau's first session format.  It remains
    optional so older transcripts continue to load; new entries always carry
    the provider selected by the host.

    ``provider`` 在 Tau 第一版会话格式之后加入。为确保旧对话记录仍可加载，
    该字段保持可选；新条目则始终携带宿主选择的提供商。
    """

    type: Literal["model_change"] = "model_change"
    model: str
    provider: str | None = None


class ThinkingLevelChangeEntry(BaseSessionEntry):
    """A thinking/reasoning level change entry."""

    # 思考或推理级别变更条目。

    type: Literal["thinking_level_change"] = "thinking_level_change"
    thinking_level: str | None = None


class CompactionEntry(BaseSessionEntry):
    """A context summary that replaces older message entries during replay."""

    # 在重放期间替代较旧消息条目的上下文摘要。

    type: Literal["compaction"] = "compaction"
    summary: str
    # Legacy Tau sessions stored every replaced id. Keep accepting and replaying
    # that shape, but omit the empty compatibility field from new JSONL records.

    # 旧版 Tau 会话会存储每个被替换条目的标识符。继续接受并重放这种结构，
    # 但在新的 JSONL 记录中省略空的兼容字段。
    replaces_entry_ids: list[str] = Field(default_factory=list, exclude_if=lambda ids: not ids)
    first_kept_entry_id: str | None = None
    tokens_before: int | None = None
    usage: Usage | None = None
    provider: str | None = None
    model: str | None = None
    response_provider: str | None = None


class BranchSummaryEntry(BaseSessionEntry):
    """A future branch summary entry."""

    # 预留的分支摘要条目。

    type: Literal["branch_summary"] = "branch_summary"
    summary: str
    branch_root_id: str | None = None
    usage: Usage | None = None
    provider: str | None = None
    model: str | None = None
    response_provider: str | None = None


class LabelEntry(BaseSessionEntry):
    """A bookmark change attached to one session entry."""

    # 附加到某个会话条目的书签变更。

    type: Literal["label"] = "label"
    target_id: str
    label: str | None = None


class LeafEntry(BaseSessionEntry):
    """A legacy active-tip pointer retained only for deserialization."""

    # 仅为反序列化而保留的旧版活动末端指针。

    type: Literal["leaf"] = "leaf"
    entry_id: str | None = None


class SessionInfoEntry(BaseSessionEntry):
    """Basic session metadata entry."""

    # 基本会话元数据条目。

    type: Literal["session_info"] = "session_info"
    created_at: float = Field(default_factory=current_timestamp)
    cwd: str | None = None
    title: str | None = None


class CustomEntry(BaseSessionEntry):
    """Extension/application-owned session data."""

    # 由扩展或应用拥有的会话数据。

    type: Literal["custom"] = "custom"
    namespace: str
    data: dict[str, JSONValue] = Field(default_factory=dict)


type SessionEntry = Annotated[
    MessageEntry
    | CustomMessageEntry
    | ModelChangeEntry
    | ThinkingLevelChangeEntry
    | CompactionEntry
    | BranchSummaryEntry
    | LabelEntry
    | LeafEntry
    | SessionInfoEntry
    | CustomEntry,
    Field(discriminator="type"),
]
