"""The event stream in inTandem's own terms.

A harness publishes far more event types than inTandem acts on, under names
and shapes of its own. An adapter turns each one it receives into a
``BackendEvent``: one of the few types below, with the fields the supervisor,
activity tracker and notifier read already pulled out. Everything else is
``EventType.OTHER``, still recorded but otherwise ignored.

The type values are the names inTandem has always journalled, so existing
journals and the dashboard read the same whichever harness produced them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    CONNECTED = "server.connected"
    SESSION_CREATED = "session.created"
    STATUS = "session.status"
    IDLE = "session.idle"
    ERROR = "session.error"
    PART_UPDATED = "message.part.updated"
    PART_DELTA = "message.part.delta"
    PERMISSION_ASKED = "permission.asked"
    PERMISSION_REPLIED = "permission.replied"
    QUESTION_ASKED = "question.asked"
    QUESTION_REPLIED = "question.replied"
    QUESTION_REJECTED = "question.rejected"
    FILE_EDITED = "file.edited"
    OTHER = "other"


ASKED = frozenset({EventType.PERMISSION_ASKED, EventType.QUESTION_ASKED})
SETTLED = frozenset(
    {EventType.PERMISSION_REPLIED, EventType.QUESTION_REPLIED, EventType.QUESTION_REJECTED}
)
TURN_OVER = frozenset({EventType.IDLE, EventType.ERROR})


class PartUpdate(BaseModel):
    """One piece of a message changed: a tool call, or text or reasoning."""

    kind: str
    """``tool``, ``text``, ``reasoning``, or whatever else the harness calls it."""
    call_id: str = ""
    tool: str = ""
    running: bool = False
    """For a tool: still pending or running, rather than finished or failed."""
    detail: str = ""
    """For a tool: a bounded one-line summary, such as the command it runs."""


class BackendEvent(BaseModel):
    type: EventType
    native_type: str
    """What the harness called it, for logs."""
    session_id: str | None = None

    request_id: str | None = None
    """The permission or question asked, replied to or rejected."""
    permission: str | None = None
    patterns: list[str] = Field(default_factory=list[str])
    question: str = ""
    """The first question asked, for an alert's one-line summary."""

    child_id: str | None = None
    """``SESSION_CREATED``: the new session, and the one that started it."""
    child_parent_id: str | None = None
    child_policy: object = None
    """The new session's own permission policy, opaque, for ``guard_subagent``."""

    status: str | None = None
    """``STATUS``: ``idle``, ``busy`` or ``retry``."""
    retry_detail: str | None = None

    error_name: str | None = None
    error_message: str = ""
    aborted: bool = False
    """``ERROR`` caused by an abort, which inTandem sends itself when replacing a spec."""

    part: PartUpdate | None = None
    delta_field: str | None = None
    """``PART_DELTA``: which field grew, such as ``text``."""
    delta: str = ""
    """``PART_DELTA``: what was appended, when the harness sends it."""

    native: dict[str, Any] = Field(default_factory=dict[str, Any])
    """The event as the harness sent it, for ``$INTANDEM_EVENT``."""
    raw: str = ""
    """The line as it arrived, for the stream recorder."""
