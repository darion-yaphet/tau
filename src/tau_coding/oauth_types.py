"""Provider-neutral OAuth contracts used by Tau's coding application.

Tau 编码应用使用的提供商无关 OAuth 契约。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from tau_agent.types import JSONValue
from tau_coding.credentials import OAuthCredential

OAuthFlowKind = Literal["browser", "device_code"]


@dataclass(frozen=True, slots=True)
class OAuthAuthInfo:
    """Authorization URL and optional instructions for a browser flow.

    浏览器流程的授权 URL 和可选说明。
    """

    url: str
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthDeviceCodeInfo:
    """User-facing values returned by an OAuth device authorization request.

    OAuth 设备授权请求返回的面向用户的数据。
    """

    user_code: str
    verification_uri: str
    interval_seconds: float | None = None
    expires_in_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class OAuthPrompt:
    """Text input requested by an OAuth provider before or during login.

    OAuth 提供商在登录前或登录期间请求的文本输入。
    """

    message: str
    placeholder: str | None = None
    allow_empty: bool = False


@dataclass(frozen=True, slots=True)
class OAuthSelectOption:
    """One choice in a provider-defined OAuth selection prompt.

    提供商定义的 OAuth 选择提示中的一个选项。
    """

    id: str
    label: str


@dataclass(frozen=True, slots=True)
class OAuthSelectPrompt:
    """Selection input requested by an OAuth provider.

    OAuth 提供商请求的选择输入。
    """

    message: str
    options: tuple[OAuthSelectOption, ...]


@dataclass(frozen=True, slots=True)
class OAuthRuntimeAuth:
    """Request authentication derived from a stored OAuth credential.

    从已存储 OAuth 凭据派生的请求认证信息。
    """

    api_key: str
    base_url: str | None = None
    headers: Mapping[str, str] | None = None


AuthCallback = Callable[[OAuthAuthInfo], None]
DeviceCodeCallback = Callable[[OAuthDeviceCodeInfo], None]
PromptCallback = Callable[[OAuthPrompt], Awaitable[str]]
SelectCallback = Callable[[OAuthSelectPrompt], Awaitable[str | None]]
ManualCodeCallback = Callable[[], Awaitable[str]]
ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class OAuthLoginCallbacks:
    """Frontend-independent callbacks available to an OAuth login flow.

    OAuth 登录流程可用的前端无关回调。
    """

    on_auth: AuthCallback
    on_device_code: DeviceCodeCallback
    on_prompt: PromptCallback
    on_select: SelectCallback
    on_progress: ProgressCallback | None = None
    on_manual_code_input: ManualCodeCallback | None = None
    method: OAuthFlowKind | None = None


class OAuthProvider(Protocol):
    """Provider-specific OAuth behavior registered with Tau.

    注册到 Tau 的提供商专用 OAuth 行为。
    """

    @property
    def id(self) -> str:
        """Stable provider/credential identifier.

        稳定的提供商或凭据标识符。
        """
        ...

    @property
    def name(self) -> str:
        """User-facing provider name.

        面向用户的提供商名称。
        """
        ...

    @property
    def flow_kinds(self) -> Sequence[OAuthFlowKind]:
        """Interactive flow families supported by this provider.

        此提供商支持的交互流程类型。
        """
        ...

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredential:
        """Complete login and return credentials to persist.

        完成登录并返回待持久化的凭据。
        """
        ...

    async def refresh(self, credential: OAuthCredential) -> OAuthCredential:
        """Refresh an expired credential.

        刷新已过期的凭据。
        """
        ...

    def runtime_auth(self, credential: OAuthCredential) -> OAuthRuntimeAuth:
        """Convert stored credentials to request auth.

        将已存储凭据转换为请求认证信息。
        """
        ...


def oauth_metadata_string(
    metadata: Mapping[str, JSONValue],
    name: str,
) -> str | None:
    """Return one non-empty string from provider-specific OAuth metadata.

    从提供商专用 OAuth 元数据中返回一个非空字符串。
    """
    value = metadata.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
