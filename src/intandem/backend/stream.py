"""Appends raw backend event lines to disk while stubbing oversized payloads.

Some backends occasionally emit multi-megabyte events (for example, OpenCode
re-broadcasting a full compaction summary). Oversized lines are replaced with a
compact stub recording the event type and byte size.

Flushes are batched at most once per second unless a caller explicitly requests
an immediate flush on important events, avoiding excessive syscall overhead.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Self

# Scan only the first 512 bytes for "type" or "method" so we can label oversized
# events without parsing the entire payload.
_TYPE_PATTERN = re.compile(r'"(?:type|method)"\s*:\s*"([A-Za-z0-9_./-]+)"')
_TYPE_SCAN = 512


class StreamRecorder:
    """Append raw SSE event lines to disk, replacing oversized lines with a JSON stub."""

    def __init__(self, path: Path, max_line: int, *, flush_every: float = 1.0) -> None:
        self.path = path
        self.max_line = max_line
        self.flush_every = flush_every
        self.dropped = 0
        self.dropped_bytes = 0
        self._sink: IO[str] | None = None
        self._last_flush = 0.0

    def __enter__(self) -> Self:
        # Restrict permissions to 0600 because file contents and command output appear in the log.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._sink = os.fdopen(descriptor, "a", encoding="utf-8")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._sink is not None:
            self._sink.close()
            self._sink = None

    def write(self, data: str, *, flush: bool = False) -> None:
        if self._sink is None:
            raise RuntimeError("StreamRecorder used outside its context")
        size = len(data)
        if 0 < self.max_line < size:
            match = _TYPE_PATTERN.search(data[:_TYPE_SCAN])
            stub = {
                "type": match.group(1) if match else "unknown",
                "inTandemDropped": True,
                "bytes": size,
            }
            data = json.dumps(stub, separators=(",", ":"))
            self.dropped += 1
            self.dropped_bytes += size
        self._sink.write(f"data: {data}\n")
        now = time.monotonic()
        if flush or now - self._last_flush >= self.flush_every:
            self._sink.flush()
            self._last_flush = now
