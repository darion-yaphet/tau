"""Safe, user-level state for the trusted llama.cpp integration.

可信 llama.cpp 集成的安全用户级状态。

Only endpoint-keyed discovery metadata lives here.  Credentials are kept in
Tau's credential store and are referenced by an opaque generation name.

这里只保存以端点为键的发现元数据。凭据保存在 Tau 的凭据存储中，并通过不透明的代际
名称引用。
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from tau_coding.paths import TauPaths

LLAMA_CPP_STATE_SCHEMA_VERSION = 1
LLAMA_CPP_CREDENTIAL_PREFIX = "llama.cpp:"


class LlamaCppStateError(RuntimeError):
    """Raised for an unreadable or unsupported llama.cpp state file.

    llama.cpp 状态文件不可读或不受支持时抛出的异常。
    """


@dataclass(frozen=True, slots=True)
class LlamaCppStoredModel:
    """Allowlisted model metadata safe to retain outside the server.

    可安全保留在服务器外的允许列表模型元数据。
    """

    id: str
    display_name: str | None = None
    context_window: int | None = None
    input_modalities: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        """Validate stored model identity and allowlisted metadata.

        校验已存储模型标识和允许列表元数据。
        """
        if not isinstance(self.id, str) or not self.id or self.id != self.id.strip():
            raise LlamaCppStateError("Stored llama.cpp model id must be a non-empty exact string")
        if self.display_name is not None and (
            not isinstance(self.display_name, str) or not self.display_name.strip()
        ):
            raise LlamaCppStateError("Stored llama.cpp display name must be non-empty")
        if self.context_window is not None and (
            not isinstance(self.context_window, int)
            or isinstance(self.context_window, bool)
            or self.context_window <= 0
        ):
            raise LlamaCppStateError("Stored context window must be a positive integer")
        if self.input_modalities is not None:
            if not isinstance(self.input_modalities, tuple) or not self.input_modalities:
                raise LlamaCppStateError("Stored input modalities must be a non-empty tuple")
            if any(item not in {"text", "image"} for item in self.input_modalities):
                raise LlamaCppStateError("Stored input modalities are unsupported")
            if len(set(self.input_modalities)) != len(self.input_modalities):
                raise LlamaCppStateError("Stored input modalities must be unique")

    def to_json(self) -> dict[str, object]:
        """Serialize the allowlisted model metadata to JSON-compatible data.

        将允许列表模型元数据序列化为 JSON 兼容数据。
        """
        value: dict[str, object] = {"id": self.id}
        if self.display_name is not None:
            value["display_name"] = self.display_name
        if self.context_window is not None:
            value["context_window"] = self.context_window
        if self.input_modalities is not None:
            value["input_modalities"] = list(self.input_modalities)
        return value


@dataclass(frozen=True, slots=True)
class LlamaCppIntegrationState:
    """One endpoint's safe integration snapshot.

    一个端点的安全集成快照。
    """

    endpoint: str
    selected_model: str | None = None
    credential_ref: str | None = None
    models: tuple[LlamaCppStoredModel, ...] = ()
    checked_at: str | None = None

    def __post_init__(self) -> None:
        """Validate endpoint, selection, credential reference, and models.

        校验端点、选择、凭据引用和模型。
        """
        if not isinstance(self.endpoint, str) or not self.endpoint.strip():
            raise LlamaCppStateError("Llama.cpp state endpoint must be non-empty")
        if self.selected_model is not None and (
            not isinstance(self.selected_model, str)
            or not self.selected_model.strip()
            or self.selected_model != self.selected_model.strip()
        ):
            raise LlamaCppStateError(
                "Selected llama.cpp model must be a non-empty exact string or None"
            )
        if self.credential_ref is not None and not _valid_credential_ref(self.credential_ref):
            raise LlamaCppStateError("Credential reference is not owned by llama.cpp")
        if not isinstance(self.models, tuple) or any(
            not isinstance(model, LlamaCppStoredModel) for model in self.models
        ):
            raise LlamaCppStateError("Llama.cpp state models are malformed")
        ids = [model.id for model in self.models]
        if len(ids) != len(set(ids)):
            raise LlamaCppStateError("Llama.cpp state model ids must be unique")
        # Keep a selected reference even when a later discovery no longer
        # reports that model. The reference is not an availability claim: it
        # lets a cached/offline resume recover the exact model if the server
        # reports it again, while the provider layer keeps it unavailable until
        # then.
        # 即使后续发现不再报告某模型，也保留所选引用。该引用并非可用性声明：它允许缓存
        # 或离线恢复在服务器再次报告该模型时恢复精确模型，同时提供商层在此之前保持其
        # 不可用状态。
        if self.checked_at is not None and not isinstance(self.checked_at, str):
            raise LlamaCppStateError("Checked timestamp must be a string or None")

    def to_json(self) -> dict[str, object]:
        """Serialize the endpoint integration snapshot to JSON-compatible data.

        将端点集成快照序列化为 JSON 兼容数据。
        """
        return {
            "endpoint": self.endpoint,
            "selected_model": self.selected_model,
            "credential_ref": self.credential_ref,
            "models": [model.to_json() for model in self.models],
            "checked_at": self.checked_at,
        }


class LlamaCppStateStore:
    """Locked and atomically replaced endpoint-keyed integration state.

    加锁并原子替换的、以端点为键的集成状态。
    """

    def __init__(
        # Initialize paths for a locked, endpoint-keyed state store.
        #
        # 初始化加锁且以端点为键的状态存储路径。
        self,
        path: Path | None = None,
        *,
        lock_path: Path | None = None,
        paths: TauPaths | None = None,
    ) -> None:
        resolved_paths = paths or TauPaths()
        self.path = path or resolved_paths.llama_cpp_state_path
        self.lock_path = lock_path or self.path.with_name(f"{self.path.name}.lock")

    def get(self, endpoint: str) -> LlamaCppIntegrationState | None:
        """Return the saved snapshot for one endpoint.

        返回一个端点的已保存快照。
        """
        with self._locked():
            _, endpoints = self._read_unlocked()
            return endpoints.get(endpoint)

    def active(self) -> LlamaCppIntegrationState | None:
        """Return the currently selected endpoint snapshot.

        返回当前选定端点的快照。
        """
        with self._locked():
            active_endpoint, endpoints = self._read_unlocked()
            return endpoints.get(active_endpoint) if active_endpoint else None

    def all(self) -> tuple[LlamaCppIntegrationState, ...]:
        """Return every saved endpoint snapshot.

        返回所有已保存的端点快照。
        """
        with self._locked():
            _, endpoints = self._read_unlocked()
            return tuple(endpoints.values())

    def save(
        self,
        state: LlamaCppIntegrationState,
        *,
        replace_endpoint: str | None = None,
    ) -> None:
        """Publish one endpoint snapshot and make it the saved endpoint.

        发布一个端点快照，并将其设为已保存端点。

        ``replace_endpoint`` lets configuration replace the prior active
        endpoint in the same atomic state-file transaction. Discovery updates
        omit it so a caller can retain endpoint-keyed snapshots when desired.

        ``replace_endpoint`` 允许配置过程在同一次原子状态文件事务中替换之前的活动端点。
        发现更新时会省略该参数，以便调用方根据需要保留按端点索引的快照。
        """
        with self._locked():
            active_endpoint, endpoints = self._read_unlocked()
            del active_endpoint
            if replace_endpoint is not None and replace_endpoint != state.endpoint:
                endpoints.pop(replace_endpoint, None)
            endpoints[state.endpoint] = state
            self._write_unlocked(state.endpoint, endpoints)

    def remove(self, endpoint: str) -> tuple[str, ...]:
        """Remove one endpoint and return credential refs no longer referenced.

        移除一个端点，并返回不再被引用的凭据引用。
        """
        with self._locked():
            active_endpoint, endpoints = self._read_unlocked()
            before = _credential_refs(endpoints.values())
            endpoints.pop(endpoint, None)
            next_active = active_endpoint if active_endpoint != endpoint else None
            self._write_unlocked(next_active, endpoints)
            return tuple(sorted(before - _credential_refs(endpoints.values())))

    def clear(self) -> tuple[str, ...]:
        """Remove all integration settings, returning referenced credentials.

        移除所有集成设置，并返回原先被引用的凭据。
        """
        with self._locked():
            _, endpoints = self._read_unlocked()
            refs = tuple(sorted(_credential_refs(endpoints.values())))
            if self.path.exists():
                self.path.unlink()
                _fsync_directory(self.path.parent)
            return refs

    def referenced_credentials(self) -> frozenset[str]:
        """Return all credential references retained by saved snapshots.

        返回已保存快照保留的所有凭据引用。
        """
        with self._locked():
            _, endpoints = self._read_unlocked()
            return frozenset(_credential_refs(endpoints.values()))

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the cross-process state lock for one operation.

        在一次操作期间持有跨进程状态锁。
        """
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.lock_path.parent != self.path.parent:
            self.lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdir(mode=...) does not tighten an already-existing directory. The
        # state directory is user-private even when it was created earlier by
        # another Tau process.
        with suppress(OSError):
            self.path.parent.chmod(0o700)
        if self.lock_path.parent != self.path.parent:
            with suppress(OSError):
                self.lock_path.parent.chmod(0o700)
        try:
            with self.lock_path.open("a+b") as handle:
                self.lock_path.chmod(0o600)
                _lock(handle)
                try:
                    _remove_temporary_files(self.path)
                    yield
                finally:
                    _unlock(handle)
        except LlamaCppStateError:
            raise
        except OSError as exc:
            raise LlamaCppStateError(f"Could not access llama.cpp state: {exc}") from exc

    def _read_unlocked(
        # Read and validate endpoint state while the caller holds the lock.
        #
        # 在调用方持锁期间读取并校验端点状态。
        self,
    ) -> tuple[str | None, dict[str, LlamaCppIntegrationState]]:
        if not self.path.exists():
            return None, {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LlamaCppStateError(f"Could not read llama.cpp state: {exc}") from exc
        if not isinstance(payload, dict):
            raise LlamaCppStateError("Llama.cpp state must be a JSON object")

        # Accept the early single-endpoint shape from the accepted plan.  It is
        # read-only compatible and is upgraded only on the next intentional save.
        if "endpoints" not in payload and "endpoint" in payload:
            allowed = {
                "schema_version",
                "endpoint",
                "selected_model",
                "credential_ref",
                "models",
                "checked_at",
            }
            if set(payload) - allowed or payload.get("schema_version") != 1:
                raise LlamaCppStateError("Unsupported llama.cpp state schema")
            state = _state_from_json(
                {key: value for key, value in payload.items() if key != "schema_version"}
            )
            return state.endpoint, {state.endpoint: state}

        if set(payload) - {"schema_version", "active_endpoint", "endpoints"}:
            raise LlamaCppStateError("Unknown field in llama.cpp state")
        if payload.get("schema_version") != LLAMA_CPP_STATE_SCHEMA_VERSION:
            raise LlamaCppStateError("Unsupported llama.cpp state schema version")
        active_endpoint = payload.get("active_endpoint")
        endpoints_raw = payload.get("endpoints")
        if active_endpoint is not None and not isinstance(active_endpoint, str):
            raise LlamaCppStateError("Active llama.cpp endpoint is malformed")
        if not isinstance(endpoints_raw, dict):
            raise LlamaCppStateError("Llama.cpp endpoints must be an object")
        endpoints: dict[str, LlamaCppIntegrationState] = {}
        for key, raw in endpoints_raw.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(raw, dict):
                raise LlamaCppStateError("Llama.cpp endpoint state is malformed")
            state = _state_from_json(raw)
            if key != state.endpoint:
                raise LlamaCppStateError("Llama.cpp endpoint key does not match its state")
            endpoints[key] = state
        if active_endpoint is not None and active_endpoint not in endpoints:
            raise LlamaCppStateError("Active llama.cpp endpoint is not stored")
        return active_endpoint, endpoints

    def _write_unlocked(
        # Atomically persist endpoint state while the caller holds the lock.
        #
        # 在调用方持锁期间原子持久化端点状态。
        self,
        active_endpoint: str | None,
        endpoints: Mapping[str, LlamaCppIntegrationState],
    ) -> None:
        payload = {
            "schema_version": LLAMA_CPP_STATE_SCHEMA_VERSION,
            "active_endpoint": active_endpoint,
            "endpoints": {
                endpoint: state.to_json() for endpoint, state in sorted(endpoints.items())
            },
        }
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        temporary: Path | None = None
        fd = -1
        try:
            fd, raw = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temporary = Path(raw)
            temporary.chmod(0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self.path.chmod(0o600)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            raise LlamaCppStateError(f"Could not write llama.cpp state: {exc}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink()


def _state_from_json(raw: Mapping[str, object]) -> LlamaCppIntegrationState:
    """Parse one endpoint integration snapshot from JSON data.

    从 JSON 数据解析一个端点集成快照。
    """
    allowed = {"endpoint", "selected_model", "credential_ref", "models", "checked_at"}
    if set(raw) - allowed:
        raise LlamaCppStateError("Unknown field in llama.cpp endpoint state")
    endpoint = raw.get("endpoint")
    models_raw = raw.get("models", [])
    if not isinstance(endpoint, str) or not isinstance(models_raw, list):
        raise LlamaCppStateError("Malformed llama.cpp endpoint state")
    if not isinstance(raw.get("selected_model"), (str, type(None))):
        raise LlamaCppStateError("Malformed selected llama.cpp model")
    if not isinstance(raw.get("credential_ref"), (str, type(None))):
        raise LlamaCppStateError("Malformed llama.cpp credential reference")
    models = tuple(_model_from_json(item) for item in models_raw)
    return LlamaCppIntegrationState(
        endpoint=endpoint,
        selected_model=cast(str | None, raw.get("selected_model")),
        credential_ref=cast(str | None, raw.get("credential_ref")),
        models=models,
        checked_at=cast(str | None, raw.get("checked_at")),
    )


def _model_from_json(raw: object) -> LlamaCppStoredModel:
    """Parse one allowlisted stored model from JSON data.

    从 JSON 数据解析一个允许列表模型。
    """
    if not isinstance(raw, dict):
        raise LlamaCppStateError("Malformed stored llama.cpp model")
    allowed = {"id", "display_name", "context_window", "input_modalities"}
    if set(raw) - allowed:
        raise LlamaCppStateError("Unknown field in stored llama.cpp model")
    modalities = raw.get("input_modalities")
    if modalities is not None and not isinstance(modalities, list):
        raise LlamaCppStateError("Malformed stored input modalities")
    return LlamaCppStoredModel(
        id=cast(str, raw.get("id")),
        display_name=cast(str | None, raw.get("display_name")),
        context_window=cast(int | None, raw.get("context_window")),
        input_modalities=tuple(modalities) if modalities is not None else None,
    )


def _credential_refs(states: Iterable[LlamaCppIntegrationState]) -> set[str]:
    """Collect credential references used by endpoint snapshots.

    收集端点快照使用的凭据引用。
    """
    return {state.credential_ref for state in states if state.credential_ref}


def _valid_credential_ref(value: object) -> bool:
    """Return whether a credential reference belongs to this integration.

    返回凭据引用是否属于此集成。
    """
    if not isinstance(value, str) or not value.startswith(LLAMA_CPP_CREDENTIAL_PREFIX):
        return False
    suffix = value.removeprefix(LLAMA_CPP_CREDENTIAL_PREFIX)
    # secrets.token_urlsafe() emits this conservative alphabet. Reject path
    # separators and control characters so state can never name an unrelated
    # credential or become a path-like injection surface.
    return bool(suffix) and all(character.isalnum() or character in "-_" for character in suffix)


def _remove_temporary_files(path: Path) -> None:
    """Remove only this store's interrupted atomic-write artifacts.

    仅清理由此存储的原子写入中断留下的文件。
    """
    pattern = f".{path.name}.*.tmp"
    for temporary in path.parent.glob(pattern):
        with suppress(OSError):
            temporary.unlink()


def _lock(handle: object) -> None:
    """Acquire an exclusive platform-specific file lock.

    获取平台专用的排他文件锁。
    """
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
    except ImportError:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
    except OSError as exc:
        raise LlamaCppStateError(f"Could not lock llama.cpp state: {exc}") from exc


def _unlock(handle: object) -> None:
    """Release a platform-specific file lock.

    释放平台专用文件锁。
    """
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except ImportError:
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]


def _fsync_directory(path: Path) -> None:
    """Synchronize the containing directory when supported.

    在平台支持时同步包含目录。
    """
    with suppress(OSError):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "LLAMA_CPP_CREDENTIAL_PREFIX",
    "LLAMA_CPP_STATE_SCHEMA_VERSION",
    "LlamaCppIntegrationState",
    "LlamaCppStateError",
    "LlamaCppStateStore",
    "LlamaCppStoredModel",
]
