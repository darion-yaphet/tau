"""Built-in and extension-ready OAuth provider registry.

内置及可由扩展接入的 OAuth 提供商注册表。
"""

from __future__ import annotations

from collections.abc import Iterable

from tau_coding.oauth import OpenAICodexOAuthProvider
from tau_coding.oauth_anthropic import AnthropicOAuthProvider
from tau_coding.oauth_github_copilot import GitHubCopilotOAuthProvider
from tau_coding.oauth_types import OAuthProvider

_BUILTIN_PROVIDERS: tuple[OAuthProvider, ...] = tuple(
    [AnthropicOAuthProvider(), GitHubCopilotOAuthProvider(), OpenAICodexOAuthProvider()]
)
_registry: dict[str, OAuthProvider] = {provider.id: provider for provider in _BUILTIN_PROVIDERS}


def get_oauth_provider(provider_id: str) -> OAuthProvider | None:
    """Return a registered OAuth provider by stable provider ID.

    按稳定的提供商 ID 返回已注册的 OAuth 提供商。
    """
    return _registry.get(provider_id)


def get_oauth_providers() -> tuple[OAuthProvider, ...]:
    """Return all registered OAuth providers in registration order.

    按注册顺序返回所有已注册的 OAuth 提供商。
    """
    return tuple(_registry.values())


def oauth_provider_ids() -> frozenset[str]:
    """Return IDs accepted by Tau's subscription login flow.

    返回 Tau 订阅登录流程接受的 ID。
    """
    return frozenset(_registry)


def register_oauth_provider(provider: OAuthProvider) -> None:
    """Register or replace an OAuth provider implementation.

    注册或替换 OAuth 提供商实现。
    """
    if not provider.id.strip():
        raise ValueError("OAuth provider id must not be empty")
    _registry[provider.id] = provider


def unregister_oauth_provider(provider_id: str) -> None:
    """Remove a custom provider or restore a replaced built-in provider.

    移除自定义提供商，或恢复被替换的内置提供商。
    """
    builtin = next(
        (provider for provider in _BUILTIN_PROVIDERS if provider.id == provider_id),
        None,
    )
    if builtin is None:
        _registry.pop(provider_id, None)
    else:
        _registry[provider_id] = builtin


def reset_oauth_providers(providers: Iterable[OAuthProvider] = _BUILTIN_PROVIDERS) -> None:
    """Reset the registry, primarily for deterministic extension tests.

    重置注册表，主要用于保证扩展测试的确定性。
    """
    _registry.clear()
    _registry.update((provider.id, provider) for provider in providers)
