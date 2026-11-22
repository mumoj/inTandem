"""Append a raw backend event stream to disk, stubbing oversized lines.

Harnesses sometimes emit multi-megabyte payloads (for instance, OpenCode
re-broadcasting a whole compaction summary on every update). Rather than
bloating the log with lines nothing reads back, the recorder notes that one
arrived and how big it was.

Flushing is batched: per-line flushes cost hundreds of thousands of syscalls on
a busy task. Callers flush explicitly on events worth acting on, and the
recorder flushes on its own at most once a second otherwise.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from types import TracebackType
from typing import IO, Self

# ``"type"`` or ``"method"`` sits near the head of every event, so a bounded
# window is enough to label an oversized line without decoding megabytes of it.
_TYPE_PATTERN = re.compile(r'"(?:type|method)"\s*:\s*"([A-Za-z0-9_./-]+)"')
_TYPE_SCAN = 512


class StreamRecorder:
    """Append raw stream lines to disk, with oversized lines replaced by a stub."""

    def __init__(self, path: Path, max_line: int, *, flush_every: float = 1.0) -> None:
        self.path = path
        self.max_line = max_line
        self.flush_every = flush_every
        self.dropped = 0
        self.dropped_bytes = 0
        self._sink: IO[str] | None = None
        self._last_flush = 0.0

    def __enter__(self) -> Self:
        # 0600: every file the model read and every command's output passes
        # through this stream.
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
