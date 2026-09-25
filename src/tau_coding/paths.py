"""Canonical filesystem paths for Tau user and project data.

Tau 用户与项目数据的规范文件系统路径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256
from os import environ
from pathlib import Path


def _default_tau_home() -> Path:
    """Return the configured Tau home, falling back to ``~/.tau``.

    返回配置的 Tau 主目录，未配置时回退到 ``~/.tau``。
    """
    value = environ.get("TAU_HOME")
    if value is None or value == "":
        return Path.home() / ".tau"

    try:
        path = Path(value).expanduser()
    except RuntimeError as exc:
        raise ValueError(
            "TAU_HOME could not expand '~'; use an absolute path or an existing user home"
        ) from exc
    if not path.is_absolute():
        raise ValueError("TAU_HOME must be an absolute path after '~' expansion")
    return path


@dataclass(frozen=True, slots=True)
class TauPaths:
    """Resolved Tau filesystem locations.

    解析后的 Tau 文件系统位置。

    Tau keeps durable application data under ``TAU_HOME`` (``~/.tau`` by
    default) while also loading project-local resources from the active working
    directory.

    Tau 将持久应用数据保存在 ``TAU_HOME``（默认为 ``~/.tau``）下，同时从当前工作
    目录加载项目本地资源。
    """

    home: Path = field(default_factory=_default_tau_home)
    agents_home: Path = field(default_factory=lambda: Path.home() / ".agents")

    @property
    def sessions_dir(self) -> Path:
        """Return the user-level session directory.

        返回用户级会话目录。
        """
        return self.home / "sessions"

    @property
    def logs_dir(self) -> Path:
        """Return Tau's user-level diagnostic log directory.

        返回 Tau 的用户级诊断日志目录。
        """
        return self.home / "logs"

    @property
    def agent_calls_log_path(self) -> Path:
        """Return the JSONL diagnostic log for agent-call failures.

        返回代理调用失败的 JSONL 诊断日志路径。
        """
        return self.logs_dir / "agent-calls.jsonl"

    @property
    def models_store_path(self) -> Path:
        """Return the persisted remote model-catalog cache path.

        返回持久化远程模型目录缓存路径。
        """
        return self.home / "models-store.json"

    @property
    def codex_version_store_path(self) -> Path:
        """Return the latest released Codex-version cache path.

        返回最新发布 Codex 版本的缓存路径。
        """
        return self.home / "codex-version-store.json"

    @property
    def codex_models_store_path(self) -> Path:
        """Return the account-scoped Codex model-catalog cache path.

        返回账户范围的 Codex 模型目录缓存路径。
        """
        return self.home / "codex-models-store.json"

    @property
    def extension_state_dir(self) -> Path:
        """Return the user-level state directory owned by built-in extensions.

        返回内置扩展拥有的用户级状态目录。
        """
        return self.home / "state" / "extensions"

    @property
    def llama_cpp_state_path(self) -> Path:
        """Return the safe built-in llama.cpp integration state path.

        返回安全的内置 llama.cpp 集成状态路径。
        """
        return self.extension_state_dir / "llama.cpp.json"

    @property
    def user_skills_dir(self) -> Path:
        """Return Tau's user-level skills directory.

        返回 Tau 的用户级技能目录。
        """
        return self.home / "skills"

    @property
    def user_prompts_dir(self) -> Path:
        """Return Tau's user-level prompt templates directory.

        返回 Tau 的用户级提示词模板目录。
        """
        return self.home / "prompts"

    @property
    def user_themes_dir(self) -> Path:
        """Return Tau's user-level TUI themes directory.

        返回 Tau 的用户级 TUI 主题目录。
        """
        return self.home / "themes"

    @property
    def user_extensions_dir(self) -> Path:
        """Return Tau's user-level extension directory.

        返回 Tau 的用户级扩展目录。
        """
        return self.home / "extensions"

    @property
    def user_agents_skills_dir(self) -> Path:
        """Return the user-level `.agents/skills` directory.

        返回用户级 `.agents/skills` 目录。
        """
        return self.agents_home / "skills"

    @property
    def user_agents_prompts_dir(self) -> Path:
        """Return the user-level `.agents/prompts` directory.

        返回用户级 `.agents/prompts` 目录。
        """
        return self.agents_home / "prompts"

    def project_tau_dir(self, cwd: Path) -> Path:
        """Return the project-local Tau resource directory.

        返回项目本地的 Tau 资源目录。
        """
        return cwd / ".tau"

    def project_agents_dir(self, cwd: Path) -> Path:
        """Return the project-local `.agents` resource directory.

        返回项目本地的 `.agents` 资源目录。
        """
        return cwd / ".agents"

    def project_skills_dir(self, cwd: Path) -> Path:
        """Return the project-local Tau skills directory.

        返回项目本地的 Tau 技能目录。
        """
        return self.project_tau_dir(cwd) / "skills"

    def project_prompts_dir(self, cwd: Path) -> Path:
        """Return the project-local Tau prompt templates directory.

        返回项目本地的 Tau 提示词模板目录。
        """
        return self.project_tau_dir(cwd) / "prompts"

    def project_themes_dir(self, cwd: Path) -> Path:
        """Return the project-local Tau TUI themes directory.

        返回项目本地的 Tau TUI 主题目录。
        """
        return self.project_tau_dir(cwd) / "themes"

    def project_agents_skills_dir(self, cwd: Path) -> Path:
        """Return the project-local `.agents/skills` directory.

        返回项目本地的 `.agents/skills` 目录。
        """
        return self.project_agents_dir(cwd) / "skills"

    def project_agents_prompts_dir(self, cwd: Path) -> Path:
        """Return the project-local `.agents/prompts` directory.

        返回项目本地的 `.agents/prompts` 目录。
        """
        return self.project_agents_dir(cwd) / "prompts"

    def project_session_dir(self, cwd: Path) -> Path:
        """Return the user-home session directory for a project cwd.

        返回项目工作目录对应的用户主目录会话目录。
        """
        resolved = cwd.resolve()
        digest = sha256(str(resolved).encode("utf-8")).hexdigest()[:6]
        slug = _slugify_path(resolved)
        return self.sessions_dir / f"{slug or 'project'}-{digest}"

    def default_session_path(self, cwd: Path) -> Path:
        """Return the default JSONL session path for a project cwd.

        返回项目工作目录对应的默认 JSONL 会话路径。
        """
        path = self.project_session_dir(cwd) / "default.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path


def _slugify_path(path: Path, *, max_length: int = 72) -> str:
    """Convert a filesystem path into a bounded session-directory slug.

    将文件系统路径转换为长度受限的会话目录标识。
    """
    parts = [part for part in path.parts if part not in (path.anchor, "")]
    try:
        relative_to_home = path.relative_to(Path.home())
    except ValueError:
        pass
    else:
        parts = ["home", *relative_to_home.parts]

    slug_parts = [
        normalized
        for part in parts
        if (normalized := re.sub(r"[^a-zA-Z0-9._-]+", "-", part).strip(".-_").lower())
    ]
    slug = "-".join(slug_parts)
    if len(slug) <= max_length:
        return slug

    suffix_parts: list[str] = []
    suffix_length = 0
    for part in reversed(slug_parts):
        next_length = suffix_length + len(part) + (1 if suffix_parts else 0)
        if next_length > max_length:
            break
        suffix_parts.append(part)
        suffix_length = next_length
    return "-".join(reversed(suffix_parts)) or slug[-max_length:].strip("-")
