"""Project-input trust policy, detection, persistence, and coordination.

项目输入信任策略、检测、持久化与协调。

Project trust controls ambient project resources. It is deliberately not a
filesystem, process, network, tool, model, or prompt-injection sandbox.

项目信任控制环境中的项目资源。它并非文件系统、进程、网络、工具、模型或提示注入
沙箱。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Literal

from tau_coding.paths import TauPaths
from tau_coding.prompt_templates import is_prompt_template_candidate
from tau_coding.skills import is_skill_candidate

TrustDefault = Literal["ask", "always", "never"]
TrustDecision = Literal["trusted", "untrusted"]
TrustOverride = Literal["approve", "decline"]
TrustScope = Literal["exact", "parent", "run"]
TrustSource = Literal["override", "empty", "extension", "saved", "default", "ui"]
TrustChoice = Literal["trust-exact", "trust-parent", "trust-run", "decline-exact", "decline-run"]

_RESOURCE_CATEGORIES = (
    "context",
    "extensions",
    "prompts",
    "settings",
    "skills",
    "system-prompts",
    "themes",
)


class ProjectTrustError(RuntimeError):
    """A trust path, store, or persistence operation failed safely.

    信任路径、存储或持久化操作安全失败时抛出的异常。
    """


@dataclass(frozen=True, slots=True)
class CanonicalProjectPath:
    """An existing, canonical project working directory.

    已存在的规范项目工作目录。
    """

    value: Path


@dataclass(frozen=True, slots=True)
class ProtectedResourceSummary:
    """Bounded metadata-only summary of protected project inputs.

    受保护项目输入的有界纯元数据摘要。
    """

    cwd: CanonicalProjectPath
    categories: tuple[str, ...]
    counts: Mapping[str, int]
    sample_paths: tuple[Path, ...] = ()

    @property
    def total(self) -> int:
        """Return the total number of detected protected resources.

        返回检测到的受保护资源总数。
        """
        return sum(self.counts.values())


@dataclass(frozen=True, slots=True)
class SavedTrustEntry:
    """A validated saved exact or inherited decision.

    已校验并保存的精确或继承决策。
    """

    path: CanonicalProjectPath
    decision: TrustDecision


@dataclass(frozen=True, slots=True)
class ProjectTrustRequest:
    """Frontend-neutral request for an interactive trust decision.

    用于交互式信任决策的前端无关请求。
    """

    cwd: CanonicalProjectPath
    resources: ProtectedResourceSummary
    inherited_entry: SavedTrustEntry | None
    choices: tuple[TrustChoice, ...] = (
        "trust-exact",
        "trust-parent",
        "trust-run",
        "decline-exact",
        "decline-run",
    )


@dataclass(frozen=True, slots=True)
class ProjectTrustResolution:
    """Completed decision for one canonical cwd.

    一个规范工作目录的已完成决策。
    """

    trusted: bool
    source: TrustSource
    saved_path: CanonicalProjectPath | None = None
    diagnostics: tuple[str, ...] = ()
    had_candidates: bool = True
    cancelled: bool = False
    # True when staged preparation deferred the durable trust-store write.
    # 暂存准备延迟持久信任存储写入时为 True。
    needs_persistence: bool = False


@dataclass(frozen=True, slots=True)
class ExtensionTrustResult:
    """Result returned by an eligible pre-trust extension.

    符合条件的信任前扩展返回的结果。
    """

    decision: Literal["approve", "decline", "defer"] = "defer"
    remember: bool = False


@dataclass(frozen=True, slots=True)
class ProjectTrustEvent:
    """Content-free payload sent to eligible pre-trust extensions.

    发送给符合条件的信任前扩展的不含内容载荷。
    """

    cwd: Path
    mode: Literal["interactive", "headless"]
    has_ui: bool
    categories: tuple[str, ...]
    counts: Mapping[str, int]
    type: Literal["project_trust"] = "project_trust"


TrustPrompt = Callable[[ProjectTrustRequest], Awaitable[TrustChoice | None]]
ExtensionDecider = Callable[[ProjectTrustEvent], Awaitable[ExtensionTrustResult | None]]


def _darwin_filesystem_path(path: Path) -> Path:
    """Return macOS's case-preserving path for an existing filesystem object.

    返回 macOS 为现有文件系统对象保留大小写的路径。
    """
    import fcntl

    descriptor = os.open(path, os.O_RDONLY)
    try:
        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)  # F_GETPATH / MAXPATHLEN
    finally:
        os.close(descriptor)
    value = raw.split(b"\0", 1)[0]
    if not value:
        raise OSError(f"Could not determine filesystem casing for {path}")
    return Path(os.fsdecode(value))


def canonicalize_project_path(path: Path, *, base: Path | None = None) -> CanonicalProjectPath:
    """Strictly canonicalize an existing destination cwd.

    严格规范化现有目标工作目录。
    """
    expanded = path.expanduser()
    if not expanded.is_absolute():
        if base is None:
            raise ProjectTrustError("A base directory is required for a relative project cwd")
        expanded = base.expanduser() / expanded
    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectTrustError(f"Could not canonicalize project cwd {expanded}: {exc}") from exc
    if not resolved.is_dir():
        raise ProjectTrustError(f"Project cwd is not a directory: {resolved}")
    try:
        if sys.platform == "win32":
            resolved = Path(os.path.normcase(str(resolved)))
        elif sys.platform == "darwin":
            # normcase() is a no-op on Darwin. F_GETPATH asks the mounted
            # filesystem for its case-preserving spelling, so aliases on a
            # case-insensitive volume share a key without collapsing distinct
            # paths on a case-sensitive volume.
            # normcase() 在 Darwin 上不执行操作。F_GETPATH 向挂载的文件系统查询其
            # 保留大小写的拼写，因此不区分大小写卷上的别名共享键，同时不会合并
            # 区分大小写卷上的不同路径。
            resolved = _darwin_filesystem_path(resolved)
    except (OSError, UnicodeError) as exc:
        raise ProjectTrustError(
            f"Could not canonicalize project cwd casing {resolved}: {exc}"
        ) from exc
    return CanonicalProjectPath(resolved)


class ProtectedResourceDetector:
    """Detect protected candidates using names and file metadata only.

    仅使用名称和文件元数据检测受保护候选项。
    """

    def __init__(self, *, max_sample_paths: int = 12) -> None:
        """Configure the maximum number of diagnostic sample paths.

        配置诊断样本路径的最大数量。
        """
        self.max_sample_paths = max_sample_paths

    def detect(self, cwd: CanonicalProjectPath) -> ProtectedResourceSummary:
        """Scan one canonical project for protected resource metadata.

        扫描一个规范项目的受保护资源元数据。
        """
        root = cwd.value
        found: dict[str, list[Path]] = {category: [] for category in _RESOURCE_CATEGORIES}
        # Project settings are not supported by a Tau loader, so they cannot
        # trigger trust until that loader exists.
        # Tau 加载器尚不支持项目设置，因此在该加载器存在前，它们不能触发信任检查。
        for namespace in (".tau", ".agents"):
            self._glob(
                found,
                "skills",
                root / namespace / "skills",
                "*/SKILL.md",
                predicate=is_skill_candidate,
            )
            self._glob(
                found,
                "prompts",
                root / namespace / "prompts",
                "*.md",
                predicate=is_prompt_template_candidate,
            )
        self._glob(found, "themes", root / ".tau" / "themes", "*.json")
        self._file(found, "system-prompts", root / ".tau" / "SYSTEM.md")
        self._file(found, "system-prompts", root / ".tau" / "APPEND_SYSTEM.md")
        self._context(found, root)
        self._extensions(found, root / ".tau" / "extensions")
        counts = {
            category: len(found[category]) for category in _RESOURCE_CATEGORIES if found[category]
        }
        samples = tuple(path for category in _RESOURCE_CATEGORIES for path in found[category])[
            : self.max_sample_paths
        ]
        return ProtectedResourceSummary(
            cwd=cwd,
            categories=tuple(counts),
            counts=counts,
            sample_paths=samples,
        )

    @staticmethod
    def _is_candidate(path: Path) -> bool:
        """Return whether a path exists or is an unreadable protected candidate.

        返回路径是否存在，或是否为不可读取的受保护候选项。
        """
        try:
            return path.is_file() or path.is_symlink()
        except OSError:
            return True

    def _file(self, found: dict[str, list[Path]], category: str, path: Path) -> None:
        """Record one protected file candidate under its category.

        在对应类别下记录一个受保护文件候选项。
        """
        if self._is_candidate(path):
            found[category].append(path)

    def _glob(
        self,
        found: dict[str, list[Path]],
        category: str,
        directory: Path,
        pattern: str,
        *,
        predicate: Callable[[Path], bool] | None = None,
    ) -> None:
        """Record protected paths matching one bounded glob pattern.

        记录匹配一个有界 glob 模式的受保护路径。
        """
        try:
            entries = tuple(directory.glob(pattern)) if directory.is_dir() else ()
        except OSError:
            # An unreadable protected directory is itself a meaningful trigger.
            # 不可读取的受保护目录本身就是有意义的触发条件。
            found[category].append(directory)
            return
        found[category].extend(
            path
            for path in entries
            if self._is_candidate(path) and (predicate is None or predicate(path))
        )

    def _context(self, found: dict[str, list[Path]], cwd: Path) -> None:
        # Match current Tau discovery: nearest project marker through cwd, then
        # cwd-local namespace context files.
        # 匹配当前 Tau 发现逻辑：先查找直到 cwd 的最近项目标记，再查找 cwd 本地命名空间
        # 上下文文件。
        markers = (".git", "pyproject.toml", "uv.lock", "setup.py", "package.json")
        project_root = cwd
        for candidate in (cwd, *cwd.parents):
            if any((candidate / marker).exists() for marker in markers):
                project_root = candidate
                break
        try:
            relative = cwd.relative_to(project_root)
        except ValueError:
            relative = Path()
        current = project_root
        self._file(found, "context", current / "AGENTS.md")
        for part in relative.parts:
            current /= part
            self._file(found, "context", current / "AGENTS.md")
        self._file(found, "context", cwd / ".tau" / "AGENTS.md")
        self._file(found, "context", cwd / ".agents" / "AGENTS.md")

    def _extensions(self, found: dict[str, list[Path]], directory: Path) -> None:
        """Record extension entrypoints and declared extension packages.

        记录扩展入口点和声明的扩展包。
        """
        try:
            entries = tuple(directory.iterdir()) if directory.is_dir() else ()
        except OSError:
            found["extensions"].append(directory)
            return
        for path in entries:
            if path.name.startswith((".", "_")):
                continue
            if path.suffix == ".py" and self._is_candidate(path):
                found["extensions"].append(path)
            elif path.is_dir():
                for candidate in (path / "extension.py", path / "pyproject.toml"):
                    self._file(found, "extensions", candidate)


class ProjectTrustStore:
    """Versioned, locked, atomically replaced trust decision store.

    带版本、加锁并以原子方式替换的信任决策存储。
    """

    def __init__(self, paths: TauPaths | None = None) -> None:
        """Initialize trust-store paths from Tau filesystem settings.

        根据 Tau 文件系统设置初始化信任存储路径。
        """
        self.paths = paths or TauPaths()
        self.path = self.paths.home / "trust.json"
        self.lock_path = self.paths.home / "trust.json.lock"
        self.pending_path = self.paths.home / "trust.json.pending"

    def nearest(self, cwd: CanonicalProjectPath) -> SavedTrustEntry | None:
        """Return the nearest exact or inherited saved trust decision.

        返回最近的精确或继承的已保存信任决策。
        """
        decisions = self.read()
        current = cwd.value
        while True:
            decision = decisions.get(current)
            if decision is not None:
                return SavedTrustEntry(CanonicalProjectPath(current), decision)
            if current.parent == current:
                return None
            current = current.parent

    def read(self) -> dict[Path, TrustDecision]:
        """Read all saved trust decisions under the store lock.

        在存储锁下读取所有已保存的信任决策。
        """
        with self._locked():
            return self._read_unlocked()

    def set(self, path: CanonicalProjectPath, decision: TrustDecision) -> None:
        """Persist one exact trust decision.

        持久化一个精确信任决策。
        """
        with self._locked():
            decisions = self._read_unlocked()
            decisions[path.value] = decision
            self._write_unlocked(decisions)

    def trust_parent(self, cwd: CanonicalProjectPath) -> CanonicalProjectPath:
        """Persist trust for the canonical parent of a project directory.

        为项目目录的规范父目录持久化信任。
        """
        parent = CanonicalProjectPath(cwd.value.parent)
        with self._locked():
            decisions = self._read_unlocked()
            decisions.pop(cwd.value, None)
            decisions[parent.value] = "trusted"
            self._write_unlocked(decisions)
        return parent

    def remove(self, path: CanonicalProjectPath) -> None:
        """Remove one saved trust decision.

        移除一个已保存的信任决策。
        """
        with self._locked():
            decisions = self._read_unlocked()
            decisions.pop(path.value, None)
            self._write_unlocked(decisions)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the cross-process trust-store lock for one operation.

        在一次操作期间持有跨进程信任存储锁。
        """
        try:
            self.paths.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self.lock_path.open("a+b") as handle:
                os.chmod(self.lock_path, 0o600)
                _lock(handle)
                try:
                    yield
                finally:
                    _unlock(handle)
        except ProjectTrustError:
            raise
        except OSError as exc:
            raise ProjectTrustError(
                f"Could not lock project trust store {self.path}: {exc}"
            ) from exc

    def _read_unlocked(self) -> dict[Path, TrustDecision]:
        # A pending journal means an update did not reach its commit point.
        # Ordinary reads must never guess whether the interrupted operation was
        # a grant or a revocation: either direction could resurrect trust.
        # Recovery is attempted only by the writer that observed its own
        # failure; a journal left by a crash remains visibly fail-closed.
        # 待处理日志表示更新未到达提交点。普通读取绝不能猜测中断操作是授权还是撤销，
        # 因为任一方向都可能恢复信任。恢复仅由观察到自身失败的写入方尝试；崩溃遗留的
        # 日志保持可见的故障关闭状态。
        if self.pending_path.exists():
            raise ProjectTrustError(
                f"Project trust store {self.path} has an incomplete update; "
                f"pending journal requires explicit recovery: {self.pending_path}"
            )
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProjectTrustError(
                f"Could not read project trust store {self.path}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"version", "decisions"}:
            raise ProjectTrustError(f"Malformed project trust store {self.path}: unknown schema")
        if payload["version"] != 1 or not isinstance(payload["decisions"], list):
            raise ProjectTrustError(f"Unsupported or malformed project trust store {self.path}")
        result: dict[Path, TrustDecision] = {}
        for raw in payload["decisions"]:
            if not isinstance(raw, dict) or set(raw) != {"path", "decision"}:
                raise ProjectTrustError(f"Malformed decision in project trust store {self.path}")
            raw_path = raw["path"]
            decision = raw["decision"]
            if not isinstance(raw_path, str) or decision not in {"trusted", "untrusted"}:
                raise ProjectTrustError(f"Malformed decision in project trust store {self.path}")
            candidate = Path(raw_path)
            if not candidate.is_absolute() or Path(os.path.normpath(raw_path)) != candidate:
                raise ProjectTrustError(f"Noncanonical path in project trust store {self.path}")
            normalized = Path(os.path.normcase(raw_path)) if sys.platform == "win32" else candidate
            if sys.platform == "darwin" and candidate.exists():
                try:
                    normalized = _darwin_filesystem_path(candidate)
                except (OSError, UnicodeError) as exc:
                    raise ProjectTrustError(
                        f"Could not validate path casing in project trust store {self.path}: {exc}"
                    ) from exc
                if normalized != candidate:
                    raise ProjectTrustError(
                        f"Noncanonical path casing in project trust store {self.path}: {candidate}"
                    )
            if normalized in result:
                raise ProjectTrustError(f"Duplicate path in project trust store {self.path}")
            result[normalized] = decision
        return result

    def _write_unlocked(self, decisions: Mapping[Path, TrustDecision]) -> None:
        """Write trust decisions with a fail-closed recovery journal.

        使用故障关闭恢复日志写入信任决策。
        """
        payload = {
            "version": 1,
            "decisions": [
                {"path": str(path), "decision": decision}
                for path, decision in sorted(decisions.items(), key=lambda item: str(item[0]))
            ],
        }
        data = (json.dumps(payload, indent=2) + "\n").encode()
        prior_bytes = self.path.read_bytes() if self.path.exists() else None

        # Persist a fail-closed undo journal before touching trust.json. Readers
        # reject the store while this marker exists, so even failed recovery can
        # never expose a newly granting destination.
        # 在修改 trust.json 前持久化故障关闭撤销日志。该标记存在时读取方会拒绝存储，
        # 因此即使恢复失败，也绝不会暴露新授权的目标。
        journal = (b"present\n" + prior_bytes) if prior_bytes is not None else b"absent\n"
        try:
            self._atomic_replace(self.pending_path, journal, prefix=".trust-pending-")
            self._atomic_replace(self.path, data, prefix=".trust-")
        except OSError as exc:
            recovery_error = self._recover_unlocked()
            detail = f"; recovery failed: {recovery_error}" if recovery_error else ""
            raise ProjectTrustError(
                f"Could not write project trust store {self.path}: {exc}{detail}"
            ) from exc

        # The destination and its directory entry are durable. Failure to clear
        # the journal is still a failed update and must restore the prior state.
        # 目标及其目录项已经持久。清除日志失败仍属于更新失败，必须恢复先前状态。
        try:
            self.pending_path.unlink()
        except OSError as exc:
            recovery_error = self._recover_unlocked()
            detail = f"; recovery failed: {recovery_error}" if recovery_error else ""
            raise ProjectTrustError(
                f"Could not commit project trust store {self.path}: {exc}{detail}"
            ) from exc
        # Journal cleanup is not part of the data commit. If this fsync fails,
        # either the deletion persists (the durable destination grants) or the
        # journal reappears after a crash (reads fail closed).
        # 日志清理不属于数据提交。如果此 fsync 失败，要么删除已持久化（持久目标授权），
        # 要么日志在崩溃后重新出现（读取故障关闭）。
        with suppress(OSError):
            _fsync_directory(self.paths.home)

    def _atomic_replace(self, destination: Path, data: bytes, *, prefix: str) -> None:
        """Atomically replace a trust-store file and sync its directory.

        原子替换信任存储文件并同步其目录。
        """
        fd = -1
        temporary: Path | None = None
        try:
            fd, raw = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=self.paths.home)
            temporary = Path(raw)
            os.chmod(temporary, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            _fsync_directory(self.paths.home)
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)

    def _recover_unlocked(self) -> OSError | None:
        """Restore the journaled state; retain the marker on every failure.

        恢复日志记录的状态；任何失败都保留标记。
        """
        if not self.pending_path.exists():
            return None
        try:
            journal = self.pending_path.read_bytes()
            marker, separator, prior_bytes = journal.partition(b"\n")
            if not separator or marker not in {b"present", b"absent"}:
                raise OSError("malformed project trust recovery journal")
            if marker == b"present":
                self._atomic_replace(self.path, prior_bytes, prefix=".trust-rollback-")
            else:
                self.path.unlink(missing_ok=True)
                _fsync_directory(self.paths.home)
            self.pending_path.unlink()
            with suppress(OSError):
                _fsync_directory(self.paths.home)
        except OSError as exc:
            return exc
        return None


