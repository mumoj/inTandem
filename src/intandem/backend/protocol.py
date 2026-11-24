"""Protocol defining the backend interface required by the supervisor.

The supervisor interacts only with ``ImplementerBackend``, decoupling it from
individual harness transports and allowing tests to use an in-memory fake.

Pending permissions and questions use a pull model: backends that receive
push-style prompts store them locally and expose them via ``permissions()`` and
``questions()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from intandem.backend.events import BackendEvent
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


class ImplementerBackend(Protocol):
    async def health(self) -> Health: ...

    async def create_session(self, title: str, guard: Guard) -> Session: ...

    async def get_session(self, session_id: str) -> Session: ...

    async def guard_subagent(self, session_id: str, guard: Guard, inherited: object = None) -> None:
        """Deny edits to ``guard.protected`` in a subagent session.

        ``inherited`` is the opaque policy reported for the subagent, or
        ``None`` if the backend should fetch it first.
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

    def events(self) -> AsyncIterator[BackendEvent]:
        """Yield events from a single stream connection until the backend closes it."""
        ...
