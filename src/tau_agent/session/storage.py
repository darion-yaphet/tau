"""Locked, append-only session storage implementations.

带锁的、仅追加的会话存储实现。
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Protocol

from tau_agent.session.entries import SessionEntry
from tau_agent.session.jsonl import entries_from_json_lines, entry_to_json_line


class SessionStorage(Protocol):
    """Append-only session storage interface.

    仅追加的会话存储接口。

    ``append_batch`` is the durable transaction boundary used by startup,
    replacement, and model selection. Implementations must make the complete
    batch visible or leave the previous transcript untouched.

    ``append_batch`` 是启动、替换和模型选择所使用的持久化事务边界。
    实现必须使整个批次可见，否则保持之前的会话记录不变。
    """

    async def append(self, entry: SessionEntry) -> None:
        """Append one entry to storage.

        向存储中追加一条记录。
        """
        ...

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Atomically append a complete batch of entries.

        以原子方式追加一个完整的记录批次。
        """
        ...

    async def read_all(self) -> list[SessionEntry]:
        """Read all entries in storage order.

        按存储顺序读取所有记录。
        """
        ...


class JsonlSessionStorage:
    """Local JSONL storage with a per-session cross-process lock.

    使用每会话跨进程锁的本地 JSONL 存储。

    The lock is deliberately separate from the transcript. Readers use a
    shared lock when the platform provides one; every write uses an exclusive
    lock and re-reads the current file before changing it. Batch writes use a
    same-directory temporary file, fsync, replace, and directory fsync.

    该锁特意与会话记录分开。平台支持时，读取方使用共享锁；
    每次写入都使用独占锁，并在更改前重新读取当前文件。批量写入使用
    同目录临时文件、fsync、替换和目录 fsync。
    """

    def __init__(self, path: str | Path) -> None:
        """Initialize storage paths for a session transcript and its lock.

        初始化会话记录及其锁文件的存储路径。
        """
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.temp_path = self.path.with_name(f".{self.path.name}.tmp")

    async def append(self, entry: SessionEntry) -> None:
        """Append one entry under the session's exclusive cross-process lock.

        在会话的跨进程独占锁下追加一条记录。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            with self.path.open("ab") as file:
                file.write(entry_to_json_line(entry).encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Atomically append all entries, preserving the old file on failure.

        以原子方式追加所有记录，并在失败时保留旧文件。
        """
        batch = tuple(entries)
        if not batch:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = b"".join(entry_to_json_line(entry).encode("utf-8") for entry in batch)
        with self._locked(exclusive=True):
            self._remove_incomplete_temp()
            previous = self.path.read_bytes() if self.path.exists() else b""
            data = previous + encoded
            self._atomic_replace(data)

    async def read_all(self) -> list[SessionEntry]:
        """Read all entries in file order; missing files are empty sessions.

        按文件顺序读取所有记录；文件缺失时视为空会话。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A fixed temp name makes crash recovery deterministic. If it exists,
        # take an exclusive lock and discard it; it was never the commit file.
        #
        # 固定的临时文件名使崩溃恢复具有确定性。如果它存在，
        # 则获取独占锁并将其丢弃；它从来都不是提交文件。
        if self.temp_path.exists():
            with self._locked(exclusive=True):
                self._remove_incomplete_temp()
                return self._read_unlocked()
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def _read_unlocked(self) -> list[SessionEntry]:
        """Read and decode the transcript while the caller holds the lock.

        在调用方已持有锁时读取并解码会话记录。
        """
        if not self.path.exists():
            return []
        return entries_from_json_lines(self.path.read_text(encoding="utf-8").split("\n"))

    def _atomic_replace(self, data: bytes) -> None:
        """Replace the transcript atomically with fully synchronized data.

        使用完整同步的数据原子替换会话记录。
        """
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        # Keep the stable recovery marker in addition to the unique temp. A
        # crash between writes leaves a safely removable artifact.
        #
        # 除唯一临时文件外，还保留稳定的恢复标记。写入之间发生崩溃时，
        # 会留下可安全删除的产物。
        try:
            os.close(descriptor)
            temporary_path = Path(temporary)
            with temporary_path.open("wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, self.path)
            _fsync_directory(self.path.parent)
        except BaseException:
            with _suppress_os_error():
                Path(temporary).unlink()
            raise

    def _remove_incomplete_temp(self) -> None:
        """Remove incomplete temporary files left by interrupted writes.

        删除写入中断后留下的不完整临时文件。
        """
        with _suppress_os_error():
            self.temp_path.unlink()
        # Also clean unique temp files left by a process killed during a
        # replacement. They are never authoritative because replace is atomic.
        #
        # 同时清理进程在替换期间被终止后留下的唯一临时文件。
        # 由于替换是原子操作，这些文件永远不是权威数据源。
        prefix = f".{self.path.name}."
        for candidate in self.path.parent.glob(f"{prefix}*.tmp"):
            with _suppress_os_error():
                candidate.unlink()

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        """Hold the session lock for the duration of the context.

        在上下文持续期间持有会话锁。
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            os.chmod(self.lock_path, 0o600)
            _lock_file(lock_file, exclusive=exclusive)
            try:
                yield
            finally:
                _unlock_file(lock_file)


class InMemorySessionStorage:
    """Deterministic storage useful for tests and embedded frontends.

    适用于测试和嵌入式前端的确定性存储。
    """

    def __init__(self, entries: Sequence[SessionEntry] = ()) -> None:
        """Initialize storage with an optional entry sequence.

        使用可选的记录序列初始化存储。
        """
        self.entries = list(entries)
        self._lock = asyncio.Lock()

    async def append(self, entry: SessionEntry) -> None:
        """Append one entry while holding the in-memory lock.

        在持有内存锁时追加一条记录。
        """
        async with self._lock:
            self.entries.append(entry)

    async def append_batch(self, entries: Sequence[SessionEntry]) -> None:
        """Append a batch of entries while holding the in-memory lock.

        在持有内存锁时追加一批记录。
        """
        async with self._lock:
            self.entries.extend(entries)

    async def read_all(self) -> list[SessionEntry]:
        """Return a snapshot of all in-memory entries.

        返回所有内存记录的快照。
        """
        async with self._lock:
            return list(self.entries)


@contextmanager
def _suppress_os_error() -> Iterator[None]:
    """Suppress operating-system errors within a cleanup context.

    在清理上下文中忽略操作系统错误。
    """
    with suppress(OSError):
        yield


def _lock_file(file: BinaryIO, *, exclusive: bool) -> None:
    """Apply an advisory lock, with a clear unsupported-platform fallback.

    应用建议锁，并为不支持的平台提供明确的回退方案。
    """
    if os.name == "nt":
        import msvcrt

        # msvcrt has no shared lock; an exclusive lock is the safe behavior for
        # reads on Windows and still provides cross-process serialization.
        #
        # msvcrt 没有共享锁；在 Windows 上读取时使用独占锁是安全行为，
        # 并且仍能提供跨进程串行化。
        del exclusive
        msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(file.fileno(), mode)


def _unlock_file(file: BinaryIO) -> None:
    """Release a previously acquired advisory file lock.

    释放之前获取的建议文件锁。
    """
    if os.name == "nt":
        import msvcrt

        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    """Persist the directory entry where the OS supports directory fsync.

    在操作系统支持目录 fsync 时持久化目录项。
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InMemorySessionStorage", "JsonlSessionStorage", "SessionStorage"]
