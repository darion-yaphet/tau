"""Trusted extension declarations bundled with Tau.

Tau 随附的可信扩展声明。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING

import httpx

from tau_coding.credentials import CredentialStore
from tau_coding.paths import TauPaths

if TYPE_CHECKING:
    from tau_coding.extensions.api import ExtensionAPI


@dataclass(frozen=True, slots=True)
class BuiltInExtensionContext:
    """Trusted runtime dependencies supplied only to bundled extensions.

    仅提供给内置扩展的可信运行时依赖。

    Filesystem extensions receive the ordinary :class:`ExtensionAPI` only. The
    context exists so a bundled integration uses the session's Tau home,
    credential store, environment snapshot, and deterministic HTTP client
    rather than silently reaching process-global defaults.

    文件系统扩展只接收普通的 :class:`ExtensionAPI`。此上下文让内置集成使用会话的
    Tau 主目录、凭据存储、环境快照和确定性 HTTP 客户端，而不是静默访问进程全局默认值。
    """

    paths: TauPaths
    credential_store: CredentialStore
    environment: Mapping[str, str]
    http_client: httpx.AsyncClient | None = None


BuiltInExtensionSetup = Callable[["ExtensionAPI"], None]
BuiltInExtensionContextSetup = Callable[["ExtensionAPI", BuiltInExtensionContext], None]


@dataclass(frozen=True, slots=True)
class BuiltInExtension:
    """One trusted extension setup function shipped as part of Tau.

    作为 Tau 一部分发布的可信扩展设置函数。

    Built-ins use the normal extension API and runtime lifecycle. They differ
    only in provenance: Tau declares their callable directly, loads them before
    filesystem extensions, and may hide them from ordinary extension listings.

    内置扩展使用普通扩展 API 和运行时生命周期。区别只在来源：Tau 直接声明其可调用
    对象，在文件系统扩展之前加载，并可在普通扩展列表中隐藏它们。
    """

    name: str
    setup: BuiltInExtensionSetup
    hidden: bool = True
    setup_with_context: BuiltInExtensionContextSetup | None = None

    def __post_init__(self) -> None:
        """Validate the declaration identity and synchronous setup callbacks.

        校验声明标识和同步设置回调。
        """
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Built-in extension name must be a non-empty string")
        if self.name != self.name.strip():
            raise ValueError("Built-in extension name must not have surrounding whitespace")
        if not callable(self.setup):
            raise ValueError("Built-in extension setup must be callable")
        setup_call = type(self.setup).__call__
        if iscoroutinefunction(self.setup) or iscoroutinefunction(setup_call):
            raise ValueError("Built-in extension setup must be a sync function")
        if not isinstance(self.hidden, bool):
            raise ValueError("Built-in extension hidden flag must be a boolean")
        if self.setup_with_context is not None:
            if not callable(self.setup_with_context):
                raise ValueError("Built-in contextual setup must be callable")
            context_setup_call = type(self.setup_with_context).__call__
            if iscoroutinefunction(self.setup_with_context) or iscoroutinefunction(
                context_setup_call
            ):
                raise ValueError("Built-in contextual setup must be a sync function")

    @property
    def source_id(self) -> str:
        """Return the stable host-owned source identity for this declaration.

        返回此声明稳定且由宿主拥有的来源标识。
        """
        return f"built-in:{self.name}"


def _llama_cpp_setup(api: ExtensionAPI) -> None:
    """Load the product extension with its normal process dependencies.

    使用正常进程依赖加载产品扩展。
    """
    from tau_coding.extensions.builtins.llama_cpp import setup

    setup(api)


def _llama_cpp_setup_with_context(
    api: ExtensionAPI,
    context: BuiltInExtensionContext,
) -> None:
    """Load the product extension lazily with trusted runtime dependencies.

    使用可信运行时依赖延迟加载产品扩展。
    """
    from tau_coding.extensions.builtins.llama_cpp import setup

    setup(api, context)


# Product capabilities are declared explicitly so filesystem discovery or
# project inputs cannot change what Tau treats as trusted.
# 产品能力会显式声明，因此文件系统发现或项目输入无法改变 Tau 视为可信的内容。
BUILT_IN_EXTENSIONS: tuple[BuiltInExtension, ...] = (
    BuiltInExtension(
        name="llama.cpp",
        setup=_llama_cpp_setup,
        hidden=True,
        setup_with_context=_llama_cpp_setup_with_context,
    ),
)

__all__ = [
    "BUILT_IN_EXTENSIONS",
    "BuiltInExtension",
    "BuiltInExtensionContext",
    "BuiltInExtensionContextSetup",
    "BuiltInExtensionSetup",
]
