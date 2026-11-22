"""Tell a thinking implementer from a stuck one.

OpenCode reports one status, ``busy``, for a turn in very different states. The
mix of events arriving is the only thing that separates them:

=================  =========================================================
working            tool calls arriving, or a tool still running
reasoning_stall    text keeps arriving but no tool has run for a long time
blocked            a permission or question is waiting on a human
hung               nothing has arrived for a long time
error              the turn ended on a provider or model error
idle               no turn running
=================  =========================================================

The reasoning stall is the one nothing else catches. On one task the
implementer produced about 9,000 characters of reasoning over 25 minutes and
wrote nothing: the spec contradicted itself and it would not guess past the
contradiction. Restarting would have thrown the reasoning away and hit the same
wall. The useful response was to fix the spec, which needs someone to notice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, Field

from intandem.backend.events import ASKED, SETTLED, BackendEvent, EventType, PartUpdate


class Activity(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    REASONING_STALL = "reasoning_stall"
    BLOCKED = "blocked"
    HUNG = "hung"
    ERROR = "error"


_TOOL_DETAIL_COUNT = 5


class ActivityReport(BaseModel):
    """The numbers behind a classification, for a human deciding what to do."""

    activity: Activity
    busy: bool
    seconds_since_tool: int
    seconds_since_text: int
    running_tools: list[str]
    pending: list[str]
    error: str | None
    retry: str | None
    tool_details: list[str] = Field(default_factory=list[str])


@dataclass
class ActivityTracker:
    """Fold one session's events, and its subagents', into an ``Activity``.

    Feed it every event with ``observe``; it ignores events for other sessions.
    A subagent runs in a child session. Its tools, text and permission requests
    count, since a subagent stuck on a prompt is the spec stuck on a prompt, but
    its idle and errors do not end the parent's turn.
    ``classify`` is cheap and side-effect free, so callers can ask as often as
    they like.
    """

    stall_after: float = 720.0
    quiet_after: float = 300.0
    text_recent: float = 120.0
    clock: Callable[[], float] = time.monotonic

    session_id: str | None = None
    busy: bool = False
    error: str | None = None
    retry: str | None = None
    last_tool: float = 0.0
    last_text: float = 0.0
    turn_started_at: float = 0.0
    pending: set[str] = field(default_factory=set[str])
    children: set[str] = field(default_factory=set[str])
    running_tools: dict[str, str] = field(default_factory=dict[str, str])
    running_tool_details: dict[str, str] = field(default_factory=dict[str, str])

    def __post_init__(self) -> None:
        self.last_tool = self.last_text = self.turn_started_at = self.clock()

    def track(self, session_id: str | None) -> None:
        """Start following a different session, forgetting the previous one."""
        self.session_id = session_id
        self.children.clear()
        self.busy = False
        self.error = None
        self.retry = None
        self.pending.clear()
        self.running_tools.clear()
        self.running_tool_details.clear()
        self.last_tool = self.last_text = self.clock()

    def turn_started(self) -> None:
        """A prompt was sent. Busy from now, before the server says so.

        Without this, a ``wait`` issued straight after a prompt would see the
        session as idle and report the turn finished before it began.
        """
        now = self.clock()
        self.busy = True
        self.error = None
        self.retry = None
        self.last_tool = self.last_text = self.turn_started_at = now

    def sync_pending(self, request_ids: set[str]) -> None:
        """Replace the pending set with what the server reports.

        Used after the event stream reconnects, when replies may have been
        missed.
        """
        self.pending = set(request_ids)

    def sync_busy(self, busy: bool, *, grace: float = 10.0) -> None:
        """Adopt the server's view of whether the turn is running.

        A prompt sent moments ago may not have reached the server's status yet,
        so a fresh turn is not ended on the strength of a stale ``idle``.
        """
        if busy and not self.busy:
            self.turn_started()
        elif not busy and self.busy and self.clock() - self.turn_started_at > grace:
            self.turn_ended()

    @property
    def family(self) -> set[str]:
        """The tracked session and every subagent session descended from it."""
        return {self.session_id, *self.children} if self.session_id else set()

    def adopt(self, session_id: str, parent_id: str | None) -> bool:
        """Follow ``session_id`` too if it descends from the tracked session."""
        if parent_id is not None and parent_id in self.family:
            self.children.add(session_id)
            return True
        return False

    def observe(self, event: BackendEvent) -> None:
        if event.type is EventType.SESSION_CREATED and event.child_id:
            self.adopt(event.child_id, event.child_parent_id)
        if self.session_id is None or event.session_id not in self.family:
            return
        now = self.clock()
        if event.session_id == self.session_id:
            self._observe_turn(event)
        if event.type is EventType.PART_UPDATED and event.part is not None:
            self._observe_part(event.part, now)
        elif event.type is EventType.PART_DELTA and event.delta_field == "text":
            self.last_text = now
        elif event.type in ASKED and event.request_id:
            self.pending.add(event.request_id)
        elif event.type in SETTLED and event.request_id:
            self.pending.discard(event.request_id)

    def _observe_turn(self, event: BackendEvent) -> None:
        """Turn boundaries count only on the tracked session, never a subagent's."""
        match event.type:
            case EventType.STATUS:
                if event.status in ("busy", "retry"):
                    if not self.busy:
                        self.turn_started()
                    self.retry = event.retry_detail if event.status == "retry" else None
                elif event.status == "idle":
                    self.turn_ended()
            case EventType.IDLE:
                self.turn_ended()
            case EventType.ERROR:
                if not event.aborted:
                    name = event.error_name or "error"
                    self.error = f"{name}: {event.error_message}"[:300]
                self.turn_ended()
            case _:
                pass

    def _observe_part(self, part: PartUpdate, now: float) -> None:
        if part.kind == "tool":
            self.last_tool = now
            if part.running:
                self.running_tools[part.call_id] = part.tool
                self.running_tool_details[part.call_id] = part.detail
            else:
                self.running_tools.pop(part.call_id, None)
                self.running_tool_details.pop(part.call_id, None)
        elif part.kind in ("text", "reasoning"):
            self.last_text = now

    def turn_ended(self) -> None:
        """The turn finished, failed, or never started."""
        self.busy = False
        self.retry = None
        self.running_tools.clear()
        self.running_tool_details.clear()

    def classify(self) -> Activity:
        if self.pending:
            return Activity.BLOCKED
        if self.error and not self.busy:
            return Activity.ERROR
        if not self.busy:
            return Activity.IDLE
        now = self.clock()
        tool_idle = now - self.last_tool
        text_idle = now - self.last_text
        if self.running_tools:
            # A long test run is work, not a hang. A tool that has produced no
            # update for as long as a reasoning stall takes is probably stuck
            # on input it will never get.
            return Activity.WORKING if tool_idle < self.stall_after else Activity.HUNG
        if text_idle < self.text_recent and tool_idle > self.stall_after:
            return Activity.REASONING_STALL
        if text_idle > self.quiet_after and tool_idle > self.quiet_after:
            return Activity.HUNG
        return Activity.WORKING

    def describe(self) -> ActivityReport:
        now = self.clock()
        return ActivityReport(
            activity=self.classify(),
            busy=self.busy,
            seconds_since_tool=round(now - self.last_tool),
            seconds_since_text=round(now - self.last_text),
            running_tools=sorted(set(self.running_tools.values())),
            tool_details=sorted(set(self.running_tool_details.values()))[:_TOOL_DETAIL_COUNT],
            pending=sorted(self.pending),
            error=self.error,
            retry=self.retry,
        )
