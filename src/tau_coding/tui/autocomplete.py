"""Prompt autocomplete helpers for Tau's Textual TUI.

Tau Textual TUI 的提示输入自动补全辅助函数。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tau_coding.commands import CommandRegistry, SlashCommand
from tau_coding.prompt_templates import PromptTemplate
from tau_coding.skills import Skill

IGNORED_FILE_COMPLETION_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tau",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
MAX_FILE_COMPLETIONS = 50


@dataclass(frozen=True, slots=True)
class CompletionOption:
    """A possible argument completion value with optional picker metadata.

    可选的参数补全值及其选择器元数据。
    """

    value: str
    description: str | None = None


class CompletionKind(StrEnum):
    """Source of a prompt completion and its Enter-key behavior.

    提示补全项的来源及其回车键行为。
    """

    COMMAND = "command"
    PROMPT_TEMPLATE = "prompt_template"
    SKILL = "skill"
    ARGUMENT = "argument"
    FILE_REFERENCE = "file_reference"
    SHELL_PATH = "shell_path"


@dataclass(frozen=True, slots=True)
class CompletionItem:
    """One selectable prompt completion.

    一条可选择的提示补全项。
    """

    display: str
    replacement: str
    start: int
    end: int
    kind: CompletionKind
    description: str | None = None
    category: str | None = None

    def apply(self, text: str) -> str:
        """Apply this completion to input text.

        将此补全项应用到输入文本中。
        """
        return f"{text[: self.start]}{self.replacement}{text[self.end :]}"

    def cursor_after_apply(self) -> int:
        """Return the cursor offset just after the applied replacement.

        返回应用替换内容后光标所在的位置。
        """
        return self.start + len(self.replacement)


@dataclass(frozen=True, slots=True)
class CompletionState:
    """Current autocomplete state for the prompt input.

    提示输入当前的自动补全状态。
    """

    items: tuple[CompletionItem, ...] = ()
    selected_index: int = 0

    @property
    def selected(self) -> CompletionItem | None:
        """Return the currently selected completion item.

        返回当前选中的补全项。
        """
        if not self.items:
            return None
        return self.items[self.selected_index]

    def select_next(self) -> CompletionState:
        """Return a state with the next item selected.

        返回选中下一项后的状态。
        """
        if not self.items:
            return self
        return CompletionState(
            items=self.items,
            selected_index=(self.selected_index + 1) % len(self.items),
        )

    def select_previous(self) -> CompletionState:
        """Return a state with the previous item selected.

        返回选中上一项后的状态。
        """
        if not self.items:
            return self
        return CompletionState(
            items=self.items,
            selected_index=(self.selected_index - 1) % len(self.items),
        )


def build_completion_state(
    text: str,
    *,
    cursor: int | None = None,
    command_registry: CommandRegistry,
    skills: Sequence[Skill],
    prompt_templates: Sequence[PromptTemplate],
    model_names: Sequence[str] = (),
    provider_names: Sequence[str] = (),
    thinking_levels: Sequence[str] = (),
    theme_names: Sequence[str] = (),
    session_ids: Sequence[str] = (),
    session_options: Sequence[CompletionOption] = (),
    cwd: Path | None = None,
) -> CompletionState:
    """Build autocomplete suggestions for the current prompt text.

    根据当前提示文本构建自动补全建议。
    """
    cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
    if not text.startswith("/") or text.startswith("//"):
        if cwd is not None:
            shell_completions = _shell_path_completions(text=text, cursor=cursor, cwd=cwd)
            if shell_completions is not None:
                return CompletionState(shell_completions)
            return CompletionState(_file_reference_completions(text=text, cursor=cursor, cwd=cwd))
        return CompletionState()

    token_end = _first_token_end(text)
    token = text[:token_end]
    has_argument_text = token_end < len(text)
    if token.startswith("/skill:"):
        if has_argument_text and _matches_skill_command(token, skills):
            # Skill arguments are prompt text, so @ file references stay available.
            #
            # 技能参数属于提示文本，因此仍可使用 @ 文件引用。
            if cwd is not None:
                return CompletionState(
                    _file_reference_completions(text=text, cursor=cursor, cwd=cwd)
                )
            return CompletionState()
        return CompletionState(_skill_completions(token=token, token_end=token_end, skills=skills))

    if ":" in token:
        return CompletionState()

    argument_completions = _command_argument_completions(
        text=text,
        token_end=token_end,
        model_names=model_names,
        provider_names=provider_names,
        thinking_levels=thinking_levels,
        theme_names=theme_names,
        session_ids=session_ids,
        session_options=session_options,
    )
    if argument_completions is not None:
        return CompletionState(argument_completions)

    if has_argument_text and _matches_prompt_template_command(token, prompt_templates):
        if cwd is not None:
            return CompletionState(_file_reference_completions(text=text, cursor=cursor, cwd=cwd))
        return CompletionState()

    if has_argument_text and _matches_registered_command(token, command_registry):
        return CompletionState()

    return CompletionState(
        _command_completions(
            token=token,
            token_end=token_end,
            registry=command_registry,
            prompt_templates=prompt_templates,
        )
    )


def _file_reference_completions(*, text: str, cursor: int, cwd: Path) -> tuple[CompletionItem, ...]:
    """Build matching @ file-reference completions for the active token.

    为当前词元构建匹配的 @ 文件引用补全项。
    """
    token = _active_file_reference_token(text, cursor)
    if token is None:
        return ()
    start, end = token
    prefix = text[start + 1 : cursor]
    existing = text[start:end]
    external_completions = _external_file_reference_completions(
        prefix=prefix,
        existing=existing,
        start=start,
        end=end,
        cwd=cwd,
    )
    if external_completions is not None:
        return external_completions

    suggestions: list[CompletionItem] = []
    for path in _iter_file_reference_paths(cwd):
        relative = path.relative_to(cwd).as_posix()
        if prefix.lower() not in relative.lower():
            continue
        display = f"@{relative}{'/' if path.is_dir() else ''}"
        if display == existing:
            continue
        suggestions.append(
            CompletionItem(
                display=display,
                replacement=display,
                start=start,
                end=end,
                kind=CompletionKind.FILE_REFERENCE,
                description="File reference",
            )
        )
        if len(suggestions) >= MAX_FILE_COMPLETIONS:
            break
    return tuple(suggestions)


def _external_file_reference_completions(
    *, prefix: str, existing: str, start: int, end: int, cwd: Path
) -> tuple[CompletionItem, ...] | None:
    """Complete parent-directory paths that resolve outside the working tree.

    补全解析到当前工作目录树之外的父目录路径。
    """
    if prefix == ".." or prefix.endswith("/.."):
        target = cwd / prefix
        if not target.is_dir():
            return ()
        display = f"@{prefix}/"
        if display == existing:
            return ()
        return (
            CompletionItem(
                display=display,
                replacement=display,
                start=start,
                end=end,
                kind=CompletionKind.FILE_REFERENCE,
                description="File reference",
            ),
        )

    if not prefix.startswith("../"):
        return None

    parent_text, name_prefix = prefix.rsplit("/", 1)
    if any(part in IGNORED_FILE_COMPLETION_DIRS for part in Path(parent_text).parts):
        return ()
    parent_dir = cwd / parent_text
    if not parent_dir.is_dir():
        return ()

    try:
        children = sorted(parent_dir.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return ()

    suggestions: list[CompletionItem] = []
    for child in children:
        if child.name in IGNORED_FILE_COMPLETION_DIRS:
            continue
        if not child.name.lower().startswith(name_prefix.lower()):
            continue
        display = f"@{parent_text}/{child.name}{'/' if child.is_dir() else ''}"
        if display == existing:
            continue
        suggestions.append(
            CompletionItem(
                display=display,
                replacement=display,
                start=start,
                end=end,
                kind=CompletionKind.FILE_REFERENCE,
                description="File reference",
            )
        )
        if len(suggestions) >= MAX_FILE_COMPLETIONS:
            break
    return tuple(suggestions)


def _active_file_reference_token(text: str, cursor: int) -> tuple[int, int] | None:
    """Find the @ reference token surrounding the current cursor position.

    查找光标所在位置附近的 @ 引用词元。
    """
    token_start = max(text.rfind(" ", 0, cursor), text.rfind("\n", 0, cursor)) + 1
    at_index = text.rfind("@", token_start, cursor)
    if at_index == -1:
        return None
    end = cursor
    while end < len(text) and not text[end].isspace():
        end += 1
    return at_index, end


def _iter_file_reference_paths(cwd: Path) -> tuple[Path, ...]:
    """Collect non-ignored paths under the working directory.

    收集工作目录下未被忽略的路径。
    """
    if not cwd.exists() or not cwd.is_dir():
        return ()
    paths: list[Path] = []
    stack = [cwd]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            continue
        for child in children:
            if _is_ignored_file_completion_path(child, cwd=cwd):
                continue
            paths.append(child)
            if child.is_dir():
                stack.append(child)
    return tuple(paths)


def _is_ignored_file_completion_path(path: Path, *, cwd: Path) -> bool:
    """Return whether a path lies outside the root or under an ignored directory.

    判断路径是否位于根目录之外或被忽略的目录中。
    """
    try:
        relative_parts = path.relative_to(cwd).parts
    except ValueError:
        return True
    return any(part in IGNORED_FILE_COMPLETION_DIRS for part in relative_parts)


def _shell_path_completions(
    *, text: str, cursor: int, cwd: Path
) -> tuple[CompletionItem, ...] | None:
    """Build filesystem completions for a shell-command path at the cursor.

    为光标处 shell 命令中的文件系统路径构建补全项。
    """
    prefix_span = _shell_command_prefix_span(text)
    if prefix_span is None:
        return None
    if cursor < prefix_span[1]:
        return ()

    start, end = _active_shell_path_token(text=text, cursor=cursor, command_start=prefix_span[1])
    token = text[start:cursor]
    if not token:
        return ()

    shell_path = _parse_shell_path_token(token)
    if shell_path is None:
        return ()
    parent_text, name_prefix, replacement_prefix = shell_path

    parent_dir = cwd / parent_text if parent_text else cwd
    if not parent_dir.exists() or not parent_dir.is_dir():
        return ()
    if parent_dir != cwd and _is_ignored_file_completion_path(parent_dir, cwd=cwd):
        return ()

    try:
        children = sorted(parent_dir.iterdir(), key=lambda path: path.name.lower())
    except OSError:
        return ()

    suggestions: list[CompletionItem] = []
    for child in children:
        if _is_ignored_file_completion_path(child, cwd=cwd):
            continue
        if not child.name.lower().startswith(name_prefix.lower()):
            continue
        relative = child.relative_to(cwd).as_posix()
        replacement = f"{replacement_prefix}{relative}{'/' if child.is_dir() else ''}"
        if replacement == text[start:end]:
            continue
        suggestions.append(
            CompletionItem(
                display=replacement,
                replacement=replacement,
                start=start,
                end=end,
                kind=CompletionKind.SHELL_PATH,
                description="Directory" if child.is_dir() else "File",
            )
        )
        if len(suggestions) >= MAX_FILE_COMPLETIONS:
            break
    return tuple(suggestions)


def _shell_command_prefix_span(text: str) -> tuple[int, int] | None:
    """Return the span of a leading shell escape marker, if present.

    如果文本以 shell 转义标记开头，则返回该标记的范围。
    """
    leading_whitespace = len(text) - len(text.lstrip())
    stripped = text[leading_whitespace:]
    if stripped.startswith("!!"):
        return (leading_whitespace, leading_whitespace + 2)
    if stripped.startswith("!"):
        return (leading_whitespace, leading_whitespace + 1)
    return None


def _active_shell_path_token(*, text: str, cursor: int, command_start: int) -> tuple[int, int]:
    """Find the shell token around the cursor while honoring backslash escapes.

    查找光标所在的 shell 词元，并正确处理反斜杠转义。
    """
    token_start = command_start
    escaped = False
    for index in range(cursor - 1, command_start - 1, -1):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char.isspace():
            token_start = index + 1
            break
    return token_start, _shell_token_end(text, cursor)


def _shell_token_end(text: str, cursor: int) -> int:
    """Find the end of a shell token, skipping escaped characters.

    查找 shell 词元的结束位置，并跳过转义字符。
    """
    index = cursor
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char.isspace():
            return index
        index += 1
    return len(text)


def _parse_shell_path_token(token: str) -> tuple[str, str, str] | None:
    """Split a safe relative shell path into parent, prefix, and output prefix.

    将安全的相对 shell 路径拆分为父目录、名称前缀和输出前缀。
    """
    replacement_prefix = ""
    path_text = token
    if path_text.startswith("./"):
        replacement_prefix = "./"
        path_text = path_text[2:]
    if path_text.startswith(("/", "~")):
        return None
    if any(char in path_text for char in "\"'`$*?[{"):
        return None

    parent_text, separator, name_prefix = path_text.rpartition("/")
    if separator and not parent_text:
        return None

    parent_parts = parent_text.split("/") if parent_text else []
    if any(part in {"", ".", ".."} for part in parent_parts):
        return None
    return parent_text, name_prefix, replacement_prefix


def _matches_skill_command(token: str, skills: Sequence[Skill]) -> bool:
    """Return whether a slash token names an available skill.

    判断斜杠命令词元是否对应可用技能。
    """
    command_name = token.removeprefix("/skill:").lower()
    return any(skill.name.lower() == command_name for skill in skills)


def _matches_prompt_template_command(
    token: str, prompt_templates: Sequence[PromptTemplate]
) -> bool:
    """Return whether a slash token names an available prompt template.

    判断斜杠命令词元是否对应可用提示模板。
    """
    command_name = token.removeprefix("/").lower()
    return any(template.name.lower() == command_name for template in prompt_templates)


def _matches_registered_command(token: str, registry: CommandRegistry) -> bool:
    """Return whether the command registry contains this slash token.

    判断命令注册表中是否包含此斜杠命令词元。
    """
    command_name = token.removeprefix("/").lower()
    return registry.get(command_name) is not None


def _command_completions(
    *,
    token: str,
    token_end: int,
    registry: CommandRegistry,
    prompt_templates: Sequence[PromptTemplate],
) -> tuple[CompletionItem, ...]:
    """Combine matching slash-command aliases and prompt-template suggestions.

    合并匹配的斜杠命令别名与提示模板建议。
    """
    prefix = token.removeprefix("/").lower()
    command_suggestions: list[CompletionItem] = []
    for command in registry.list_commands():
        command_suggestions.extend(
            _command_alias_completions(command, prefix=prefix, token_end=token_end)
        )
    prompt_suggestions = [
        CompletionItem(
            display=f"/{template.name}",
            replacement=f"/{template.name}",
            start=0,
            end=token_end,
            kind=CompletionKind.PROMPT_TEMPLATE,
            description=template.description or "Prompt template",
            category="Custom prompts",
        )
        for template in prompt_templates
        if template.name.lower().startswith(prefix)
    ]
    return (
        *sorted(command_suggestions, key=lambda item: _command_completion_sort_key(item, prefix)),
        *sorted(prompt_suggestions, key=lambda item: _command_completion_sort_key(item, prefix)),
    )


def _command_completion_sort_key(item: CompletionItem, prefix: str) -> tuple[int, str]:
    """Rank direct prefix matches before other command search matches.

    将直接前缀匹配项排在其他命令搜索匹配项之前。
    """
    if not prefix:
        return (0, item.display)
    display_name = item.display.removeprefix("/").removesuffix(":").lower()
    direct_match_rank = 0 if display_name.startswith(prefix) else 1
    return (direct_match_rank, item.display)


def _command_alias_completions(
    command: SlashCommand, *, prefix: str, token_end: int
) -> list[CompletionItem]:
    """Build distinct completion items from a command's names and aliases.

    根据命令名称与别名构建去重后的补全项。
    """
    names = (
        (command.name,) if not prefix else (command.name, *command.aliases, *command.search_terms)
    )
    suggestions: list[CompletionItem] = []
    seen: set[str] = set()
    for name in names:
        if not name.startswith(prefix):
            continue
        replacement_name = name if name in (command.name, *command.aliases) else command.name
        display = f"/{replacement_name}"
        replacement = f"/{replacement_name}"
        if command.name == "skill" and replacement_name == command.name:
            display = "/skill:"
            replacement = "/skill:"
        if display in seen:
            continue
        seen.add(display)
        suggestions.append(
            CompletionItem(
                display=display,
                replacement=replacement,
                start=0,
                end=token_end,
                kind=CompletionKind.COMMAND,
                description=command.description,
                category="Commands",
            )
        )
    return suggestions


def _skill_completions(
    *, token: str, token_end: int, skills: Sequence[Skill]
) -> tuple[CompletionItem, ...]:
    """Build matching completions for the available skills.

    为可用技能构建匹配的补全项。
    """
    prefix = token.removeprefix("/skill:").lower()
    suggestions = [
        CompletionItem(
            display=f"/skill:{skill.name}",
            replacement=f"/skill:{skill.name}",
            start=0,
            end=token_end,
            kind=CompletionKind.SKILL,
            description=skill.description,
        )
        for skill in sorted(skills, key=lambda item: item.name)
        if skill.name.lower().startswith(prefix)
    ]
    return tuple(suggestions)


def _command_argument_completions(
    *,
    text: str,
    token_end: int,
    model_names: Sequence[str],
    provider_names: Sequence[str],
    thinking_levels: Sequence[str],
    theme_names: Sequence[str],
    session_ids: Sequence[str],
    session_options: Sequence[CompletionOption],
) -> tuple[CompletionItem, ...] | None:
    """Dispatch argument completion to the option set for a known command.

    将参数补全分派给已知命令对应的选项集合。
    """
    if token_end >= len(text):
        return None

    command_name = text[:token_end].removeprefix("/").lower()
    if command_name in {"model", "scoped-models"}:
        return _value_completions(
            text=text,
            start=token_end + 1,
            options=_completion_options(model_names, description="Switch model"),
            sort=True,
        )
    if command_name in {"login", "logout"}:
        return _value_completions(
            text=text,
            start=token_end + 1,
            options=_completion_options(provider_names, description="Switch provider"),
            sort=True,
        )
    if command_name == "resume":
        return _value_completions(
            text=text,
            start=token_end + 1,
            options=(
                session_options
                if session_options
                else _completion_options(session_ids, description="Resume session")
            ),
            sort=False,
        )
    if command_name == "theme":
        return _value_completions(
            text=text,
            start=token_end + 1,
            options=_completion_options(theme_names, description="Set TUI theme"),
            sort=False,
        )
    return None


def _value_completions(
    *,
    text: str,
    start: int,
    options: Sequence[CompletionOption],
    sort: bool,
) -> tuple[CompletionItem, ...]:
    """Filter argument options by the active token and create completion items.

    按当前词元筛选参数选项并创建补全项。
    """
    end = _argument_token_end(text, start)
    prefix = text[start:end].lower()
    ordered_options = sorted(options, key=lambda item: item.value) if sort else options
    return tuple(
        CompletionItem(
            display=option.value,
            replacement=option.value,
            start=start,
            end=end,
            kind=CompletionKind.ARGUMENT,
            description=option.description,
        )
        for option in ordered_options
        if option.value.lower().startswith(prefix)
    )


def _completion_options(
    values: Sequence[str],
    *,
    description: str,
) -> tuple[CompletionOption, ...]:
    """Wrap plain string values in completion options with shared metadata.

    将普通字符串值包装为带有统一元数据的补全选项。
    """
    return tuple(CompletionOption(value=value, description=description) for value in values)


def _first_token_end(text: str) -> int:
    """Return the end offset of the first space-delimited token.

    返回第一个以空格分隔的词元的结束位置。
    """
    separator = text.find(" ")
    return len(text) if separator == -1 else separator


def _argument_token_end(text: str, start: int) -> int:
    """Return the end offset of the argument token starting at ``start``.

    返回从 ``start`` 开始的参数词元结束位置。
    """
    separator = text.find(" ", start)
    return len(text) if separator == -1 else separator
