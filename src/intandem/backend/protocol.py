"""``ImplementerBackend``: everything the supervisor needs from a harness.

The supervisor depends on this protocol, never on a harness's transport. Tests
swap in an in-memory fake, and each harness (OpenCode 1, OpenCode 2, Claude
Code, Codex) is one adapter that satisfies it.

The shape is pull: pending permissions and questions are listed on request.
A harness that pushes requests at its client keeps them in a pending map and
serves them from these methods.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol

from intandem.backend.guard import Guard
from intandem.backend.models import (
    FileDiff,
    Health,
    Message,
    ModelRef,
    PermissionReply,
    PermissionRequest,
    QuestionRequest,
    Session,
    SessionStatus,
)

if TYPE_CHECKING:
    # Still OpenCode's bus event until the stream gets its own neutral type.
    from intandem.opencode.events import Event


class ImplementerBackend(Protocol):
    async def health(self) -> Health: ...

    async def create_session(self, title: str, guard: Guard) -> Session: ...

    async def get_session(self, session_id: str) -> Session: ...

    async def guard_subagent(self, session_id: str, guard: Guard, inherited: object = None) -> None:
        """Forbid a subagent's session to edit ``guard.protected``, outright.

        ``inherited`` is the subagent's own policy as the backend reported it,
        opaque to inTandem. ``None`` means the backend should look it up.
        """
        ...

    async def session_status(self) -> dict[str, SessionStatus]: ...

    async def messages(self, session_id: str, limit: int | None = None) -> list[Message]: ...

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None: ...

    async def abort(self, session_id: str) -> bool: ...

    async def permissions(self) -> list[PermissionRequest]: ...

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool: ...

    async def questions(self) -> list[QuestionRequest]: ...

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool: ...

    async def reject_question(self, request_id: str) -> bool: ...

    async def diff(self, session_id: str) -> list[FileDiff]: ...

    def events(self) -> AsyncIterator[Event]: ...
