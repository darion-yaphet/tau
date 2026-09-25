"""Markdown skill loading and expansion.

Markdown 技能的加载与展开。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tau_coding.resources import (
    ResourceDiagnostic,
    ResourceError,
    TauResourcePaths,
    derive_description,
    parse_markdown_resource,
)


@dataclass(frozen=True, slots=True)
class Skill:
    """A markdown skill resource.

    一个 Markdown 技能资源。
    """

    name: str
    path: Path
    content: str
    description: str | None = None
    disable_model_invocation: bool = False


def is_skill_candidate(path: Path) -> bool:
    """Return whether an entry is a loader-eligible ``*/SKILL.md`` candidate.

    返回条目是否为加载器可接收的 ``*/SKILL.md`` 候选项。
    """
    return path.name == "SKILL.md" and (path.is_file() or path.is_symlink())


@dataclass(frozen=True, slots=True)
class SkillInvocation:
    """Parsed expanded skill invocation message.

    已解析的展开技能调用消息。
    """

    name: str
    location: str
    content: str
    additional_instructions: str | None = None


def load_skills(paths: TauResourcePaths | None = None) -> list[Skill]:
    """Load markdown skills from Tau and `.agents` resource directories.

    从 Tau 和 `.agents` 资源目录加载 Markdown 技能。

    Resource directories are loaded in increasing precedence order, so project
    resources override user resources with the same skill name. Duplicate names
    within the same directory remain invalid.

    资源目录按优先级从低到高加载，因此同名项目资源会覆盖用户资源。
    同一目录中的重复名称仍视为无效。
    """
    resource_paths = paths or TauResourcePaths()
    skills_by_name: dict[str, Skill] = {}

    for skills_dir in resource_paths.skills_dirs:
        for skill in _load_skills_from_dir(skills_dir):
            skills_by_name[skill.name] = skill

    return sorted(skills_by_name.values(), key=lambda skill: skill.name)


def load_skills_with_diagnostics(
    paths: TauResourcePaths | None = None,
) -> tuple[list[Skill], list[ResourceDiagnostic]]:
    """Load skills and return non-fatal discovery diagnostics.

    加载技能并返回非致命的发现诊断信息。

    Resource directories are loaded in increasing precedence order. Higher
    precedence resources replace lower precedence resources with the same name,
    and that replacement is reported as a diagnostic.

    资源目录按优先级从低到高加载。高优先级的同名资源会替换低优先级资源，
    并将该替换报告为诊断信息。
    """
    resource_paths = paths or TauResourcePaths()
    skills_by_name: dict[str, Skill] = {}
    diagnostics: list[ResourceDiagnostic] = []

    for skills_dir in resource_paths.skills_dirs:
        skills, directory_diagnostics = _load_skills_from_dir_with_diagnostics(skills_dir)
        diagnostics.extend(directory_diagnostics)
        for skill in skills:
            previous = skills_by_name.get(skill.name)
            if previous is not None:
                diagnostics.append(
                    ResourceDiagnostic(
                        kind="skill",
                        name=skill.name,
                        path=skill.path,
                        message=f"overrides lower-precedence resource at {previous.path}",
                    )
                )
            skills_by_name[skill.name] = skill

    return sorted(skills_by_name.values(), key=lambda skill: skill.name), diagnostics


def expand_skill_command(text: str, skills: Sequence[Skill]) -> str | None:
    """Expand `/skill:name` prompt text, or return None for non-skill text.

    展开 `/skill:name` 提示词文本；非技能文本则返回 None。
    """
    stripped = text.strip()
    if not stripped.startswith("/skill:"):
        return None

    command_and_request = stripped.split(maxsplit=1)
    command = command_and_request[0]
    request = command_and_request[1] if len(command_and_request) > 1 else None
    name = command.removeprefix("/skill:").strip()
    if not name:
        raise ResourceError("Skill command must include a skill name")

    skill_by_name = {skill.name: skill for skill in skills}
    skill = skill_by_name.get(name)
    if skill is None:
        raise ResourceError(f"Unknown skill: {name}")

    additional_instructions = request.strip() if request is not None else None
    return format_skill_invocation(skill, additional_instructions)


def format_skill_invocation(
    skill: Skill,
    additional_instructions: str | None = None,
) -> str:
    """Format a full skill invocation prompt.

    格式化完整的技能调用提示词。
    """
    skill_block = (
        f'<skill name="{skill.name}" location="{skill.path}">\n'
        f"References are relative to {skill.path.parent}.\n\n"
        f"{skill.content.strip()}\n"
        "</skill>"
    )
    if additional_instructions and additional_instructions.strip():
        return f"{skill_block}\n\n{additional_instructions.strip()}"
    return skill_block


def parse_skill_invocation(text: str) -> SkillInvocation | None:
    """Parse Tau's expanded skill invocation message format.

    解析 Tau 展开的技能调用消息格式。
    """
    match = re.match(
        r'^<skill name="([^"]+)" location="([^"]+)">\n([\s\S]*?)\n</skill>(?:\n\n([\s\S]+))?$',
        text,
    )
    if match is None:
        return None
    name, location, content, additional_instructions = match.groups()
    return SkillInvocation(
        name=name,
        location=location,
        content=content,
        additional_instructions=additional_instructions,
    )


def build_skill_index(skills: Sequence[Skill]) -> str:
    """Build a concise index of available skills for future system prompt assembly.

    构建简洁的可用技能索引，供后续组装系统提示词。
    """
    visible_skills = [skill for skill in skills if not skill.disable_model_invocation]
    if not visible_skills:
        return "Available skills: none"
    lines = ["Available skills:"]
    for skill in sorted(visible_skills, key=lambda item: item.name):
        description = skill.description or "No description"
        lines.append(f"- {skill.name}: {description}")
    return "\n".join(lines)


def _load_skills_from_dir(skills_dir: Path) -> list[Skill]:
    """Load one skill directory and raise on non-informational diagnostics.

    加载一个技能目录，并在出现非提示性诊断时抛出错误。
    """
    skills, diagnostics = _load_skills_from_dir_with_diagnostics(skills_dir)
    for diagnostic in diagnostics:
        # Bare-.md migration hints are informational — the file is skipped,
        # but that is not an error. Only fatal problems raise here; the full
        # diagnostic stream is available through ``load_skills_with_diagnostics``.
        #
        # 裸 .md 文件的迁移提示仅供参考，文件会被跳过，但这并非错误。这里只
        # 对致命问题抛出异常；完整诊断流可通过 ``load_skills_with_diagnostics`` 获取。
        if diagnostic.severity == "info":
            continue
        raise ResourceError(diagnostic.message)
    return skills


def _load_skills_from_dir_with_diagnostics(
    skills_dir: Path,
) -> tuple[list[Skill], list[ResourceDiagnostic]]:
    """Load skills from one directory and retain all discovery diagnostics.

    从一个目录加载技能，并保留所有发现诊断信息。
    """
    if not skills_dir.exists() or not skills_dir.is_dir():
        return [], []

    # Tau follows the Agent Skills spec everywhere: a skill is a directory
    # containing a ``SKILL.md`` file. Bare ``*.md`` files at the root of a
    # skills directory are not skills and are surfaced as a diagnostic that
    # tells the user how to migrate. This intentionally diverges from Pi,
    # which keeps a permissive "any .md is a skill" path in its own
    # ``.pi/skills/`` and ``~/.pi/agent/skills/`` folders purely for
    # backward compatibility. See ADR 0003.
    #
    # Tau 在所有位置都遵循 Agent Skills 规范：技能是包含 ``SKILL.md`` 文件
    # 的目录。技能目录根部的裸 ``*.md`` 文件不属于技能，而会生成诊断信息，
    # 告知用户如何迁移。这一点有意不同于 Pi；Pi 仅为向后兼容，仍在自身的
    # ``.pi/skills/`` 和 ``~/.pi/agent/skills/`` 目录中保留宽松的“任意 .md
    # 都是技能”路径。参见 ADR 0003。
    skills: list[Skill] = []
    diagnostics: list[ResourceDiagnostic] = []
    seen: set[str] = set()
    for path in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        skill_path: Path | None = None
        name = path.stem
        if path.is_dir():
            skill_path = path / "SKILL.md"
            name = path.name
            if not skill_path.exists():
                continue
        elif path.is_file() and path.suffix.lower() == ".md":
            if path.name.upper() == "AGENTS.MD":
                continue
            diagnostics.append(
                ResourceDiagnostic(
                    kind="skill",
                    name=path.stem,
                    path=path,
                    message=(
                        "bare .md files are no longer treated as skills; "
                        f"move it to {(path.parent / path.stem / 'SKILL.md')}"
                    ),
                    severity="info",
                )
            )
            continue
        else:
            continue

        if name in seen:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="skill",
                    name=name,
                    path=skill_path,
                    message=f"Duplicate skill name ignored in {skills_dir}",
                )
            )
            continue
        seen.add(name)
        try:
            skills.append(_load_skill(name, skill_path))
        except (OSError, UnicodeDecodeError) as exc:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="skill",
                    name=name,
                    path=skill_path,
                    message=f"could not read skill: {exc}",
                    severity="error",
                )
            )
    return skills, diagnostics


def _load_skill(name: str, path: Path) -> Skill:
    """Parse one SKILL.md file into a skill resource.

    将一个 SKILL.md 文件解析为技能资源。
    """
    raw = path.read_text(encoding="utf-8")
    metadata, content = parse_markdown_resource(raw)
    description = metadata.get("description") or derive_description(content)
    disable_model_invocation = (
        metadata.get("disable-model-invocation", "").strip().lower() == "true"
    )
    return Skill(
        name=name,
        path=path,
        content=content,
        description=description,
        disable_model_invocation=disable_model_invocation,
    )
