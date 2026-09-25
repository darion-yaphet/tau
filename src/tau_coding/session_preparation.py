"""Shared trust-aware staging for coding-session startup and replacement.

用于编码会话启动和替换的共享信任感知暂存流程。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from tau_agent.provider import ModelProvider
from tau_coding.session import CodingSession, CodingSessionConfig


@dataclass(frozen=True, slots=True)
class SessionPreparationRequest:
    """Frontend-neutral request for a staged coding session.

    与前端无关的编码会话暂存请求。
    """

    storage: object
    destination_cwd: Path
    model: str
    provider: str | None = None
    session_provider: str | None = None
    session_id: str | None = None


@dataclass(slots=True)
class PreparedCodingSession:
    """A candidate session whose resources are not authoritative until adopted.

    一个候选会话，其资源在被接纳前不具备权威性。
    """

    session: CodingSession
    _state: str = "prepared"

    @property
    def provider(self) -> ModelProvider:
        """Return the model provider owned by the prepared session.

        返回暂存会话所拥有的模型提供者。
        """
        return self.session.provider

    async def adopt(self) -> CodingSession:
        """Commit staged entries, then transfer the candidate's ownership.

        提交暂存条目，然后转移候选会话的所有权。
        """
        if self._state != "prepared":
            raise RuntimeError(f"Prepared session is already {self._state}")
        trust_resolution = getattr(self.session, "project_trust_resolution", None)
        if trust_resolution is not None and trust_resolution.cancelled:
            await self.abort()
            raise ValueError("Project trust decision cancelled; session was not adopted")
        try:
            commit = getattr(self.session, "_commit_prepared_entries", None)
            if commit is not None:
                await commit()
        except BaseException:
            await self.abort()
            raise
        self._state = "adopted"
        return self.session

    async def abort(self) -> None:
        """Close an unpublished candidate exactly once.

        对尚未发布的候选会话执行一次且仅一次关闭。
        """
        if self._state != "prepared":
            return
        self._state = "aborted"
        close = getattr(self.session, "aclose", None)
        if close is not None:
            await close()


async def prepare_coding_session(
    config: CodingSessionConfig,
    *,
    session_loader: type[CodingSession] | None = None,
) -> PreparedCodingSession:
    """Prepare a session through the shared trust/provider lifecycle.

    通过共享的信任与提供者生命周期准备会话。

    Application frontends use this entry point with authoritative writes
    deferred. Adoption appends the complete staged batch before exposing the
    candidate. Supplying a provider remains the compatibility seam for static
    embedded callers, which retain the historical lazy initial write.

    应用前端通过此入口使用延迟的权威写入。接纳操作会在暴露候选会话前
    追加完整的暂存批次。传入提供者仍是静态嵌入调用方的兼容接口，保留
    原有的延迟首次写入行为。
    """
    # Every frontend gets the same candidate-first durability boundary, even
    # when it supplies a compatibility provider object. Provider ownership is
    # controlled independently by CodingSessionConfig.owns_initial_provider.
    #
    # 每个前端都采用相同的候选优先持久化边界，即使其提供了兼容的提供者
    # 对象。提供者所有权由 CodingSessionConfig.owns_initial_provider 独立控制。
    staged_config = replace(config, defer_authoritative_writes=True)
    loader = session_loader or CodingSession
    session = await loader.load(staged_config)
    return PreparedCodingSession(session)


__all__ = [
    "PreparedCodingSession",
    "SessionPreparationRequest",
    "prepare_coding_session",
]
