"""Manages a shared ``opencode serve`` process for a hub or MCP session.

Starting OpenCode takes several seconds and significant memory, so a single
server is shared across all OpenCode tasks in a session (each scoped to its own
directory). It is stopped on shutdown, and on Linux ``PR_SET_PDEATHSIG`` ensures
the kernel terminates it if the parent exits unexpectedly.

This module uses synchronous subprocess and threading primitives because it is
invoked from both MCP worker threads and lifespan hooks.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from intandem.opencode.server import Version, free_port, probe_version, require_supported
from intandem.supervisor.state import write_private

_PR_SET_PDEATHSIG = 1


class HostError(RuntimeError):
    """Raised when the shared OpenCode server fails to start or exits."""


def _die_with_parent() -> None:  # pragma: no cover - runs in the child
    """Ask the Linux kernel to send SIGTERM if the parent thread exits."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)


@dataclass
class SharedHost:
    """Manages a loopback ``opencode serve`` process shared across task directories.

    ``start()`` spawns the process asynchronously and ``wait_ready()`` blocks
    until its health endpoint responds. A 0600 JSON record is written under
    ``records`` so later runs can clean up orphaned servers.
    """

    binary: str
    log_path: Path
    records: Path
    startup_timeout: float = 60.0
    port: int = field(default_factory=free_port)
    password: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    process: subprocess.Popen[bytes] | None = field(default=None, init=False)
    version: Version | None = field(default=None, init=False)
    _ready: threading.Event = field(default_factory=threading.Event, init=False)
    _failure: str | None = field(default=None, init=False)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def record(self) -> Path:
        return self.records / f"{os.getpid()}.json"

    def start(self, *, die_with_parent: bool = True) -> None:
        """Spawn ``opencode serve`` and start polling its health endpoint in a daemon thread.

        ``die_with_parent`` binds child lifetime to the calling thread via
        ``PR_SET_PDEATHSIG``, so it should only be enabled from a long-lived
        thread (such as the main thread).
        """
        self.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # OpenCode 1.x reads OPENCODE_SERVER_PASSWORD; 2.x reads OPENCODE_PASSWORD.
        env = {
            **os.environ,
            "OPENCODE_SERVER_PASSWORD": self.password,
            "OPENCODE_PASSWORD": self.password,
        }
        argv = [self.binary, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)]
        on_fork = _die_with_parent if die_with_parent and sys.platform == "linux" else None
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                argv,
                cwd=self.log_path.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                # prctl is async-signal-safe between fork and exec.
                preexec_fn=on_fork,
            )
        write_private(
            self.record,
            json.dumps({"owner": os.getpid(), "pid": self.process.pid, "url": self.url}) + "\n",
        )
        threading.Thread(target=self._await_health, name="opencode-host", daemon=True).start()

    def _await_health(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        with httpx.Client(auth=("opencode", self.password), timeout=2.0) as http:
            while time.monotonic() < deadline:
                if self.process is not None and self.process.poll() is not None:
                    self._failure = f"opencode serve exited with {self.process.returncode}"
                    break
                with contextlib.suppress(httpx.HTTPError):
                    try:
                        version = probe_version(http, self.url)
                        if version is not None:
                            self.version = require_supported(version)
                    except (RuntimeError, KeyError, ValueError) as exc:
                        self._failure = str(exc)
                        break
                    if self.version is not None:
                        break
                time.sleep(0.25)
            else:
                self._failure = f"opencode serve did not answer within {self.startup_timeout:.0f}s"
        self._ready.set()

    def wait_ready(self, timeout: float) -> None:
        """Block until the server is ready, or raise ``HostError`` on timeout or failure."""
        if not self._ready.wait(timeout):
            raise HostError(f"the shared OpenCode server is still starting; see {self.log_path}")
        if self._failure is not None:
            raise HostError(
                f"the shared OpenCode server failed: {self._failure}; see {self.log_path}"
            )

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            # 1.x flushes per-directory instances on /global/dispose; 2.x has no such route.
            if self.version is None or self.version.major == 1:
                with contextlib.suppress(httpx.HTTPError):
                    httpx.post(
                        f"{self.url}/global/dispose",
                        auth=("opencode", self.password),
                        timeout=timeout / 2,
                    )
            process.terminate()
            try:
                process.wait(timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        with contextlib.suppress(FileNotFoundError):
            self.record.unlink()


def reap_orphans(records: Path) -> list[int]:
    """Terminate any recorded OpenCode servers whose owner process is gone, returning their PIDs."""
    reaped: list[int] = []
    for path in records.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            owner, pid = int(record["owner"]), int(record["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if _alive(owner):
            continue
        if _alive(pid) and _is_opencode(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
                reaped.append(pid)
        path.unlink(missing_ok=True)
    return reaped


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_opencode(pid: int) -> bool:
    """Verify ``pid`` is actually an ``opencode serve`` process in case the PID was recycled."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return sys.platform != "linux"  # Without /proc, fall back to trusting the record.
    return any(part.endswith(b"opencode") for part in cmdline) and b"serve" in cmdline
