"""Opt-in end-to-end smoke test for ``AntigravityBackend`` with a real ``agy acp`` CLI.

Set ``INTANDEM_E2E_ANTIGRAVITY=1`` with a signed-in ``agy`` on ``PATH`` to run;
skipped by default.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from intandem.antigravity import AntigravityBackend
from intandem.backend.events import EventType
from intandem.backend.guard import Guard

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("INTANDEM_E2E_ANTIGRAVITY") != "1" or shutil.which("agy") is None,
        reason="needs INTANDEM_E2E_ANTIGRAVITY=1 and a signed-in `agy` on PATH",
    ),
]

PROMPT = (
    "Create a file named hello.txt in the current directory containing the single "
    "line: hi. Do nothing else, and do not ask questions; just stop when it is written."
)


async def test_a_real_turn_writes_a_file_and_reports_tokens(task_dir: Path) -> None:
    async with AntigravityBackend(task_dir) as backend:
        assert (await backend.health()).healthy
        session = await backend.create_session("e2e", Guard(protected=("instruction.md",)))
        stream = backend.events()
        await backend.prompt(session.id, PROMPT, None)
        async with asyncio.timeout(300):
            async for event in stream:
                if event.type is EventType.PERMISSION_ASKED and event.request_id:
                    await backend.reply_permission(event.request_id, "reject")
                elif event.type in (EventType.IDLE, EventType.ERROR):
                    assert event.type is EventType.IDLE, event.error_message
                    break
        info = await backend.get_session(session.id)
    assert (task_dir / "hello.txt").read_text().strip() == "hi"
    assert info.tokens is not None
    assert info.tokens.input > 0
    assert info.tokens.output > 0
