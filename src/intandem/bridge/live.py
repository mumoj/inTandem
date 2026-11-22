"""The dashboard's live feed: one long-lived response that pushes what changed.

Polling made the page lag by up to one poll interval and refetch the whole
transcript whether or not it had moved. Here the supervisor's revision
counters say when something changed, and the feed sends only the parts that
did, as server-sent events:

``overview``  the same body as ``GET /overview``, whenever it differs
``journal``   new entries, as ``GET /journal?after=``
``messages``  the last messages, as ``GET /messages``, when the transcript moved
``diff``      as ``GET /diff``, when a file may have changed
``cost``      as ``GET /cost``, after the transcript moved

Bursts are coalesced into frames, and the costlier parts are rate limited, so
a model streaming tokens costs at most one transcript read a second.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

import anyio.to_thread
from pydantic import BaseModel

from intandem.backend.errors import BackendError
from intandem.bridge.schemas import DiffOut, JournalOut, MessageOut, MessagesOut, OverviewOut
from intandem.supervisor.supervisor import Supervisor, SupervisorError

FRAME = 0.1
"""Gather a burst of changes for this long before sending them."""
PING = 15.0
"""Send a comment this often when nothing changes, so proxies keep the line open."""
MESSAGES_EVERY = 1.0
DIFF_EVERY = 2.0
COST_EVERY = 5.0
MESSAGE_LIMIT = 12
JOURNAL_LIMIT = 200


async def overview(supervisor: Supervisor) -> OverviewOut:
    attention = supervisor.attention()
    return OverviewOut(
        status=supervisor.status(),
        attention=attention.reason if attention else None,
        attention_detail=attention.detail if attention else "",
        pending=await supervisor.pending(),
    )


def frame(event: str, body: str) -> str:
    # Pydantic's JSON holds no raw newlines, so one ``data`` line is enough.
    return f"event: {event}\ndata: {body}\n\n"


@dataclass
class _Part:
    """One rate-limited part of the feed, refetched when its revision moves."""

    every: float
    fetch: Callable[[], Awaitable[BaseModel | None]]
    seen: int = -1
    sent_at: float = float("-inf")
    last: str = ""

    def due_in(self, revision: int, now: float) -> float | None:
        """Seconds until this part should be sent, or ``None`` if it is current."""
        if revision == self.seen:
            return None
        return max(0.0, self.sent_at + self.every - now)


@dataclass
class LiveFeed:
    supervisor: Supervisor
    journal_offset: int | None = None
    _overview: str = ""
    _spec: tuple[str | None, str | None] = (None, None)
    _parts: dict[str, _Part] = field(default_factory=dict[str, _Part])

    def __post_init__(self) -> None:
        self._parts = {
            "messages": _Part(MESSAGES_EVERY, self._messages),
            "diff": _Part(DIFF_EVERY, self._diff),
            "cost": _Part(COST_EVERY, self._cost),
        }

    def _revisions(self) -> dict[str, int]:
        sup = self.supervisor
        return {
            "messages": sup.message_revision,
            "diff": sup.file_revision,
            "cost": sup.message_revision,
        }

    def due_in(self) -> float:
        """How long the feed may sleep before a rate-limited part falls due."""
        now = time.monotonic()
        revisions = self._revisions()
        waits = [
            wait
            for name, part in self._parts.items()
            if (wait := part.due_in(revisions[name], now)) is not None
        ]
        return min([PING, *waits])

    async def collect(self) -> list[str]:
        """Frames for everything that changed since the last call."""
        sup = self.supervisor
        frames: list[str] = []
        current = await overview(sup)
        body = current.model_dump_json()
        if body != self._overview:
            self._overview = body
            frames.append(frame("overview", body))
        # A new spec or session: everything about the last one is stale.
        spec = (current.status.spec_id, current.status.session_id)
        if spec != self._spec:
            self._spec = spec
            for part in self._parts.values():
                part.seen, part.sent_at = -1, float("-inf")
        entries, self.journal_offset = await anyio.to_thread.run_sync(
            sup.journal.read_from, self.journal_offset, JOURNAL_LIMIT
        )
        if entries:
            out = JournalOut(entries=entries, next=self.journal_offset)
            frames.append(frame("journal", out.model_dump_json()))
        now = time.monotonic()
        revisions = self._revisions()
        for name, part in self._parts.items():
            wait = part.due_in(revisions[name], now)
            if wait is None or wait > 0:
                continue
            part.seen, part.sent_at = revisions[name], now
            try:
                model = await part.fetch()
            except (BackendError, SupervisorError):
                continue  # No spec yet, or the harness hiccupped: try on the next change.
            if model is None:
                continue
            body = model.model_dump_json()
            if body != part.last:
                part.last = body
                frames.append(frame(name, body))
        return frames

    async def _messages(self) -> BaseModel | None:
        sup = self.supervisor
        record = sup.state.current
        if record is None:
            return None
        messages = await sup.messages(MESSAGE_LIMIT)
        return MessagesOut(
            session_id=record.session_id, messages=[MessageOut.of(m) for m in messages]
        )

    async def _diff(self) -> BaseModel | None:
        if self.supervisor.state.current is None:
            return None
        return DiffOut(files=await self.supervisor.diff())

    async def _cost(self) -> BaseModel | None:
        return await self.supervisor.cost()


async def stream(
    supervisor: Supervisor,
    disconnected: Callable[[], Awaitable[bool]],
    journal_offset: int | None = None,
) -> AsyncIterator[str]:
    feed = LiveFeed(supervisor, journal_offset)
    revision = -1
    yield ": live\n\n"
    quiet_since = time.monotonic()
    while not supervisor.closing:
        frames = await feed.collect()
        for chunk in frames:
            yield chunk
        now = time.monotonic()
        if frames:
            quiet_since = now
        elif now - quiet_since >= PING:
            quiet_since = now
            yield ": ping\n\n"
        if await disconnected():
            return
        revision = await supervisor.changed(revision, feed.due_in())
        await asyncio.sleep(FRAME)
