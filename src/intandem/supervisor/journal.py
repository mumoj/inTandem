"""Append-only JSONL audit log of supervisor actions and blocking stream events.

Records one line per state transition and escalation so a task's history can be
inspected without reading full session transcripts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

from intandem.supervisor.state import dump_jsonl

log = logging.getLogger("intandem.journal")


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, action: str, **details: Any) -> None:
        """Append a JSONL entry for ``action``.

        ``time`` and ``action`` are placed last in the dict so keys in
        ``details`` cannot overwrite them.
        """
        record = {**details, "time": round(time.time(), 3), "action": action}
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            # Single write() on an O_APPEND fd avoids interleaved lines across writers.
            os.write(descriptor, dump_jsonl(record).encode())
        finally:
            os.close(descriptor)
        summary = " ".join(f"{k}={v}" for k, v in details.items() if v not in (None, "", []))
        log.info("%s %s", action, summary)

    def read_from(self, offset: int | None, limit: int) -> tuple[list[dict[str, Any]], int]:
        """Read complete lines starting at byte ``offset`` and return ``(entries, next_offset)``.

        When ``offset`` is ``None``, returns the last ``limit`` lines. Incomplete
        trailing lines (without a newline) are deferred to the next read.
        """
        if not self.path.exists():
            return [], 0
        with self.path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            if offset is None or offset > size:
                stream.seek(0)
                lines = deque(stream, maxlen=limit)
                return _parse(lines), size
            stream.seek(offset)
            entries: list[bytes] = []
            position = offset
            for line in stream:
                if not line.endswith(b"\n") or len(entries) >= limit:
                    break
                entries.append(line)
                position += len(line)
            return _parse(entries), position

    def tail(self, count: int) -> list[str]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as stream:
            return [line.rstrip("\n") for line in deque(stream, maxlen=count)]


def _parse(lines: Iterable[bytes]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if isinstance(entry, dict):
                entries.append(cast(dict[str, Any], entry))
    return entries
