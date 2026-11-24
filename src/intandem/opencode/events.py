"""OpenCode 1.x SSE event model and raw stream recorder re-export.

Some session transitions (permission prompts, questions, and provider errors)
are only visible on the event stream, so the supervisor consumes all events and
logs the raw stream to disk.
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from intandem.backend.jsonpath import dig, dig_str
from intandem.backend.stream import StreamRecorder

__all__ = ["ABORTED", "Event", "StreamRecorder"]

ABORTED = "MessageAbortedError"
"""Error name OpenCode emits when a turn is aborted."""


class Event(BaseModel):
    """Single OpenCode bus event with an untyped ``properties`` dict.

    Keeping ``properties`` untyped avoids breaking when OpenCode adds new event
    types or fields in minor releases.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    type: str
    properties: dict[str, Any] = Field(default_factory=dict[str, Any])
    _raw: str = PrivateAttr(default="")

    @classmethod
    def parse(cls, data: str) -> Event | None:
        """Parse an SSE ``data`` payload, returning ``None`` if it is not a valid event."""
        try:
            event = cls.model_validate_json(data)
        except ValidationError:
            return None
        event._raw = data
        return event

    @classmethod
    def parse_global(cls, data: str, directory: str) -> Event | None:
        """Parse a ``/global/event`` envelope if it applies to ``directory``.

        Shared servers wrap events as ``{"directory": ..., "payload": event}``.
        Events without a directory (such as ``server.connected``) apply to all
        listeners and are kept.
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
        """Read a nested value from ``properties`` (e.g. ``event.prop("status", "type")``)."""
        return dig(self.properties, *keys)

    @property
    def session_id(self) -> str | None:
        """Extract the session ID from whichever field this event type uses."""
        return (
            dig_str(self.properties, "sessionID")
            or dig_str(self.properties, "part", "sessionID")
            or dig_str(self.properties, "info", "sessionID")
        )

    @property
    def request_id(self) -> str | None:
        """Extract the permission or question ID (``id`` when asked, ``requestID`` when settled)."""
        return dig_str(self.properties, "requestID") or dig_str(self.properties, "id")

    @property
    def error_name(self) -> str | None:
        return dig_str(self.properties, "error", "name")

    @property
    def error_message(self) -> str:
        return dig_str(self.properties, "error", "data", "message") or ""
