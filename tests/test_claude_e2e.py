"""Opt-in end-to-end smoke test for ``ClaudeCodeBackend`` with a real ``claude`` CLI.

Set ``INTANDEM_E2E_CLAUDE=1`` with a logged-in ``claude`` CLI on ``PATH`` to
run; skipped by default.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from intandem.backend.events import EventType
from intandem.backend.guard import Guard
from intandem.claude.backend import ClaudeCodeBackend

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("INTANDEM_E2E_CLAUDE") != "1" or shutil.which("claude") is None,
        reason="needs INTANDEM_E2E_CLAUDE=1 and a logged-in `claude` on PATH",
    ),
]

PROMPT = (
    "Do exactly two things, then stop. 1. Use the Write tool to create hello.txt "
    "containing the single line: hi. 2. Use the Edit tool to append the line 'x' "
    "to instruction.md. If a tool call is refused, do not retry it and do not ask "
    "questions; just stop."
)


async def test_a_real_turn_writes_a_file_and_the_guard_stops_a_protected_edit(
    task_dir: Path,
) -> None:
    before = (task_dir / "instruction.md").read_text()
    async with ClaudeCodeBackend(task_dir) as backend:
        assert (await backend.health()).healthy
        session = await backend.create_session("e2e", Guard(protected=("instruction.md",)))
        stream = backend.events()
        await backend.prompt(session.id, PROMPT, None)
        asked: list[list[str]] = []
        async with asyncio.timeout(300):
            async for event in stream:
                if event.type is EventType.PERMISSION_ASKED and event.request_id:
                    asked.append(event.patterns)
                    await backend.reply_permission(event.request_id, "reject")
                elif event.type in (EventType.IDLE, EventType.ERROR):
                    assert event.type is EventType.IDLE, event.error_message
                    break
        info = await backend.get_session(session.id)
    assert (task_dir / "hello.txt").read_text().strip() == "hi"
    assert ["instruction.md"] in asked
    assert (task_dir / "instruction.md").read_text() == before
    assert info.tokens is not None
    assert info.tokens.output > 0
