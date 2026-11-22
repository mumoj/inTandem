"""OpenCode's event stream: the envelope, and a recorder for the raw lines.

The stream is the only place some facts exist. A permission request blocks the
model but appears in neither the session status nor its messages, and a turn
that dies on a provider error looks exactly like ongoing work everywhere else.
So the supervisor consumes every event, and the raw stream is kept on disk as
evidence.
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from intandem.backend.jsonpath import dig, dig_str
from intandem.backend.stream import StreamRecorder

__all__ = ["ABORTED", "Event", "StreamRecorder"]

ABORTED = "MessageAbortedError"
"""The error OpenCode raises for an abort. Routine when a spec is replaced."""


class Event(BaseModel):
    """One bus event. ``properties`` stays a plain dict on purpose.

    OpenCode publishes nearly a hundred event types and adds more in minor
    releases. Modelling each would make the supervisor brittle against the one
    it does not care about; the handful it acts on are read by key.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: str
    properties: dict[str, Any] = Field(default_factory=dict[str, Any])
    _raw: str = PrivateAttr(default="")

    @classmethod
    def parse(cls, data: str) -> Event | None:
        """Parse one SSE ``data`` payload, or ``None`` if it is not an event."""
        try:
            event = cls.model_validate_json(data)
        except ValidationError:
            return None
        event._raw = data
        return event

    @classmethod
    def parse_global(cls, data: str, directory: str) -> Event | None:
        """One ``/global/event`` payload, if it belongs to ``directory``.

        A server shared by several tasks wraps every event as
        ``{"directory": ..., "payload": event}``. Events without a directory,
        such as ``server.connected``, concern every listener and are kept.
        """
        try:
            envelope = json.loads(data)
        except ValueError:
            return None
        if not isinstance(envelope, dict):
            return None
        wrapped = cast(dict[str, Any], envelope)
        if wrapped.get("directory") not in (None, directory):
            return None
        try:
            event = cls.model_validate(wrapped.get("payload"))
        except ValidationError:
            return None
        event._raw = data
        return event

    @property
    def raw(self) -> str:
        return self._raw

    def prop(self, *keys: str) -> object:
        """A nested property, or ``None``: ``event.prop("status", "type")``."""
        return dig(self.properties, *keys)

    @property
    def session_id(self) -> str | None:
        """The session an event belongs to, wherever this event type keeps it."""
        return (
            dig_str(self.properties, "sessionID")
            or dig_str(self.properties, "part", "sessionID")
            or dig_str(self.properties, "info", "sessionID")
        )

    @property
    def request_id(self) -> str | None:
        """The permission or question id: ``id`` when asked, ``requestID`` once settled."""
        return dig_str(self.properties, "requestID") or dig_str(self.properties, "id")

    @property
    def error_name(self) -> str | None:
        return dig_str(self.properties, "error", "name")

    @property
    def error_message(self) -> str:
        return dig_str(self.properties, "error", "data", "message") or ""
