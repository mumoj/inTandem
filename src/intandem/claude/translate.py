"""Claude Agent SDK messages as ``BackendEvent`` and transcript messages.

The SDK hands over typed dataclasses (``AssistantMessage``, ``ResultMessage``,
...). ``to_native`` turns each into a JSON-able dict tagged with the wire
``type`` it came from, and everything after that reads the dict, so this
module never imports the SDK and its tests need no CLI.

Every read of an SDK message by key lives here, as ``opencode/translate.py``
does for OpenCode.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, cast

from intandem.backend.events import BackendEvent, EventType, PartUpdate
from intandem.backend.jsonpath import dig, dig_list, dig_str
from intandem.backend.models import CacheUsage, Message, MessageInfo, Part, TokenUsage

PROVIDER = "anthropic"
_TOOL_DETAIL_LIMIT = 240

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
"""The SDK's file-editing tools: the ones a protected path stops."""

# The SDK's class names, as the type each one is on the wire.
_TAGS = {
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
    "ServerToolUseBlock": "server_tool_use",
    "ServerToolResultBlock": "server_tool_result",
    "UserMessage": "user",
    "AssistantMessage": "assistant",
    "SystemMessage": "system",
    "ResultMessage": "result",
    "StreamEvent": "stream_event",
    "RateLimitEvent": "rate_limit_event",
    "ConversationResetMessage": "conversation_reset",
}


def _tag(value: object) -> str:
    for cls in type(value).__mro__:
        if (tag := _TAGS.get(cls.__name__)) is not None:
            return tag
    return type(value).__name__


