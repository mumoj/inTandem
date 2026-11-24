"""Alert logging and asynchronous delivery for events that need user attention.

Only events that block progress (permission prompts, questions, stalls,
protected-file violations, and unexpected errors) are escalated. Normal turn
completions are handled by ``wait``, and abort errors caused by retiring a
session are ignored to avoid alert noise.

All alerts are appended to ``notifications.log``. External delivery (shell
command or ntfy) runs in background tasks so slow notifiers never block event
stream processing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from intandem.backend.events import BackendEvent, EventType

log = logging.getLogger(__name__)

MAX_EVENT_BYTES = 64 * 1024
"""Payload limit to stay well under Linux's 128 KiB per-env-var limit."""


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str
    summary: str
    session_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict[str, Any])

    @classmethod
    def from_event(cls, event: BackendEvent) -> Alert | None:
        """Convert a backend event into an ``Alert``, or ``None`` if it should not be escalated."""
        match event.type:
            case EventType.PERMISSION_ASKED:
                patterns = ", ".join(event.patterns)
                summary = (
                    f"{event.request_id} wants {event.permission} on {patterns[:120]}; "
                    "answer with `inTandem allow` or `inTandem deny`"
                )
            case EventType.QUESTION_ASKED:
                asked = event.question[:160]
                summary = f"{event.request_id} asks: {asked}; answer with `inTandem answer`"
            case EventType.ERROR:
                if event.aborted:
                    return None
                summary = f"{event.error_name or 'error'}: {event.error_message[:300]}"
            case _:
                return None
        return cls(event.type.value, summary, event.session_id, event.native)


def _event_json(alert: Alert) -> str:
    """Serialize ``alert.payload`` for ``$INTANDEM_EVENT``, stubbing oversized payloads.

    Returning a small JSON stub instead of slicing the string guarantees the
    notification command always receives valid JSON.
    """
    encoded = json.dumps(alert.payload)  # ASCII by default, so len(encoded) == byte length.
    if len(encoded) <= MAX_EVENT_BYTES:
        return encoded
    return json.dumps(
        {
            "kind": alert.kind,
            "session_id": alert.session_id,
            "truncated": True,
            "size": len(encoded),
        }
    )


class Notifier:
    def __init__(
        self,
        *,
        log_path: Path,
        task: str,
        state_dir: Path,
        command: str | None = None,
        ntfy_topic: str | None = None,
        ntfy_server: str = "https://ntfy.sh",
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.log_path = log_path
        self.task = task
        self.state_dir = state_dir
        self.command = command
        self.ntfy_topic = ntfy_topic
        self.ntfy_url = f"{ntfy_server.rstrip('/')}/{ntfy_topic}" if ntfy_topic else None
        self.timeout = timeout
        self._http = http
        # Keep strong references to background tasks so asyncio does not GC them mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()

    def escalate(self, alert: Alert) -> None:
        self._record(alert)
        if self.command:
            self._spawn(self._run_command(alert))
        if self.ntfy_url:
            self._spawn(self._publish(alert))

    def _record(self, alert: Alert) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        line = f"{stamp}\t{self.task}\t{alert.kind}\t{alert.summary}\n"
        descriptor = os.open(self.log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, line.encode())
        finally:
            os.close(descriptor)
        log.warning("alert %s %s", alert.kind, alert.summary)

    def _spawn(self, coroutine: Any) -> None:
        task: asyncio.Task[None] = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_command(self, alert: Alert) -> None:
        assert self.command is not None
        env = {
            **os.environ,
            "INTANDEM_NOTIFY": alert.summary,
            "INTANDEM_KIND": alert.kind,
            "INTANDEM_EVENT": _event_json(alert),
            "INTANDEM_TASK": self.task,
            "INTANDEM_STATE_DIR": str(self.state_dir),
        }
        try:
            process = await asyncio.create_subprocess_shell(
                self.command,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.error("notify command failed to start: %s", exc)
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=self.timeout)
        except TimeoutError:
            # Kill hung notification commands so they do not accumulate.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            log.error("notify command timed out after %ss", self.timeout)
        else:
            if process.returncode:
                log.error("notify command exited %s", process.returncode)

    async def _publish(self, alert: Alert) -> None:
        assert self.ntfy_url is not None
        http = self._http or httpx.AsyncClient(timeout=self.timeout)
        try:
            response = await http.post(
                self.ntfy_url,
                content=alert.summary.encode(),
                # Pass header values as bytes because httpx encodes str headers as ASCII,
                # while ntfy expects UTF-8.
                headers={b"Title": f"inTandem {self.task}: {alert.kind}".encode()},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("ntfy publish failed: %s", exc)
        finally:
            if self._http is None:
                await http.aclose()

    async def drain(self) -> None:
        """Wait for in-flight alert deliveries to finish during shutdown."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=self.timeout)
