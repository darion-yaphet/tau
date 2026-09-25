"""User-home session management for Tau coding sessions.

管理用户主目录中的 Tau 编码会话。
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from tau_coding.paths import TauPaths

_MAX_SESSION_ID_BYTES = 128
_RESERVED_SESSION_IDS = frozenset({"default", "index"})
_WINDOWS_RESERVED_FILE_STEMS = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")

InferenceProviderMode = Literal["automatic", "fixed"]


def normalize_session_name(value: str) -> str:
    """Return a trimmed, single-line session name or raise ValueError.

    返回去除首尾空白的单行会话名称，否则抛出 ValueError。
    """
    name = value.strip()
    if not name:
        raise ValueError("Session name cannot be empty")
    if any(char in name for char in "\r\n\t"):
        raise ValueError("Session name must be a single line.")
    return name


def validate_session_id(session_id: str) -> None:
    """Reject custom session ids that are unsafe as file names.

    拒绝不能安全用作文件名的自定义会话标识符。
    """
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "Session id must be non-empty, contain only alphanumeric characters, '-', '_', "
            "and '.', and start and end with an alphanumeric character"
        )
    if len(session_id.encode("utf-8")) > _MAX_SESSION_ID_BYTES:
        raise ValueError(f"Session id must be at most {_MAX_SESSION_ID_BYTES} bytes")
    normalized_id = session_id.casefold()
    if normalized_id in _RESERVED_SESSION_IDS:
        raise ValueError(f"Session id is reserved: {session_id}")
    if normalized_id.partition(".")[0] in _WINDOWS_RESERVED_FILE_STEMS:
        raise ValueError(f"Session id is not a portable file name: {session_id}")


class SessionRecordModel(BaseModel):
    """JSON-serializable coding-session metadata.

    可序列化为 JSON 的编码会话元数据。
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    path: str
    cwd: str
    model: str
    provider_name: str | None = None
    inference_provider: str | None = None
    inference_provider_mode: InferenceProviderMode | None = None
    title: str | None = None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class CodingSessionRecord:
    """Metadata for one durable coding session.

    一个持久化编码会话的元数据。
    """

    id: str
    path: Path
    cwd: Path
    model: str
    title: str | None
    created_at: float
    updated_at: float
    provider_name: str | None = None
    inference_provider: str | None = None
    inference_provider_mode: InferenceProviderMode = "automatic"

    @classmethod
    def from_model(cls, model: SessionRecordModel) -> CodingSessionRecord:
        """Convert a JSON model to a record.

        将 JSON 模型转换为记录。
        """
        return cls(
            id=model.id,
            path=Path(model.path),
            cwd=Path(model.cwd),
            model=model.model,
            title=model.title,
            created_at=model.created_at,
            updated_at=model.updated_at,
            provider_name=model.provider_name,
            inference_provider=model.inference_provider,
            inference_provider_mode=(
                model.inference_provider_mode
                or ("fixed" if model.inference_provider is not None else "automatic")
            ),
        )

    def to_model(self) -> SessionRecordModel:
        """Convert this record to a JSON model.

        将当前记录转换为 JSON 模型。
        """
        return SessionRecordModel(
            id=self.id,
            path=str(self.path),
            cwd=str(self.cwd),
            model=self.model,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            provider_name=self.provider_name,
            inference_provider=self.inference_provider,
            inference_provider_mode=self.inference_provider_mode,
        )


