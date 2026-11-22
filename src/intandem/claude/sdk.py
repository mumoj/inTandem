"""The parts of the Claude Agent SDK the backend touches, loaded only when used.

``claude-agent-sdk`` is an optional extra. ``require_sdk`` is the one place that
checks for it, and turns a missing install into an error that says what to do.
``SDKClient`` is the slice of ``ClaudeSDKClient`` the backend calls, so tests
can hand it a fake.
"""

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
    """Why this run may bill the API instead of the user's subscription, if it may.

    The CLI prefers ``ANTHROPIC_API_KEY`` to the subscription login when the key
    is set. inTandem never sets or needs it; it only says so.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            "ANTHROPIC_API_KEY is set, so the claude CLI bills its requests to that API "
            "key instead of your Claude subscription. Unset it to use the subscription."
        )
    return None


class SDKClient(Protocol):
    """What the backend calls on ``claude_agent_sdk.ClaudeSDKClient``."""

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
