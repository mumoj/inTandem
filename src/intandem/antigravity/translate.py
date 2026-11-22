"""ACP ``session/update`` notifications as ``BackendEvent`` and transcript messages.

Every read of an ACP payload by key lives here and in ``guard.py``, as
``claude/translate.py`` does for the Agent SDK. The ``Translator`` keeps one
session's state between notifications: each tool call's fields, since a
``tool_call_update`` sends only what changed, and the assistant message the
turn is building, which ``messages()`` serves once ``session/prompt`` returns.
"""

from __future__ import annotations

import json
from typing import Any, cast

from intandem.antigravity.guard import COMMAND_KEYS, PATH_KEYS, is_edit, tool_name
from intandem.backend.events import BackendEvent, EventType, PartUpdate
from intandem.backend.jsonpath import dig, dig_list, dig_str
from intandem.backend.models import CacheUsage, Message, MessageInfo, Part, TokenUsage

PROVIDER = "antigravity"
_TOOL_DETAIL_LIMIT = 240
_RUNNING = frozenset({"pending", "in_progress"})
_DETAIL_KEYS = (*COMMAND_KEYS, *PATH_KEYS, "pattern", "Query", "query", "url", "Url", "description")


def dumps(native: object) -> str:
    return json.dumps(native, separators=(",", ":"), default=str)


def tool_detail(tool: str, status: str, call: object) -> str:
    """A bounded one-line summary of a tool call, for stall diagnosis."""
    summary = next(
        (
            value
            for container in ("rawInput", "args")
            for key in _DETAIL_KEYS
            if (value := dig_str(call, container, key))
        ),
        None,
    )
    if not summary:
        locations = dig_list(call, "locations")
        summary = dig_str(locations[0], "path") if locations else None
    if not summary:
        title = dig_str(call, "title")
        summary = title if title and title != tool else None
    if not summary:
        return f"{tool} ({status})"
    summary = " ".join(summary.split())
    if len(summary) > _TOOL_DETAIL_LIMIT:
        summary = summary[: _TOOL_DETAIL_LIMIT - 1] + "…"
    return f"{tool} ({status}): {summary}"


def token_usage(usage: object) -> TokenUsage:
    """A ``session/prompt`` result's ``usage`` in neutral terms."""

    def number(key: str) -> float:
        value = dig(usage, key)
        return float(value) if isinstance(value, int | float) else 0.0

    return TokenUsage(
        input=number("inputTokens"),
        output=number("outputTokens"),
        reasoning=number("thoughtTokens"),
        cache=CacheUsage(read=number("cachedReadTokens"), write=number("cachedWriteTokens")),
    )


def add_usage(total: TokenUsage, more: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input=total.input + more.input,
        output=total.output + more.output,
        reasoning=total.reasoning + more.reasoning,
        cache=CacheUsage(
            read=total.cache.read + more.cache.read, write=total.cache.write + more.cache.write
        ),
    )


class Translator:
    """One ACP session's updates as neutral events, and its turns as messages."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._calls: dict[str, dict[str, Any]] = {}
        self._tool_parts: dict[str, Part] = {}
        self._parts: list[Part] = []

    def update(self, params: dict[str, Any]) -> list[BackendEvent]:
        """The events one ``session/update`` makes. The first carries it in ``raw``."""
        update = dig(params, "update")
        body = cast(dict[str, Any], update) if isinstance(update, dict) else {}
        kind = dig_str(body, "sessionUpdate") or "unknown"
        events: list[BackendEvent] = []
        match kind:
            case "agent_thought_chunk":
                events = self._chunk(body, "reasoning")
            case "agent_message_chunk":
                events = self._chunk(body, "text")
            case "tool_call" | "tool_call_update":
                events = self._tool(body)
            case _:
                # usage_update, session_info_update, available_commands_update,
                # config_option_update, plan, ...: recorded, nothing to act on.
                pass
        if not events:
            events = [self._event(EventType.OTHER)]
        native_type = f"antigravity.{kind}"
        for index, event in enumerate(events):
            event.native_type = native_type
            event.native = params
            event.raw = (
                dumps(params)
                if index == 0
                else dumps({"type": native_type, "derived": event.type.value})
            )
        return events

    def finish(self, result: object, message_id: str, model: str | None) -> Message:
        """The turn ``session/prompt`` just ended, as one assistant message."""
        usage = dig(result, "usage")
        parts, self._parts = self._parts, []
        self._calls.clear()
        self._tool_parts.clear()
        info = MessageInfo.model_validate(
            {
                "id": message_id,
                "role": "assistant",
                "tokens": None if usage is None else token_usage(usage),
                "model_id": model,
                "provider_id": PROVIDER,
                "stop_reason": dig_str(result, "stopReason"),
            }
        )
        return Message(info=info, parts=parts)

    # ---- updates ------------------------------------------------------------

    def _event(self, kind: EventType, **fields: Any) -> BackendEvent:
        return BackendEvent(type=kind, native_type="", session_id=self.session_id, **fields)

    def _chunk(self, body: dict[str, Any], part_type: str) -> list[BackendEvent]:
        if dig_str(body, "content", "type") != "text":
            return []
        text = dig_str(body, "content", "text") or ""
        last = self._parts[-1] if self._parts else None
        if last is not None and last.type == part_type:
            last.text = (last.text or "") + text
        else:
            self._parts.append(Part(type=part_type, text=text))
        return [
            self._event(
                EventType.PART_DELTA,
                part=PartUpdate(kind=part_type),
                delta_field="text",
                delta=text,
            )
        ]

    def _tool(self, body: dict[str, Any]) -> list[BackendEvent]:
        call_id = dig_str(body, "toolCallId") or ""
        call = self._calls.setdefault(call_id, {})
        fresh = not call
        call.update({k: v for k, v in body.items() if k != "sessionUpdate" and v is not None})
        tool = tool_name(call)
        status = dig_str(call, "status") or "pending"
        running = status in _RUNNING
        shown = "running" if running else ("error" if status == "failed" else status)
        part = PartUpdate(
            kind="tool",
            call_id=call_id,
            tool=tool,
            running=running,
            detail=tool_detail(tool, shown, call),
        )
        if fresh:
            tool_part = Part(
                type="tool",
                tool=tool,
                call_id=call_id,
                status=shown,
                input=_input(call),
                output=_output(call),
            )
            self._parts.append(tool_part)
            if call_id:
                self._tool_parts[call_id] = tool_part
        elif call_id in self._tool_parts:
            existing = self._tool_parts[call_id]
            existing.status = shown
            if (inp := _input(call)) is not None:
                existing.input = inp
            if (out := _output(call)) is not None:
                existing.output = out
        events = [self._event(EventType.PART_UPDATED, part=part)]
        if status == "completed" and is_edit(call):
            events.append(self._event(EventType.FILE_EDITED))
        if not running:
            self._calls.pop(call_id, None)
        return events


def _input(call: dict[str, Any]) -> object:
    return call.get("rawInput") if call.get("rawInput") is not None else call.get("args")


def _output(call: dict[str, Any]) -> str | None:
    raw: object = call.get("rawOutput")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict | list) and raw:
        return dumps(cast(object, raw))
    return None


def user_message(text: str, message_id: str) -> Message:
    return Message(
        info=MessageInfo(id=message_id, role="user"), parts=[Part(type="text", text=text)]
    )
