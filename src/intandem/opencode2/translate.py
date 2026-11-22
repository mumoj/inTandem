"""OpenCode 2 bus events as ``BackendEvent``.

2.x streams a tool call as several events, and only the first names the tool,
so a ``Translator`` remembers each running call between them. One translator
per event stream: a reconnect starts a fresh one, and the supervisor resyncs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, cast

from intandem.backend.events import BackendEvent, EventType, PartUpdate
from intandem.opencode.jsonpath import dig, dig_list, dig_str

_TOOL_DETAIL_LIMIT = 240

_TOOL_RUNNING = frozenset(
    {
        "session.tool.input.started",
        "session.tool.input.ended",
        "session.tool.called",
        "session.tool.progress",
    }
)
_TOOL_DONE = frozenset({"session.tool.success", "session.tool.failed"})
_TEXT_DELTA = frozenset({"session.text.delta", "session.reasoning.delta"})
_TEXT_PART = {
    "session.text.started": "text",
    "session.text.ended": "text",
    "session.reasoning.started": "reasoning",
    "session.reasoning.ended": "reasoning",
}


@dataclass
class _Call:
    tool: str = "tool"
    detail: str = ""


@dataclass
class Translator:
    calls: dict[str, _Call] = field(default_factory=dict[str, _Call])

    def parse(self, line: str) -> BackendEvent | None:
        """One SSE ``data`` payload, or ``None`` if it is not an event."""
        try:
            decoded = json.loads(line)
        except ValueError:
            return None
        if not isinstance(decoded, dict):
            return None
        native = cast(dict[str, Any], decoded)
        if not isinstance(native.get("type"), str):
            return None
        return self.translate(native, line)

    def translate(self, native: dict[str, Any], raw: str = "") -> BackendEvent:
        kind = str(native["type"])
        data: object = native.get("data")
        out = BackendEvent(
            type=EventType.OTHER,
            native_type=kind,
            session_id=dig_str(data, "sessionID") or dig_str(data, "form", "sessionID"),
            native=native,
            raw=raw or json.dumps(native, separators=(",", ":")),
        )
        if kind == "server.connected":
            out.type = EventType.CONNECTED
        elif kind == "session.created":
            out.type = EventType.SESSION_CREATED
            out.child_id = dig_str(data, "sessionID")
            out.child_parent_id = dig_str(data, "parentID")
            out.child_policy = dig(data, "permissions")
        elif kind == "session.execution.started":
            out.type, out.status = EventType.STATUS, "busy"
        elif kind == "session.retry.scheduled":
            out.type, out.status = EventType.STATUS, "retry"
            message = dig_str(data, "error", "message") or ""
            out.retry_detail = f"attempt {dig(data, 'attempt')}: {message[:200]}"
        elif kind == "session.execution.succeeded":
            out.type = EventType.IDLE
        elif kind == "session.execution.failed":
            out.type = EventType.ERROR
            out.error_name = dig_str(data, "error", "type") or "error"
            out.error_message = dig_str(data, "error", "message") or ""
        elif kind == "session.execution.interrupted":
            out.type, out.aborted = EventType.ERROR, True
            out.error_name = f"interrupted ({dig_str(data, 'reason') or 'unknown'})"
        elif kind in _TEXT_DELTA:
            out.type, out.delta_field = EventType.PART_DELTA, "text"
        elif kind in _TEXT_PART:
            out.type, out.part = EventType.PART_UPDATED, PartUpdate(kind=_TEXT_PART[kind])
        elif kind in _TOOL_RUNNING or kind in _TOOL_DONE:
            out.type, out.part = EventType.PART_UPDATED, self._tool(kind, data)
        elif kind == "permission.asked":
            out.type = EventType.PERMISSION_ASKED
            out.request_id = dig_str(data, "id")
            out.permission = dig_str(data, "action")
            out.patterns = [str(r) for r in dig_list(data, "resources")]
        elif kind == "permission.replied":
            out.type, out.request_id = EventType.PERMISSION_REPLIED, dig_str(data, "requestID")
        elif kind == "form.created":
            out.type = EventType.QUESTION_ASKED
            out.request_id = dig_str(data, "form", "id")
            first = next(iter(dig_list(data, "form", "fields")), None)
            out.question = dig_str(first, "description") or dig_str(first, "title") or ""
        elif kind == "form.replied":
            out.type, out.request_id = EventType.QUESTION_REPLIED, dig_str(data, "id")
        elif kind == "form.cancelled":
            out.type, out.request_id = EventType.QUESTION_REJECTED, dig_str(data, "id")
        elif kind == "filesystem.changed":
            out.type = EventType.FILE_EDITED
        return out

    def _tool(self, kind: str, data: object) -> PartUpdate:
        call_id = dig_str(data, "id") or ""
        call = self.calls.setdefault(call_id, _Call())
        if name := dig_str(data, "name"):
            call.tool = name
        if isinstance(inputs := dig(data, "input"), dict):
            call.detail = _summary(cast(dict[str, Any], inputs))
        done = kind in _TOOL_DONE
        if done:
            self.calls.pop(call_id, None)
        status = "failed" if kind == "session.tool.failed" else "completed" if done else "running"
        detail = (
            f"{call.tool} ({status}): {call.detail}" if call.detail else f"{call.tool} ({status})"
        )
        return PartUpdate(
            kind="tool", call_id=call_id, tool=call.tool, running=not done, detail=detail
        )


def _summary(inputs: dict[str, Any]) -> str:
    """A bounded one-line summary of a tool call's input: its command, or its path."""
    for key in ("command", "path", "pattern", "url", "description"):
        value = inputs.get(key)
        if isinstance(value, str) and value:
            text = " ".join(value.split())
            if len(text) > _TOOL_DETAIL_LIMIT:
                text = text[: _TOOL_DETAIL_LIMIT - 1] + "…"
            return text
    return ""
