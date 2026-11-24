"""Normalized event types produced by backend adapters.

Each harness emits its own event names and payload shapes. Adapters translate
those into ``BackendEvent`` instances with the fields needed by the supervisor,
activity tracker, and notifier. Unhandled events become ``EventType.OTHER`` so
they are still logged without affecting supervisor logic.

Enum values match the historical journal event names so existing journals and
the dashboard work consistently across harnesses.
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
    """Describes an update to a message part (tool call, text, or reasoning)."""

    kind: str
    """Part type such as ``tool``, ``text``, or ``reasoning``."""
    call_id: str = ""
    tool: str = ""
    running: bool = False
    """True while a tool call is still pending or executing."""
    detail: str = ""
    """Short one-line summary for a tool call, such as the command being run."""


class BackendEvent(BaseModel):
    type: EventType
    native_type: str
    """Original event name from the backend, kept for logging."""
    session_id: str | None = None

    request_id: str | None = None
    """ID of the permission request or question."""
    permission: str | None = None
    patterns: list[str] = Field(default_factory=list[str])
    question: str = ""
    """First question prompt text, used in one-line alert summaries."""

    child_id: str | None = None
    """Session ID of a newly created child session (for ``SESSION_CREATED``)."""
    child_parent_id: str | None = None
    child_policy: object = None
    """Opaque permission policy reported for the new child session, passed to ``guard_subagent``."""

    status: str | None = None
    """Session status for ``STATUS`` events: ``idle``, ``busy``, or ``retry``."""
    retry_detail: str | None = None

    error_name: str | None = None
    error_message: str = ""
    aborted: bool = False
    """True when an ``ERROR`` event resulted from an intentional abort."""

    part: PartUpdate | None = None
    delta_field: str | None = None
    """Field updated by a ``PART_DELTA`` event (e.g. ``text`` or ``tool``)."""
    delta: str = ""
    """Appended text for a ``PART_DELTA`` event, if provided by the backend."""

    model: str | None = None
    """Resolved model name reported by the backend (e.g. from init or assistant messages)."""

    native: dict[str, Any] = Field(default_factory=dict[str, Any])
    """Original backend payload, forwarded to ``$INTANDEM_EVENT``."""
    raw: str = ""
    """Raw serialized event line written by the stream recorder."""
