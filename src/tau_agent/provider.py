"""Provider contract owned by Tau's portable agent layer.

由 Tau 可移植代理层拥有的模型提供者协议。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from tau_agent.messages import AgentMessage
from tau_agent.provider_events import AssistantMessageEvent
from tau_agent.tools import AgentTool


class CancellationToken(Protocol):
    """Cancellation contract for an active provider stream.

    用于活动模型提供者事件流的取消协议。
    """
    def is_cancelled(self) -> bool:
        """Return whether the current stream should stop.

        返回当前事件流是否应停止。
        """
        ...


class ModelProvider(Protocol):
    """Provider-neutral Pi-compatible model stream interface.

    与模型提供者无关的 Pi 兼容模型流接口。
    """

    def stream_response(
        self,
        *,
        model: str,
        system: str,
        messages: list[AgentMessage],
        tools: list[AgentTool],
        signal: CancellationToken | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[AssistantMessageEvent]:
        """Stream one model response as assistant message events.

        将一次模型响应以助手消息事件的形式进行流式传输。

        Providers may use ``session_id`` for request routing or prompt-cache
        affinity. Unsupported providers ignore it.

        模型提供者可使用 ``session_id`` 进行请求路由或提示缓存亲和性管理。
        不支持该功能的模型提供者会忽略它。
        """
        ...
