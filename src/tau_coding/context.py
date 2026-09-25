"""Project instruction discovery for Tau coding sessions.

为 Tau 编码会话发现项目指令。
"""

from __future__ import annotations

from pathlib import Path

from tau_coding.resources import ResourceDiagnostic, TauResourcePaths
from tau_coding.system_prompt import ProjectContextFile

PROJECT_MARKERS = (".git", "pyproject.toml", "uv.lock", "setup.py", "package.json")


def discover_project_context(
    paths: TauResourcePaths | None = None,
) -> tuple[ProjectContextFile, ...]:
    """Discover project instruction files for system prompt context.

    发现用于系统提示词上下文的项目指令文件。
    """
    context_files, _diagnostics = discover_project_context_with_diagnostics(paths)
    return context_files


def discover_project_context_with_diagnostics(
    paths: TauResourcePaths | None = None,
) -> tuple[tuple[ProjectContextFile, ...], tuple[ResourceDiagnostic, ...]]:
    """Discover project instruction files and return non-fatal diagnostics.

    发现项目指令文件，并返回非致命诊断信息。
    """
    resource_paths = paths or TauResourcePaths()
    context_files: list[ProjectContextFile] = []
    diagnostics: list[ResourceDiagnostic] = []
    for path in _context_file_candidates(resource_paths):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="context",
                    path=path,
                    message=f"could not read context file: {exc}",
                )
            )
            continue
        context_files.append(ProjectContextFile(path=str(path), content=content))
    return tuple(context_files), tuple(diagnostics)


def _context_file_candidates(paths: TauResourcePaths) -> tuple[Path, ...]:
    """Collect existing project instruction files in precedence order.

    按优先顺序收集现有的项目指令文件。
    """
    candidates: list[Path] = [paths.root / "AGENTS.md"]
    if paths.agents_root is not None:
        candidates.append(paths.agents_root / "AGENTS.md")

    if paths.cwd is not None and paths.project_resources_enabled:
        cwd = paths.cwd.expanduser().resolve()
        project_root = _find_project_root(cwd)
        candidates.extend(_ancestor_agents_files(project_root, cwd))
        tau_paths = paths._paths()
        candidates.extend(
            [
                tau_paths.project_tau_dir(cwd) / "AGENTS.md",
                tau_paths.project_agents_dir(cwd) / "AGENTS.md",
            ]
        )

    existing = [path for path in candidates if path.is_file()]
    return tuple(_dedupe_resolved_paths(existing))


def _find_project_root(cwd: Path) -> Path:
    """Find the nearest ancestor containing a recognized project marker.

    查找包含已识别项目标记的最近祖先目录。
    """
    for path in (cwd, *cwd.parents):
        if any((path / marker).exists() for marker in PROJECT_MARKERS):
            return path
    return cwd


def _ancestor_agents_files(project_root: Path, cwd: Path) -> list[Path]:
    """Return AGENTS.md candidates from the project root through the cwd.

    返回从项目根目录到当前目录沿途的 AGENTS.md 候选文件。
    """
    try:
        relative = cwd.relative_to(project_root)
    except ValueError:
        return [cwd / "AGENTS.md"]

    paths = [project_root / "AGENTS.md"]
    current = project_root
    for part in relative.parts:
        current = current / part
        paths.append(current / "AGENTS.md")
    return paths


def _dedupe_resolved_paths(paths: list[Path]) -> list[Path]:
    """Resolve and de-duplicate paths while preserving their order.

    解析路径并去重，同时保留原有顺序。
    """
    seen: set[Path] = set()
    deduped: list[Path] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(resolved)
    return deduped
