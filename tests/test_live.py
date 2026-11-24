"""Tests for the SSE live dashboard feed (event coalescing, rate limiting, and shutdown)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from intandem.bridge import live
from intandem.bridge.app import create_app
from intandem.supervisor.supervisor import Supervisor
from tests.fakes import event


def _events(frames: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for chunk in frames:
        name, data = chunk.split("\n")[:2]
        out[name.removeprefix("event: ")] = json.loads(data.removeprefix("data: "))
    return out


def _tool(session_id: str, status: str) -> object:
    part = {
        "id": "prt_1",
        "sessionID": session_id,
        "messageID": "msg_1",
        "type": "tool",
        "callID": "call_1",
        "tool": "bash",
        "state": {"status": status},
    }
    return event("message.part.updated", sessionID=session_id, part=part, time=0)


@pytest.fixture(autouse=True)
def no_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MESSAGES_EVERY", "DIFF_EVERY", "COST_EVERY"):
        monkeypatch.setattr(live, name, 0.0)


async def test_the_first_frame_has_everything_then_only_changes(supervisor: Supervisor) -> None:
    feed = live.LiveFeed(supervisor)
    first = _events(await feed.collect())
    assert set(first) >= {"overview", "cost"}  # An empty journal sends nothing.
    assert await feed.collect() == []  # Nothing moved: nothing sent.

    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    sent = _events(await feed.collect())
    assert sent.keys() >= {"overview", "journal", "messages", "diff"}
    overview = sent["overview"]
    assert isinstance(overview, dict)
    assert overview["status"]["spec_id"] == "S-1"

    before = supervisor.message_revision
    await supervisor.handle_event(_tool(record.session_id, "running"))  # type: ignore[arg-type]
    assert supervisor.message_revision > before
    # The transcript was reread, but the fake's is unchanged, so it is not resent.
    assert "messages" not in _events(await feed.collect())


async def test_the_journal_resumes_from_the_offset_the_page_had(supervisor: Supervisor) -> None:
    await supervisor.submit_spec("S-1", "Add a greeting.")
    _, offset = supervisor.journal.read_from(None, 200)
    feed = live.LiveFeed(supervisor, offset)
    assert "journal" not in _events(await feed.collect())
    supervisor.journal.append("note", detail="later")
    entries = _events(await feed.collect())["journal"]
    assert isinstance(entries, dict)
    assert [e["action"] for e in entries["entries"]] == ["note"]


async def test_transcript_reads_are_rate_limited(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "MESSAGES_EVERY", 10.0)
    record = await supervisor.submit_spec("S-1", "Add a greeting.")
    feed = live.LiveFeed(supervisor)
    assert "messages" in _events(await feed.collect())
    await supervisor.handle_event(_tool(record.session_id, "running"))  # type: ignore[arg-type]
    assert "messages" not in _events(await feed.collect())
    assert 9 < feed.due_in() <= 10  # The feed wakes itself when the read falls due.


async def test_changed_wakes_on_a_change_and_times_out_otherwise(supervisor: Supervisor) -> None:
    now = supervisor.revision
    assert await supervisor.changed(now, 0.01) == now
    waiting = asyncio.create_task(supervisor.changed(now, 5))
    await asyncio.sleep(0)
    supervisor.journal.append("note")
    await supervisor.tick()
    assert await asyncio.wait_for(waiting, 1) > now


async def test_the_stream_ends_when_the_supervisor_closes(supervisor: Supervisor) -> None:
    async def connected() -> bool:
        return False

    chunks = live.stream(supervisor, connected)
    assert await anext(chunks) == ": live\n\n"
    assert (await anext(chunks)).startswith("event: overview")

    async def drain() -> list[str]:
        return [chunk async for chunk in chunks]

    draining = asyncio.create_task(drain())
    await asyncio.sleep(0.05)
    await supervisor.begin_shutdown()
    await asyncio.wait_for(draining, 2)


async def test_the_live_route_needs_the_token(supervisor: Supervisor) -> None:
    app = create_app(supervisor=supervisor, token="t0ken")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bridge"
    ) as client:
        assert (await client.get("/live")).status_code == 401


async def test_a_quiet_feed_only_pings_now_and_then(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live, "FRAME", 0.0)

    async def connected() -> bool:
        return False

    chunks = live.stream(supervisor, connected)
    received: list[str] = []

    async def read() -> None:
        async for chunk in chunks:
            received.append(chunk)

    reading = asyncio.create_task(read())
    for _ in range(20):  # Changes that alter nothing the page shows.
        await supervisor.tick()
        await asyncio.sleep(0.01)
    await supervisor.begin_shutdown()
    await asyncio.wait_for(reading, 2)
    assert ": ping\n\n" not in received
