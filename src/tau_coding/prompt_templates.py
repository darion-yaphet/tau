"""Markdown prompt template loading and rendering.

Markdown 提示词模板的加载与渲染。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tau_coding.resources import (
    ResourceDiagnostic,
    ResourceError,
    TauResourcePaths,
    derive_description,
    parse_markdown_resource,
)

_TEMPLATE_VARIABLE_RE = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")
_PROMPT_ARGUMENT_RE = re.compile(
    r"\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"
)
_ARGUMENT_TEMPLATE_VARIABLES = {"arguments", "args"}
_RESERVED_TEMPLATE_NAMES = frozenset({"prompts", "skills", "tools", "reload"})


def is_prompt_template_candidate(path: Path) -> bool:
    """Return whether a directory entry is eligible for prompt loading.

    返回目录条目是否符合提示词加载条件。
    """
    return path.suffix.lower() == ".md" and path.stem.casefold() not in _RESERVED_TEMPLATE_NAMES


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A markdown prompt template resource.

    一个 Markdown 提示词模板资源。
    """

    name: str
    path: Path
    content: str
    description: str | None = None


def load_prompt_templates(paths: TauResourcePaths | None = None) -> list[PromptTemplate]:
    """Load markdown prompt templates from Tau and `.agents` resource directories.

    从 Tau 和 `.agents` 资源目录加载 Markdown 提示词模板。
    """
    resource_paths = paths or TauResourcePaths()
    templates_by_name: dict[str, PromptTemplate] = {}
    for prompts_dir in resource_paths.prompts_dirs:
        for template in _load_prompt_templates_from_dir(prompts_dir):
            templates_by_name[template.name] = template
    return sorted(templates_by_name.values(), key=lambda template: template.name)


def load_prompt_templates_with_diagnostics(
    paths: TauResourcePaths | None = None,
) -> tuple[list[PromptTemplate], list[ResourceDiagnostic]]:
    """Load prompt templates and return non-fatal discovery diagnostics.

    加载提示词模板并返回非致命的发现诊断信息。
    """
    resource_paths = paths or TauResourcePaths()
    templates_by_name: dict[str, PromptTemplate] = {}
    diagnostics: list[ResourceDiagnostic] = []
    for prompts_dir in resource_paths.prompts_dirs:
        templates, directory_diagnostics = _load_prompt_templates_from_dir_with_diagnostics(
            prompts_dir
        )
        diagnostics.extend(directory_diagnostics)
        for template in templates:
            previous = templates_by_name.get(template.name)
            if previous is not None:
                diagnostics.append(
                    ResourceDiagnostic(
                        kind="prompt",
                        name=template.name,
                        path=template.path,
                        message=f"overrides lower-precedence resource at {previous.path}",
                    )
                )
            templates_by_name[template.name] = template
    return sorted(templates_by_name.values(), key=lambda template: template.name), diagnostics


def parse_prompt_template_arguments(text: str) -> list[str]:
    """Parse prompt invocation arguments using Pi's simple quote rules.

    使用 Pi 的简单引号规则解析提示词调用参数。
    """
    arguments: list[str] = []
    current: list[str] = []
    quote: str | None = None

    for character in text:
        if quote is not None:
            if character == quote:
                quote = None
            else:
                current.append(character)
        elif character in {"'", '"'}:
            quote = character
        elif character.isspace():
            if current:
                arguments.append("".join(current))
                current = []
        else:
            current.append(character)

    if current:
        arguments.append("".join(current))
    return arguments


def substitute_prompt_template_args(content: str, arguments: Sequence[str]) -> str:
    """Substitute Pi-compatible positional and aggregate prompt arguments.

    替换兼容 Pi 的位置参数和聚合提示词参数。
    """
    all_arguments = " ".join(arguments)

    def replace(match: re.Match[str]) -> str:
        """Resolve one Pi-compatible argument placeholder.

        解析一个兼容 Pi 的参数占位符。
        """
        default_target = match.group(1)
        if default_target is not None:
            default_value = match.group(2) or ""
            if default_target in {"@", "ARGUMENTS"}:
                value = all_arguments
            else:
                index = int(default_target) - 1
                value = arguments[index] if 0 <= index < len(arguments) else ""
            return value or default_value

        slice_start = match.group(3)
        if slice_start is not None:
            start = max(int(slice_start) - 1, 0)
            slice_length = match.group(4)
            if slice_length is None:
                return " ".join(arguments[start:])
            return " ".join(arguments[start : start + int(slice_length)])

        simple = match.group(5) or ""
        if simple in {"@", "ARGUMENTS"}:
            return all_arguments
        index = int(simple) - 1
        return arguments[index] if 0 <= index < len(arguments) else ""

    return _PROMPT_ARGUMENT_RE.sub(replace, content)


def render_prompt_template(
    template: PromptTemplate,
    variables: Mapping[str, str],
    *,
    missing: str | None = None,
) -> str:
    """Render a prompt template using `{{ variable }}` placeholders.

    使用 `{{ variable }}` 占位符渲染提示词模板。

    By default, missing variables raise `ResourceError`. Callers that treat
    templates as user-facing shortcuts can pass `missing` to render absent
    variables as a fallback string instead.

    默认情况下，缺少变量会抛出 `ResourceError`。将模板作为面向用户快捷方式
    的调用方，可以传入 `missing`，以便用后备字符串渲染缺失变量。
    """

    def replace(match: re.Match[str]) -> str:
        """Resolve one named template variable.

        解析一个具名模板变量。
        """
        name = match.group(1)
        value = variables.get(name)
        if value is None:
            if missing is None:
                raise ResourceError(f"Missing prompt template variable: {name}")
            return missing
        return value

    return _TEMPLATE_VARIABLE_RE.sub(replace, template.content)