class SessionManager:
    """Create, index, list, and resume user-home coding sessions.

    创建、索引、列出和恢复用户主目录中的编码会话。
    """

    def __init__(self, paths: TauPaths | None = None) -> None:
        """Initialize session management with the configured Tau paths.

        使用配置的 Tau 路径初始化会话管理器。
        """
        self.paths = paths or TauPaths()

    @property
    def index_path(self) -> Path:
        """Return the legacy global session metadata index path.

        返回旧版全局会话元数据索引路径。
        """
        return self.paths.sessions_dir / "index.jsonl"

    def project_index_path(self, cwd: Path) -> Path:
        """Return the session metadata index path for a project cwd.

        返回项目工作目录对应的会话元数据索引路径。
        """
        return self.paths.project_session_dir(cwd) / "index.jsonl"

    def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        """Return indexed sessions, newest updated first.

        返回已索引的会话，最近更新的排在最前。

        When `cwd` is provided, only sessions for that resolved working directory
        are returned. Without `cwd`, records are aggregated across project
        indexes and the legacy global index.

        提供 `cwd` 时，只返回该解析后工作目录的会话。未提供 `cwd` 时，
        聚合所有项目索引以及旧版全局索引中的记录。
        """
        records = self._read_project_records(cwd) if cwd is not None else self._read_all_records()
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def get_session(self, session_id: str) -> CodingSessionRecord | None:
        """Return a session record by id, if present.

        按标识符返回会话记录；不存在时返回 None。
        """
        for record in self._read_all_records():
            if record.id == session_id:
                return record
        return None

    def latest_session_for_cwd(self, cwd: Path) -> CodingSessionRecord | None:
        """Return the most recently updated session for a working directory.

        返回指定工作目录中最近更新的会话。
        """
        records = self.list_sessions(cwd)
        return records[0] if records else None

    def create_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:
        """Create and index a new session record.

        创建并索引新的会话记录。
        """
        record = self.prepare_session(
            cwd=cwd,
            model=model,
            provider_name=provider_name,
            inference_provider=inference_provider,
            inference_provider_mode=inference_provider_mode,
            title=title,
            session_id=session_id,
        )
        self.index_session(record)
        return record

    def create_session_exclusive(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:
        """Atomically reserve and index a session transcript without overwriting.

        以原子方式预留并索引会话记录文件，且不覆盖已有文件。
        """
        record = self.prepare_session(
            cwd=cwd,
            model=model,
            provider_name=provider_name,
            inference_provider=inference_provider,
            inference_provider_mode=inference_provider_mode,
            title=title,
            session_id=session_id,
        )
        if self.get_session(record.id) is not None:
            raise RuntimeError(f"Session already exists with id '{record.id}'")
        try:
            with record.path.open("x", encoding="utf-8"):
                pass
        except FileExistsError as exc:
            raise RuntimeError(f"Session already exists with id '{record.id}'") from exc
        except Exception:
            with suppress(OSError):
                record.path.unlink(missing_ok=True)
            raise
        try:
            return self.index_session(record)
        except Exception:
            with suppress(Exception):
                self._remove(record)
            with suppress(OSError):
                record.path.unlink(missing_ok=True)
            raise

    def prepare_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:
        """Return metadata for a session without adding it to the resume index.

        返回会话元数据，但不将其添加到恢复索引。
        """
        now = time()
        resolved_cwd = cwd.resolve()
        record_id = uuid4().hex if session_id is None else session_id
        validate_session_id(record_id)
        project_session_dir = self.paths.project_session_dir(resolved_cwd)
        default_session_id = f"default-{project_session_dir.name}"
        if record_id.casefold() == default_session_id.casefold():
            raise ValueError(f"Session id is reserved: {record_id}")
        path = project_session_dir / f"{record_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return CodingSessionRecord(
            id=record_id,
            path=path,
            cwd=resolved_cwd,
            model=model,
            provider_name=provider_name,
            inference_provider=inference_provider,
            inference_provider_mode=(
                inference_provider_mode
                or ("fixed" if inference_provider is not None else "automatic")
            ),
            title=title,
            created_at=now,
            updated_at=now,
        )

    def index_session(self, record: CodingSessionRecord) -> CodingSessionRecord:
        """Add a prepared session record to the resume index.

        将准备好的会话记录添加到恢复索引。
        """
        self._upsert(record)
        return record

    def get_or_create_default_session(
        self, *, cwd: Path, model: str, provider_name: str | None = None
    ) -> CodingSessionRecord:
        """Return the default project session, creating an index record when needed.

        返回项目默认会话，并在需要时创建索引记录。
        """
        resolved_cwd = cwd.resolve()
        project_hash = self.paths.project_session_dir(resolved_cwd).name
        session_id = f"default-{project_hash}"
        existing = self.get_session(session_id)
        if existing is not None:
            return existing

        now = time()
        path = self.paths.default_session_path(resolved_cwd)
        record = CodingSessionRecord(
            id=session_id,
            path=path,
            cwd=resolved_cwd,
            model=model,
            provider_name=provider_name,
            title="Default session",
            created_at=now,
            updated_at=now,
        )
        self._upsert(record)
        return record

    def touch_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        provider_name: str | None = None,
        inference_provider: str | None = None,
        inference_provider_mode: InferenceProviderMode | None = None,
        preserve_inference_provider: bool = True,
        title: str | None = None,
    ) -> CodingSessionRecord | None:
        """Update a session's last-used metadata.

        更新会话最近使用的元数据。
        """
        existing = self.get_session(session_id)
        if existing is None:
            return None
        updated = CodingSessionRecord(
            id=existing.id,
            path=existing.path,
            cwd=existing.cwd,
            model=model or existing.model,
            provider_name=provider_name if provider_name is not None else existing.provider_name,
            inference_provider=(
                existing.inference_provider if preserve_inference_provider else inference_provider
            ),
            inference_provider_mode=(
                existing.inference_provider_mode
                if preserve_inference_provider or inference_provider_mode is None
                else inference_provider_mode
            ),
            title=title if title is not None else existing.title,
            created_at=existing.created_at,
            updated_at=time(),
        )
        self._upsert(updated)
        return updated

    def _read_index(self, path: Path) -> list[CodingSessionRecord]:
        """Read session records from one JSONL index.

        从一个 JSONL 索引读取会话记录。
        """
        if not path.exists():
            return []

        records: list[CodingSessionRecord] = []
        # Split on newlines only: str.splitlines() would also split on characters
        # like U+2028 that appear unescaped inside JSON string values.
        #
        # 仅按换行符拆分：str.splitlines() 还会按 U+2028 等字符拆分，而这些
        # 字符可能以未转义形式出现在 JSON 字符串值中。
        for line in path.read_text(encoding="utf-8").split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            model = SessionRecordModel.model_validate_json(stripped)
            records.append(CodingSessionRecord.from_model(model))
        return records

    def _read_project_records(self, cwd: Path) -> list[CodingSessionRecord]:
        """Read and de-duplicate records belonging to one project directory.

        读取并去重属于一个项目目录的会话记录。
        """
        resolved_cwd = cwd.resolve()
        records = self._read_index(self.project_index_path(resolved_cwd))
        records.extend(
            record for record in self._read_index(self.index_path) if record.cwd == resolved_cwd
        )
        return _deduplicate_records(records)

    def _read_all_records(self) -> list[CodingSessionRecord]:
        """Read and de-duplicate records from all known session indexes.

        从所有已知会话索引读取并去重记录。
        """
        records = self._read_index(self.index_path)
        for index_path in self.paths.sessions_dir.glob("*/index.jsonl"):
            records.extend(self._read_index(index_path))
        return _deduplicate_records(records)

    def _write_index(self, path: Path, records: list[CodingSessionRecord]) -> None:
        """Rewrite one JSONL index from the supplied records.

        使用给定记录重写一个 JSONL 索引。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(record.to_model().model_dump_json() for record in records)
        if content:
            content += "\n"
        path.write_text(content, encoding="utf-8")

    def _upsert(self, record: CodingSessionRecord) -> None:
        """Insert or replace one record in its project index.

        在所属项目索引中插入或替换一条记录。
        """
        path = self.project_index_path(record.cwd)
        records = [item for item in self._read_index(path) if item.id != record.id]
        records.append(record)
        self._write_index(path, records)

    def _remove(self, record: CodingSessionRecord) -> None:
        """Remove one record from its project index.

        从所属项目索引中删除一条记录。
        """
        path = self.project_index_path(record.cwd)
        records = [item for item in self._read_index(path) if item.id != record.id]
        self._write_index(path, records)


def _deduplicate_records(records: list[CodingSessionRecord]) -> list[CodingSessionRecord]:
    """Keep the newest record for each session id.

    为每个会话标识符保留最新记录。
    """
    by_id: dict[str, CodingSessionRecord] = {}
    for record in records:
        existing = by_id.get(record.id)
        if existing is None or record.updated_at >= existing.updated_at:
            by_id[record.id] = record
    return list(by_id.values())