def to_native(value: object) -> Any:
    """An SDK message, block or value as plain JSON-able data.

    Each dataclass becomes a dict with its wire ``type`` first. A
    ``SystemMessage`` subclass keeps ``type: system``; its ``subtype`` says which.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {"type": _tag(value)}
        for field in dataclasses.fields(value):
            out[field.name] = to_native(getattr(value, field.name))
        return out
    if isinstance(value, dict):
        return {str(k): to_native(v) for k, v in cast(dict[object, object], value).items()}
    if isinstance(value, list | tuple):
        return [to_native(v) for v in cast(list[object], value)]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def dumps(native: object) -> str:
    return json.dumps(native, separators=(",", ":"), default=str)


def tool_detail(tool: str, status: str, tool_input: object) -> str:
    """A bounded one-line summary of a tool call, for stall diagnosis."""
    summary = next(
        (
            value
            for key in ("command", "file_path", "notebook_path", "pattern", "url", "description")
            if (value := dig_str(tool_input, key))
        ),
        None,
    )
    if not summary:
        return f"{tool} ({status})"
    summary = " ".join(summary.split())
    if len(summary) > _TOOL_DETAIL_LIMIT:
        summary = summary[: _TOOL_DETAIL_LIMIT - 1] + "…"
    return f"{tool} ({status}): {summary}"


def token_usage(usage: object) -> TokenUsage:
    """The Messages API ``usage`` block in neutral terms."""

    def number(key: str) -> float:
        value = dig(usage, key)
        return float(value) if isinstance(value, int | float) else 0.0

    return TokenUsage(
        input=number("input_tokens"),
        output=number("output_tokens"),
        cache=CacheUsage(
            read=number("cache_read_input_tokens"), write=number("cache_creation_input_tokens")
        ),
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
    """One session's SDK messages as neutral events.

    Stateful only to remember each tool call's name and input between its
    ``tool_use`` and its ``tool_result``, which arrive in different messages.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._calls: dict[str, tuple[str, object]] = {}

    def translate(self, native: dict[str, Any], *, aborting: bool = False) -> list[BackendEvent]:
        """The events one message makes. The first carries the message's line in ``raw``."""
        kind = dig_str(native, "type") or "unknown"
        events: list[BackendEvent] = []
        match kind:
            case "assistant":
                events = self._assistant(native)
            case "user":
                events = self._user(native)
            case "stream_event":
                events = self._stream(native)
            case "system":
                events = self._system(native)
            case "result":
                events = self._result(native, aborting=aborting)
            case "rate_limit_event":
                events = self._rate_limit(native)
            case _:
                pass
        if not events:
            events = [self._event(EventType.OTHER)]
        model = dig_str(native, "model") if kind == "assistant" else None
        if kind == "system" and dig_str(native, "subtype") == "init":
            model = dig_str(native, "data", "model") or dig_str(native, "model")
        native_type = f"claude.{kind}"
        for index, event in enumerate(events):
            event.native_type = native_type
            event.native = native
            if model and event.model is None:
                event.model = model
            event.raw = (
                dumps(native)
                if index == 0
                else dumps({"type": native_type, "derived": event.type.value})
            )
        return events

    def _event(self, kind: EventType, **fields: Any) -> BackendEvent:
        return BackendEvent(type=kind, native_type="", session_id=self.session_id, **fields)

    def _assistant(self, native: dict[str, Any]) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        for block in dig_list(native, "content"):
            match dig_str(block, "type"):
                case "text":
                    events.append(self._event(EventType.PART_UPDATED, part=PartUpdate(kind="text")))
                case "thinking":
                    part = PartUpdate(kind="reasoning")
                    events.append(self._event(EventType.PART_UPDATED, part=part))
                case "tool_use" | "server_tool_use":
                    call_id = dig_str(block, "id") or ""
                    tool = dig_str(block, "name") or "tool"
                    tool_input = dig(block, "input")
                    self._calls[call_id] = (tool, tool_input)
                    part = PartUpdate(
                        kind="tool",
                        call_id=call_id,
                        tool=tool,
                        running=True,
                        detail=tool_detail(tool, "running", tool_input),
                    )
                    events.append(self._event(EventType.PART_UPDATED, part=part))
                case _:
                    pass
        return events

    def _user(self, native: dict[str, Any]) -> list[BackendEvent]:
        events: list[BackendEvent] = []
        for block in dig_list(native, "content"):
            if dig_str(block, "type") not in ("tool_result", "server_tool_result"):
                continue
            call_id = dig_str(block, "tool_use_id") or ""
            tool, tool_input = self._calls.pop(call_id, ("tool", None))
            failed = dig(block, "is_error") is True
            status = "error" if failed else "completed"
            part = PartUpdate(
                kind="tool",
                call_id=call_id,
                tool=tool,
                running=False,
                detail=tool_detail(tool, status, tool_input),
            )
            events.append(self._event(EventType.PART_UPDATED, part=part))
            if tool in EDIT_TOOLS and not failed:
                events.append(self._event(EventType.FILE_EDITED))
        return events

    def _stream(self, native: dict[str, Any]) -> list[BackendEvent]:
        event_type = dig_str(native, "event", "type")
        if event_type == "content_block_start":
            block = dig(native, "event", "content_block")
            if dig_str(block, "type") in ("tool_use", "server_tool_use"):
                call_id = dig_str(block, "id") or ""
                tool = dig_str(block, "name") or "tool"
                part = PartUpdate(
                    kind="tool",
                    call_id=call_id,
                    tool=tool,
                    running=True,
                    detail=f"{tool} (preparing)",
                )
                return [self._event(EventType.PART_UPDATED, part=part)]
            return []
        if event_type != "content_block_delta":
            return []
        delta_type = dig_str(native, "event", "delta", "type")
        if delta_type in ("text_delta", "thinking_delta", "signature_delta"):
            return [self._event(EventType.PART_DELTA, delta_field="text")]
        if delta_type == "input_json_delta":
            return [self._event(EventType.PART_DELTA, delta_field="tool")]
        return []

    def _system(self, native: dict[str, Any]) -> list[BackendEvent]:
        if dig_str(native, "subtype") == "thinking_tokens":
            return [self._event(EventType.PART_DELTA, delta_field="text")]
        return []

    def _result(self, native: dict[str, Any], *, aborting: bool) -> list[BackendEvent]:
        idle = self._event(EventType.STATUS, status="idle")
        self._calls.clear()
        terminal = dig_str(native, "terminal_reason") or ""
        if aborting or terminal.startswith("aborted"):
            error = self._event(
                EventType.ERROR,
                error_name="MessageAbortedError",
                error_message="the turn was interrupted",
                aborted=True,
            )
            return [idle, error]
        if dig(native, "is_error") is True:
            errors = [str(e) for e in dig_list(native, "errors")]
            message = "; ".join(errors) or dig_str(native, "result") or ""
            status = dig(native, "api_error_status")
            if status is not None:
                message = f"API error (HTTP {status}): {message}".rstrip(": ")
            name = dig_str(native, "subtype") or "error"
            error = self._event(EventType.ERROR, error_name=name, error_message=message[:2000])
            return [idle, error]
        return [idle, self._event(EventType.IDLE)]

    def _rate_limit(self, native: dict[str, Any]) -> list[BackendEvent]:
        if dig_str(native, "rate_limit_info", "status") != "rejected":
            return []
        resets = dig(native, "rate_limit_info", "resets_at")
        detail = f"rate limited, resets at {resets}" if resets is not None else "rate limited"
        return [self._event(EventType.STATUS, status="retry", retry_detail=detail)]


def assistant_message(native: dict[str, Any], message_id: str) -> Message:
    """One ``AssistantMessage`` as a transcript entry."""
    parts: list[Part] = []
    for block in dig_list(native, "content"):
        match dig_str(block, "type"):
            case "text":
                parts.append(Part(type="text", text=dig_str(block, "text") or ""))
            case "thinking":
                parts.append(Part(type="reasoning", text=dig_str(block, "thinking") or ""))
            case "tool_use" | "server_tool_use":
                tool_input = dig(block, "input")
                parts.append(
                    Part.model_validate(
                        {
                            "type": "tool",
                            "tool": dig_str(block, "name") or "tool",
                            "call_id": dig_str(block, "id") or "",
                            "input": tool_input,
                        }
                    )
                )
            case _:
                pass
    usage = dig(native, "usage")
    info = MessageInfo(
        id=message_id,
        role="assistant",
        tokens=None if usage is None else token_usage(usage),
        model_id=dig_str(native, "model"),
        provider_id=PROVIDER,
    )
    return Message(info=info, parts=parts)


def user_message(text: str, message_id: str) -> Message:
    return Message(
        info=MessageInfo(id=message_id, role="user"), parts=[Part(type="text", text=text)]
    )
