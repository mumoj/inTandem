"""OpenCode 1.x bus events as ``BackendEvent``.

Every read of an OpenCode event payload by key lives here, so the rest of
inTandem never learns where OpenCode keeps a session id or a tool's status.
"""

from __future__ import annotations

from intandem.backend.events import BackendEvent, EventType, PartUpdate
from intandem.opencode.events import ABORTED, Event
from intandem.opencode.jsonpath import dig, dig_list, dig_str

_TYPES = {member.value: member for member in EventType if member is not EventType.OTHER}
_RUNNING_TOOL_STATES = frozenset({"pending", "running"})
_TOOL_DETAIL_LIMIT = 240


def from_opencode(event: Event) -> BackendEvent:
    """``event`` in neutral terms. An unknown type becomes ``OTHER``, never an error."""
    kind = _TYPES.get(event.type, EventType.OTHER)
    props = event.properties
    out = BackendEvent(
        type=kind,
        native_type=event.type,
        session_id=event.session_id,
        native=event.model_dump(mode="json"),
        raw=event.raw,
    )
    match kind:
        case EventType.SESSION_CREATED:
            out.child_id = dig_str(props, "info", "id")
            out.child_parent_id = dig_str(props, "info", "parentID")
            out.child_policy = dig(props, "info", "permission")
        case EventType.STATUS:
            out.status = dig_str(props, "status", "type")
            if out.status == "retry":
                message = dig_str(props, "status", "message") or ""
                out.retry_detail = f"attempt {event.prop('status', 'attempt')}: {message[:200]}"
        case EventType.ERROR:
            out.error_name = event.error_name
            out.error_message = event.error_message
            out.aborted = event.error_name == ABORTED
        case EventType.PART_UPDATED:
            out.part = _part(event.prop("part"))
        case EventType.PART_DELTA:
            out.delta_field = dig_str(props, "field")
        case EventType.PERMISSION_ASKED:
            out.request_id = event.request_id
            out.permission = dig_str(props, "permission")
            out.patterns = [str(p) for p in dig_list(props, "patterns")]
        case EventType.QUESTION_ASKED:
            out.request_id = event.request_id
            first = next(iter(dig_list(props, "questions")), None)
            out.question = dig_str(first, "question") or dig_str(first, "header") or ""
        case (
            EventType.PERMISSION_REPLIED | EventType.QUESTION_REPLIED | EventType.QUESTION_REJECTED
        ):
            out.request_id = event.request_id
        case _:
            pass
    return out


def _part(part: object) -> PartUpdate | None:
    kind = dig_str(part, "type")
    if kind is None:
        return None
    if kind != "tool":
        return PartUpdate(kind=kind)
    status = dig_str(part, "state", "status")
    return PartUpdate(
        kind=kind,
        call_id=dig_str(part, "callID") or dig_str(part, "id") or "",
        tool=dig_str(part, "tool") or "tool",
        running=status in _RUNNING_TOOL_STATES,
        detail=_tool_detail(part),
    )


def _tool_detail(part: object) -> str:
    """A bounded summary of a tool call, for stall diagnosis."""
    tool = dig_str(part, "tool") or "tool"
    status = dig_str(part, "state", "status") or "unknown"
    command = dig_str(part, "state", "input", "command")
    title = dig_str(part, "state", "title")
    summary = command or title
    if not summary:
        return f"{tool} ({status})"
    summary = " ".join(summary.split())
    if len(summary) > _TOOL_DETAIL_LIMIT:
        summary = summary[: _TOOL_DETAIL_LIMIT - 1] + "…"
    return f"{tool} ({status}): {summary}"