def expand_prompt_template_command(
    text: str,
    templates: Sequence[PromptTemplate],
) -> str | None:
    """Expand `/name [arguments]` text with a loaded prompt template.

    使用已加载的提示词模板展开 `/name [arguments]` 文本。

    Template names are matched by markdown filename stem. Invocation arguments use
    Pi-compatible `$1`, `$2`, `$@`, and `$ARGUMENTS` placeholders, including default
    values and simple slices. Legacy `{{ arguments }}` and `{{ args }}` placeholders
    remain supported. If a template has no argument placeholder, arguments are
    appended after a blank line.

    模板名称按 Markdown 文件名主干匹配。调用参数使用兼容 Pi 的 `$1`、`$2`、
    `$@` 和 `$ARGUMENTS` 占位符，包括默认值和简单切片。仍支持旧版
    `{{ arguments }}` 和 `{{ args }}` 占位符。如果模板中没有参数占位符，
    则在一个空行后追加参数。
    """
    stripped = text.strip()
    if not stripped.startswith("/") or stripped.startswith("//") or stripped.startswith("/skill:"):
        return None

    name, args = _parse_prompt_template_command(stripped)
    if not name:
        return None

    template = _find_prompt_template(name, templates)
    if template is None:
        return None

    arguments = parse_prompt_template_arguments(args)
    argument_text = " ".join(arguments)
    rendered = substitute_prompt_template_args(template.content, arguments)
    rendered = render_prompt_template(
        PromptTemplate(template.name, template.path, rendered, template.description),
        {"arguments": argument_text, "args": argument_text},
        missing="",
    )
    if arguments and not _template_references_arguments(template.content):
        return f"{rendered.rstrip()}\n\n{args}"
    return rendered


def _template_references_arguments(content: str) -> bool:
    """Return whether template content references invocation arguments.

    返回模板内容是否引用了调用参数。
    """
    return bool(_PROMPT_ARGUMENT_RE.search(content)) or any(
        match.group(1) in _ARGUMENT_TEMPLATE_VARIABLES
        for match in _TEMPLATE_VARIABLE_RE.finditer(content)
    )


def _find_prompt_template(
    name: str,
    templates: Sequence[PromptTemplate],
) -> PromptTemplate | None:
    """Find a prompt template by its normalized command name.

    按规范化的命令名称查找提示词模板。
    """
    normalized_name = name.strip().removeprefix("/").lower()
    for template in templates:
        if template.name.lower() == normalized_name:
            return template
    return None


def _parse_prompt_template_command(text: str) -> tuple[str, str]:
    """Split a prompt-template command into its name and arguments.

    将提示词模板命令拆分为名称和参数。
    """
    match = re.match(r"^/([^\s]+)(?:\s+([\s\S]*))?$", text)
    if match is None:
        return "", ""
    return match.group(1).lower(), (match.group(2) or "").strip()


def _load_prompt_templates_from_dir(prompts_dir: Path) -> list[PromptTemplate]:
    """Load one prompt directory and raise its first diagnostic as an error.

    加载一个提示词目录，并将首条诊断作为错误抛出。
    """
    templates, diagnostics = _load_prompt_templates_from_dir_with_diagnostics(prompts_dir)
    if diagnostics:
        first = diagnostics[0]
        raise ResourceError(first.message)
    return templates


def _load_prompt_templates_from_dir_with_diagnostics(
    prompts_dir: Path,
) -> tuple[list[PromptTemplate], list[ResourceDiagnostic]]:
    """Load templates from one directory with non-fatal diagnostics.

    从一个目录加载模板，并返回非致命诊断信息。
    """
    if not prompts_dir.exists() or not prompts_dir.is_dir():
        return [], []

    templates: list[PromptTemplate] = []
    diagnostics: list[ResourceDiagnostic] = []
    seen: set[str] = set()
    for path in sorted(prompts_dir.glob("*.md"), key=lambda item: item.name):
        name = path.stem
        if not is_prompt_template_candidate(path):
            diagnostics.append(
                ResourceDiagnostic(
                    kind="prompt",
                    name=name,
                    path=path,
                    message=(
                        f"prompt template name is reserved by the built-in /{name.casefold()} "
                        "command; template ignored"
                    ),
                )
            )
            continue
        if name in seen:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="prompt",
                    name=name,
                    path=path,
                    message=f"Duplicate prompt template name ignored in {prompts_dir}",
                )
            )
            continue
        seen.add(name)
        try:
            templates.append(_load_prompt_template(name, path))
        except (OSError, UnicodeDecodeError) as exc:
            diagnostics.append(
                ResourceDiagnostic(
                    kind="prompt",
                    name=name,
                    path=path,
                    message=f"could not read prompt template: {exc}",
                    severity="error",
                )
            )
    return templates, diagnostics


def _load_prompt_template(name: str, path: Path) -> PromptTemplate:
    """Parse one Markdown file into a prompt template resource.

    将一个 Markdown 文件解析为提示词模板资源。
    """
    raw = path.read_text(encoding="utf-8")
    metadata, content = parse_markdown_resource(raw)
    description = metadata.get("description") or derive_description(content)
    return PromptTemplate(name=name, path=path, content=content, description=description)