class ProjectTrustCoordinator:
    """Resolve and cache trust outcomes per canonical cwd for one invocation.

    在一次调用中按规范工作目录解析并缓存信任结果。
    """

    def __init__(
        self, store: ProjectTrustStore, detector: ProtectedResourceDetector | None = None
    ) -> None:
        """Initialize trust coordination with a store and protected-input detector.

        使用存储和受保护输入检测器初始化信任协调。
        """
        self.store = store
        self.detector = detector or ProtectedResourceDetector()
        self._cache: dict[Path, ProjectTrustResolution] = {}

    async def resolve(
        self,
        cwd: Path,
        *,
        override: TrustOverride | None = None,
        default: TrustDefault = "ask",
        interactive: bool = False,
        prompt: TrustPrompt | None = None,
        extension_deciders: Sequence[ExtensionDecider] = (),
        refresh: bool = False,
        cache_result: bool = True,
        persist: bool = True,
    ) -> tuple[ProtectedResourceSummary, ProjectTrustResolution]:
        """Resolve project trust from saved, default, extension, or user decisions.

        从已保存、默认、扩展或用户决策解析项目信任。
        """
        canonical = canonicalize_project_path(cwd, base=Path.cwd())
        summary = self.detector.detect(canonical)

        def finish(result: ProjectTrustResolution) -> ProjectTrustResolution:
            """Cache and return one completed trust resolution.

            缓存并返回一个已完成的信任解析结果。
            """
            if cache_result:
                self._cache[canonical.value] = result
            return result

        cached = self._cache.get(canonical.value)
        if cached is not None and cached.had_candidates:
            return summary, cached
        if cached is not None and not refresh and not summary.categories:
            return summary, cached
        diagnostics: list[str] = []
        if override is not None:
            result = ProjectTrustResolution(
                trusted=override == "approve",
                source="override",
                had_candidates=bool(summary.categories),
            )
            return summary, finish(result)
        if not summary.categories:
            result = ProjectTrustResolution(trusted=True, source="empty", had_candidates=False)
            return summary, finish(result)

        event = ProjectTrustEvent(
            cwd=canonical.value,
            mode="interactive" if interactive else "headless",
            has_ui=interactive and prompt is not None,
            categories=summary.categories,
            counts=summary.counts,
        )
        for decide in extension_deciders:
            try:
                extension_result = await decide(event)
            except Exception as exc:  # noqa: BLE001 - extensions safely defer on errors
                diagnostics.append(f"project_trust extension failed: {type(exc).__name__}: {exc}")
                continue
            if extension_result is None or extension_result.decision == "defer":
                continue
            trusted = extension_result.decision == "approve"
            saved_path: CanonicalProjectPath | None = None
            needs_persistence = False
            if extension_result.remember:
                saved_path = canonical
                if persist:
                    try:
                        self.store.set(canonical, "trusted" if trusted else "untrusted")
                    except ProjectTrustError as exc:
                        diagnostics.append(str(exc))
                        trusted = False
                        saved_path = None
                else:
                    needs_persistence = True
            result = ProjectTrustResolution(
                trusted=trusted,
                source="extension",
                saved_path=saved_path,
                diagnostics=tuple(diagnostics),
                needs_persistence=needs_persistence,
            )
            return summary, finish(result)

        inherited: SavedTrustEntry | None = None
        store_failed = False
        try:
            inherited = self.store.nearest(canonical)
        except ProjectTrustError as exc:
            store_failed = True
            diagnostics.append(str(exc))
        if inherited is not None:
            result = ProjectTrustResolution(
                trusted=inherited.decision == "trusted",
                source="saved",
                saved_path=inherited.path,
                diagnostics=tuple(diagnostics),
            )
            return summary, finish(result)
        if default != "ask":
            result = ProjectTrustResolution(
                trusted=default == "always" and not store_failed,
                source="default",
                diagnostics=tuple(diagnostics),
            )
            return summary, finish(result)
        if not interactive or prompt is None:
            result = ProjectTrustResolution(
                trusted=False, source="default", diagnostics=tuple(diagnostics)
            )
            return summary, finish(result)

        choice = await prompt(ProjectTrustRequest(canonical, summary, inherited))
        trusted = choice in {"trust-exact", "trust-parent", "trust-run"}
        saved_path = None
        needs_persistence = False
        try:
            if choice == "trust-exact":
                saved_path = canonical
                if persist:
                    self.store.set(canonical, "trusted")
                else:
                    needs_persistence = True
            elif choice == "trust-parent":
                saved_path = CanonicalProjectPath(canonical.value.parent)
                if persist:
                    saved_path = self.store.trust_parent(canonical)
                else:
                    needs_persistence = True
            elif choice == "decline-exact":
                saved_path = canonical
                if persist:
                    self.store.set(canonical, "untrusted")
                else:
                    needs_persistence = True
        except ProjectTrustError as exc:
            diagnostics.append(str(exc))
            trusted = False
            saved_path = None
        result = ProjectTrustResolution(
            trusted=trusted,
            source="ui",
            saved_path=saved_path,
            diagnostics=tuple(diagnostics),
            cancelled=choice is None,
            needs_persistence=needs_persistence,
        )
        return summary, finish(result)

    def commit(self, cwd: CanonicalProjectPath, result: ProjectTrustResolution) -> None:
        """Publish a staged resolution after its candidate is adopted.

        候选项被采用后发布暂存的解析结果。
        """
        if result.needs_persistence and result.saved_path is not None:
            # A store failure cannot undo an already adopted run.  The write is
            # intentionally fail-closed: the next process asks again rather
            # than accidentally treating an uncommitted grant as durable.
            # 存储失败无法撤销已采用的运行。写入有意采用故障关闭：下一个进程会再次询问，
            # 而不会错误地把未提交授权视为持久授权。
            with suppress(ProjectTrustError):
                self.store.set(
                    result.saved_path,
                    "trusted" if result.trusted else "untrusted",
                )
        self._cache[cwd.value] = result


def format_trust_diagnostic(
    summary: ProtectedResourceSummary, resolution: ProjectTrustResolution
) -> str:
    """Return one bounded, content-free decision diagnostic.

    返回一个有界且不含内容的决策诊断。
    """
    categories = (
        ", ".join(f"{category}={summary.counts[category]}" for category in summary.categories)
        or "none"
    )
    scope = f" via {resolution.saved_path.value}" if resolution.saved_path is not None else ""
    outcome = "trusted" if resolution.trusted else "untrusted"
    return (
        f"Project inputs for {summary.cwd.value}: {outcome} "
        f"(source={resolution.source}{scope}; {categories}). "
        "Project trust is an input-loading guard, not a sandbox."
    )


def _lock(handle: IO[bytes]) -> None:
    """Acquire an exclusive platform-specific file lock.

    获取平台专用的排他文件锁。
    """
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        raise ProjectTrustError(f"Could not acquire project trust lock: {exc}") from exc


def _unlock(handle: IO[bytes]) -> None:
    """Release a platform-specific file lock.

    释放平台专用文件锁。
    """
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    """Synchronize a directory entry when supported by the platform.

    在平台支持时同步目录项。
    """
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
