"""Lazy loader and minimal client protocol for the optional ``claude-agent-sdk`` extra."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING, Protocol

from intandem.backend.errors import BackendError

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

INSTALL_HINT = (
    "the claude harness needs the Claude Agent SDK: install inTandem with the "
    "`claude` extra, e.g. `uv tool install 'inTandem[claude]'` or `pip install 'inTandem[claude]'`"
)


def require_sdk() -> None:
    """Raise ``BackendError`` unless ``claude_agent_sdk`` can be imported."""
    try:
        import claude_agent_sdk  # noqa: F401  # pyright: ignore[reportUnusedImport]
    except ImportError as exc:
        raise BackendError(INSTALL_HINT) from exc


def api_key_warning() -> str | None:
    """Warn if ``ANTHROPIC_API_KEY`` is set, since ``claude`` prefers it over subscription auth."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "ANTHROPIC_API_KEY is set, so the claude CLI bills its requests to that API "
            "key instead of your Claude subscription. Unset it to use the subscription."
        )
    return None


class SDKClient(Protocol):
    """Subset of ``claude_agent_sdk.ClaudeSDKClient`` used by ``ClaudeCodeBackend``."""

    async def connect(self) -> None: ...

    async def query(self, prompt: str, session_id: str = "default") -> None: ...

    def receive_messages(self) -> AsyncIterator[object]: ...

    async def interrupt(self) -> None: ...

    async def set_model(self, model: str | None = None) -> None: ...

    async def disconnect(self) -> None: ...


ClientFactory = Callable[["ClaudeAgentOptions"], SDKClient]


def default_client(options: ClaudeAgentOptions) -> SDKClient:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options=options)
