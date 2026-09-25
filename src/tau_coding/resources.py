"""Markdown resource path and frontmatter helpers.

Markdown 资源路径与前置元数据辅助工具。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from tau_agent.types import JSONValue
from tau_coding.paths import TauPaths


class ResourceError(ValueError):
    """Raised when Tau resources are invalid or cannot be expanded.

    Tau 资源无效或无法展开时抛出的异常。
    """


@dataclass(frozen=True, slots=True)
class SystemPromptResources:
    """Discovered Tau-native system-prompt file contents and sources.

    已发现的 Tau 原生系统提示词文件内容及来源。
    """

    custom_prompt: str | None = None
    custom_prompt_path: Path | None = None
    append_prompt: str | None = None
    append_prompts: tuple[str, ...] = ()
    append_prompt_paths: tuple[Path, ...] = ()
    diagnostics: tuple[ResourceDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class ResourceDiagnostic:
    """A non-fatal resource discovery problem or precedence note.

    非致命资源发现问题或优先级说明。
    """

    kind: str
    message: str
    path: Path | None = None
    name: str | None = None
    severity: str = "warning"

    def format(self) -> str:
        """Return a concise human-readable diagnostic line.

        返回简洁且便于阅读的诊断行。
        """
        parts = [self.severity, self.kind]
        if self.name is not None:
            parts.append(self.name)
        label = " ".join(parts)
        if self.path is None:
            return f"{label}: {self.message}"
        return f"{label}: {self.message} ({self.path})"


@dataclass(frozen=True, slots=True)
class TauResourcePaths:
    """Filesystem locations for Tau markdown resources.

    Tau Markdown 资源的文件系统位置。

    By default Tau loads Tau-native resources from the configured Tau home and
    `.agents` resources from the user home directory. When a cwd is provided,
    project-local `.tau` and `.agents` resources are loaded automatically as
    well.

    默认情况下，Tau 从配置的 Tau 主目录加载 Tau 原生资源，并从用户主目录加载
    `.agents` 资源。提供 cwd 后，还会自动加载项目本地的 `.tau` 和 `.agents` 资源。
    """

    root: Path = field(default_factory=lambda: TauPaths().home)
    cwd: Path | None = None
    agents_root: Path | None = field(default_factory=lambda: Path.home() / ".agents")
    paths: TauPaths | None = None
    project_resources_enabled: bool = True

    @property
    def skills_dir(self) -> Path:
        """Return the primary Tau skills directory.

        返回主要 Tau 技能目录。
        """
        return self.root / "skills"

    @property
    def prompts_dir(self) -> Path:
        """Return the primary Tau prompt templates directory.

        返回主要 Tau 提示词模板目录。
        """
        return self.root / "prompts"

    @property
    def system_prompt_path(self) -> Path:
        """Return the user-level replacement system-prompt file.

        返回用户级替换系统提示词文件。
        """
        return self.root / "SYSTEM.md"

    @property
    def append_system_prompt_path(self) -> Path:
        """Return the user-level appended system-prompt file.

        返回用户级追加系统提示词文件。
        """
        return self.root / "APPEND_SYSTEM.md"

    @property
    def skills_dirs(self) -> tuple[Path, ...]:
        """Return skill directories in increasing precedence order.

        按优先级递增顺序返回技能目录。

        Only the ``skills`` subdirectory of an ``.agents`` root is scanned,
        never the root ``.agents`` directory itself (which may contain
        ``README.md``, ``AGENTS.md``, etc.).

        只扫描 `.agents` 根目录下的 ``skills`` 子目录，绝不扫描 `.agents` 根目录
        本身，因为其中可能包含 ``README.md``、``AGENTS.md`` 等文件。
        """
        paths = self._paths()
        dirs = [self.skills_dir]
        if self.agents_root is not None:
            dirs.append(self.agents_root / "skills")
        if self.cwd is not None and self.project_resources_enabled:
            dirs.extend(
                [
                    paths.project_skills_dir(self.cwd),
                    paths.project_agents_skills_dir(self.cwd),
                ]
            )
        return tuple(_dedupe_paths(dirs))

    @property
    def themes_dirs(self) -> tuple[Path, ...]:
        """Return TUI theme directories in increasing precedence order.

        按优先级递增顺序返回 TUI 主题目录。

        Themes are Tau-specific, so unlike skills and prompts no ``.agents``
        directories are scanned.

        主题是 Tau 专用资源，因此与技能和提示词不同，不会扫描任何 `.agents` 目录。
        """
        paths = self._paths()
        dirs = [self.root / "themes"]
        if self.cwd is not None and self.project_resources_enabled:
            dirs.append(paths.project_themes_dir(self.cwd))
        return tuple(_dedupe_paths(dirs))

    @property
    def prompts_dirs(self) -> tuple[Path, ...]:
        """Return prompt template directories in increasing precedence order.

        按优先级递增顺序返回提示词模板目录。
        """
        paths = self._paths()
        dirs = [self.prompts_dir]
        if self.agents_root is not None:
            dirs.append(self.agents_root / "prompts")
        if self.cwd is not None and self.project_resources_enabled:
            dirs.extend(
                [
                    paths.project_prompts_dir(self.cwd),
                    paths.project_agents_prompts_dir(self.cwd),
                ]
            )
        return tuple(_dedupe_paths(dirs))

    def _paths(self) -> TauPaths:
        """Resolve the Tau path helper associated with this resource plan.

        解析与此资源计划关联的 Tau 路径辅助对象。
        """
        agents_home = self.agents_root or Path.home() / ".agents"
        return self.paths or TauPaths(home=self.root, agents_home=agents_home)


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    """Deduplicate expanded paths while preserving precedence order.

    在保留优先级顺序的同时去重展开后的路径。
    """
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped


def discover_system_prompt_resources(
    paths: TauResourcePaths,
    *,
    custom_prompt_explicit: bool = False,
    enabled: bool = True,
) -> SystemPromptResources:
    """Discover a precedence-selected base and cumulative append prompt files.

    发现按优先级选择的基础提示词及累计追加提示词文件。
    """
    if not enabled:
        return SystemPromptResources()

    diagnostics: list[ResourceDiagnostic] = []
    custom_prompt, custom_path = _discover_system_prompt_file(
        paths,
        filename="SYSTEM.md",
        label="replacement",
        explicit=custom_prompt_explicit,
        diagnostics=diagnostics,
    )
    append_prompts, append_paths = _discover_append_system_prompt_files(
        paths,
        diagnostics=diagnostics,
    )
    return SystemPromptResources(
        custom_prompt=custom_prompt,
        custom_prompt_path=custom_path,
        append_prompt="\n\n".join(append_prompts) if append_prompts else None,
        append_prompts=append_prompts,
        append_prompt_paths=append_paths,
        diagnostics=tuple(diagnostics),
    )


def _discover_system_prompt_file(
    paths: TauResourcePaths,
    *,
    filename: str,
    label: str,
    explicit: bool,
    diagnostics: list[ResourceDiagnostic],
) -> tuple[str | None, Path | None]:
    """Select and read one replacement system-prompt file by precedence.

    按优先级选择并读取一个替换系统提示词文件。
    """
    candidates: list[tuple[str, Path]] = []
    if paths.cwd is not None and paths.project_resources_enabled:
        candidates.append(("project", paths.cwd / ".tau" / filename))
    candidates.append(("user", paths.root / filename))

    existing: list[tuple[str, Path]] = []
    for scope, path in candidates:
        try:
            if path.exists():
                existing.append((scope, path))
        except OSError as exc:
            raise ResourceError(
                f"Could not inspect {label} system prompt file {path}: {exc}"
            ) from exc

    if explicit:
        for _scope, path in existing:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="system-prompt",
                    name=label,
                    path=path,
                    severity="info",
                    message="ignored because an explicit startup value takes precedence",
                )
            )
        return None, None
    if not existing:
        return None, None

    selected_scope, selected_path = existing[0]
    try:
        content = selected_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ResourceError(
            f"Could not read {label} system prompt file {selected_path} as UTF-8: {exc}"
        ) from exc

    diagnostics.append(
        ResourceDiagnostic(
            kind="system-prompt",
            name=label,
            path=selected_path,
            severity="info",
            message=f"selected {selected_scope} system prompt file",
        )
    )
    for _scope, path in existing[1:]:
        diagnostics.append(
            ResourceDiagnostic(
                kind="system-prompt",
                name=label,
                path=path,
                message=f"shadowed by higher-precedence file {selected_path}",
            )
        )
    return content, selected_path


def _discover_append_system_prompt_files(
    paths: TauResourcePaths,
    *,
    diagnostics: list[ResourceDiagnostic],
) -> tuple[tuple[str, ...], tuple[Path, ...]]:
    """Read every append file in broad-to-specific order.

    按从宽泛到具体的顺序读取所有追加文件。
    """
    candidates: list[tuple[str, Path]] = [("user", paths.root / "APPEND_SYSTEM.md")]
    if paths.cwd is not None and paths.project_resources_enabled:
        candidates.append(("project", paths.cwd / ".tau" / "APPEND_SYSTEM.md"))

    contents: list[str] = []
    selected_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for scope, path in candidates:
        try:
            resolved_path = path.expanduser().resolve()
            if resolved_path in seen_paths:
                continue
            seen_paths.add(resolved_path)
            exists = path.exists()
        except OSError as exc:
            raise ResourceError(
                f"Could not inspect append system prompt file {path}: {exc}"
            ) from exc
        if not exists:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ResourceError(
                f"Could not read append system prompt file {path} as UTF-8: {exc}"
            ) from exc
        contents.append(content)
        selected_paths.append(path)
        diagnostics.append(
            ResourceDiagnostic(
                kind="system-prompt",
                name="append",
                path=path,
                severity="info",
                message=f"selected {scope} system prompt file",
            )
        )

    return tuple(contents), tuple(selected_paths)


def resource_paths_with_cwd(
    paths: TauResourcePaths | None,
    cwd: Path,
) -> TauResourcePaths:
    """Return resource paths with a cwd available for project-local discovery.

    返回带 cwd 的资源路径，以支持项目本地发现。
    """
    if paths is None:
        return TauResourcePaths(cwd=cwd)
    # A resource plan is destination-bound. Replacement/resume callers may
    # supply a plan created for the source session, but only its user/global
    # roots and feature flags are reusable.
    # 资源计划绑定到目标。替换或恢复调用方可能提供为源会话创建的计划，但只有其
    # 用户或全局根目录及功能标志可以复用。
    return TauResourcePaths(
        root=paths.root,
        cwd=cwd,
        agents_root=paths.agents_root,
        paths=paths.paths,
        project_resources_enabled=paths.project_resources_enabled,
    )


def resource_paths_with_project_trust(
    paths: TauResourcePaths,
    *,
    trusted: bool,
) -> TauResourcePaths:
    """Return a coherent global-only or global-plus-project resource plan.

    返回一致的仅全局或全局加项目资源计划。
    """
    return TauResourcePaths(
        root=paths.root,
        cwd=paths.cwd,
        agents_root=paths.agents_root,
        paths=paths.paths,
        project_resources_enabled=trusted,
    )


def parse_markdown_resource(text: str) -> tuple[dict[str, str], str]:
    """Parse minimal YAML-like frontmatter from a markdown resource.

    从 Markdown 资源解析最小化的类 YAML 前置元数据。

    Only simple `key: value` pairs are supported. This keeps resource parsing
    dependency-free and avoids evaluating arbitrary code.

    仅支持简单的 `key: value` 对。这使资源解析无需依赖，并避免执行任意代码。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized

    end = normalized.find("\n---", 4)
    if end == -1:
        return {}, normalized

    raw_frontmatter = normalized[4:end]
    body = normalized[end + len("\n---") :]
    if body.startswith("\n"):
        body = body[1:]

    metadata: dict[str, str] = {}
    for line in raw_frontmatter.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition(":")
        if not separator:
            continue
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, body


def derive_description(content: str) -> str | None:
    """Derive a short description from markdown content.

    从 Markdown 内容派生简短描述。
    """
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or None
        return stripped
    return None


def metadata_to_json(metadata: dict[str, str]) -> dict[str, JSONValue]:
    """Convert string metadata into JSON-like values.

    将字符串元数据转换为类 JSON 值。
    """
    return dict(metadata)
