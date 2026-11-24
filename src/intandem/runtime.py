"""Runtime wiring for a single supervised task.

Tasks do not run their own HTTP servers. ``open_task`` constructs a task's
supervisor, bridge FastAPI app, and harness backend (the shared OpenCode
1.x/2.x server, Claude Code, or Antigravity ACP), which the hub mounts under
its own server. Background workers run on the hub's event loop inside one
``TaskGroup``:

* the event pump (forwards backend events to the supervisor),
* the monitor (periodically checks the result file, protected files, and stalls),
* the host watcher (stops the task if the backend stops responding).

If any worker raises an unexpected error, the task group cancels the others and
the exit stack closes the backend and releases the task lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from pydantic import SecretStr

from intandem.backend.errors import BackendError
from intandem.backend.models import ModelRef
from intandem.backend.protocol import ImplementerBackend
from intandem.backend.stream import StreamRecorder
from intandem.bridge.app import OpenCodeLogin, create_app
from intandem.bridge.manifest import Manifest
from intandem.claude.sdk import api_key_warning
from intandem.config import Settings, TaskPaths
from intandem.notify import Notifier
from intandem.opencode.client import OpenCodeClient, OpenCodeError
from intandem.opencode.server import Version, aprobe_version, require_supported
from intandem.opencode2.client import OpenCode2Client
from intandem.supervisor.activity import ActivityTracker
from intandem.supervisor.journal import Journal
from intandem.supervisor.protected import ProtectedFiles
from intandem.supervisor.state import StateStore, SupervisorLock
from intandem.supervisor.supervisor import Supervisor

log = logging.getLogger(__name__)

# Flush the stream log immediately on these events; batch flushes for others.
_FLUSH_ON = frozenset(
    {"permission.asked", "question.asked", "session.error", "session.idle", "server.connected"}
)


async def pump_events(
    client: ImplementerBackend,
    supervisor: Supervisor,
    recorder: StreamRecorder,
    *,
    max_delay: float = 30.0,
) -> None:
    """Forward backend events to the supervisor, reconnecting with exponential backoff."""
    delay = 1.0
    while True:
        try:
            async for event in client.events():
                delay = 1.0
                recorder.write(event.raw, flush=event.type in _FLUSH_ON)
                try:
                    await supervisor.handle_event(event)
                except Exception:
                    # Keep reading the stream even if handling a single event fails,
                    # otherwise subsequent events and failure reports would stall.
                    log.exception("event handler failed on %s", event.type)
            supervisor.journal.append("stream_closed")
        except BackendError as exc:
            supervisor.journal.append("stream_failed", error=str(exc), retry_in=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


OpenCodeBackend = OpenCodeClient | OpenCode2Client
"""Client type for whichever OpenCode major version the shared server is running."""


async def _connect(
    stack: contextlib.AsyncExitStack, settings: Settings
) -> tuple[OpenCodeLogin, OpenCodeBackend, Version]:
    """Connect to the shared OpenCode server for this task's directory.

    Probes the server's reported version to pick the 1.x or 2.x client so
    upgrading OpenCode between runs works without changing configuration.
    """
    if not (settings.opencode_url and settings.opencode_password):
        raise ValueError("a task needs the shared OpenCode server: opencode_url and its password")
    password = settings.opencode_password.get_secret_value()
    async with httpx.AsyncClient(auth=("opencode", password), timeout=10.0) as probe:
        try:
            version = require_supported(await aprobe_version(probe, settings.opencode_url))
        except httpx.HTTPError as exc:
            raise OpenCodeError("GET", settings.opencode_url, None, str(exc)) from exc
    directory = str(settings.task_dir)
    client: OpenCodeBackend
    if version.major == 2:
        client = await stack.enter_async_context(
            OpenCode2Client(settings.opencode_url, password, directory=directory)
        )
    else:
        client = await stack.enter_async_context(
            OpenCodeClient(settings.opencode_url, password, directory=directory)
        )
    # Dispose only this task's instance on exit; the hub owns the shared server.
    stack.push_async_callback(_dispose, client)
    return OpenCodeLogin(settings.opencode_url, password), client, version


async def make_backend(
    stack: contextlib.AsyncExitStack, settings: Settings
) -> tuple[ImplementerBackend, OpenCodeLogin | None, Version | None]:
    """Create the implementer backend configured by ``settings.harness``.

    Returns the backend along with the OpenCode login and version when using
    OpenCode, or ``None`` for harnesses that run their own CLI directly
    (``claude`` runs a ``claude`` process per session; ``antigravity`` runs one
    ``agy acp`` process per task).
    """
    if settings.harness == "claude":
        # Lazy import because the claude extra is optional.
        from intandem.claude.backend import ClaudeCodeBackend

        backend = ClaudeCodeBackend(settings.task_dir, cli=settings.claude_bin)
        await stack.enter_async_context(backend)
        await backend.health()  # Fail at startup if the CLI is missing.
        return backend, None, None
    if settings.harness == "antigravity":
        from intandem.antigravity.backend import AntigravityBackend

        antigravity = AntigravityBackend(settings.task_dir, cli=settings.antigravity_bin)
        await stack.enter_async_context(antigravity)
        await antigravity.health()  # Starts `agy acp` so a missing or broken CLI fails early.
        return antigravity, None, None
    login, client, version = await _connect(stack, settings)
    return client, login, version


async def _dispose(client: OpenCodeBackend) -> None:
    with contextlib.suppress(OpenCodeError):
        await client.dispose()


async def watch_host(
    client: ImplementerBackend, journal: Journal, *, interval: float = 5.0, misses: int = 3
) -> None:
    """Exit after consecutive failed health checks so the task shuts down cleanly.

    Stopping cleanly allows the supervisor to resume from its state file the
    next time the task is started.
    """
    missed = 0
    while missed < misses:
        await asyncio.sleep(interval)
        try:
            await client.health()
            missed = 0
        except BackendError:
            missed += 1
    journal.append("host_lost", missed=missed)


@dataclass
class Task:
    """Holds a supervised task's supervisor, workers, and FastAPI bridge app."""

    settings: Settings
    paths: TaskPaths
    client: ImplementerBackend
    supervisor: Supervisor
    recorder: StreamRecorder
    app: FastAPI
    login: OpenCodeLogin | None
    """``None`` for harnesses that do not use an OpenCode server."""
    version: Version | None
    stop: asyncio.Event

    @property
    def journal(self) -> Journal:
        return self.supervisor.journal

    def manifest(self, *, host: str, port: int, token: str, path: str = "") -> Manifest:
        return Manifest(
            host=host,
            port=port,
            path=path,
            token=SecretStr(token),
            task_dir=str(self.settings.task_dir),
            state_dir=str(self.paths.root),
            opencode_port=httpx.URL(self.login.url).port if self.login else None,
            opencode_version=str(self.version) if self.version else None,
            opencode_password=SecretStr(self.login.password) if self.login else None,
        )

    async def run_workers(self) -> None:
        """Run the event pump, monitor, and host watcher until ``stop`` is set.

        If any worker fails with an exception, the task group cancels the
        remaining workers and propagates the error.
        """
        try:
            async with asyncio.TaskGroup() as group:
                workers = [
                    group.create_task(pump_events(self.client, self.supervisor, self.recorder)),
                    group.create_task(self.supervisor.monitor(self.settings.poll_interval)),
                    group.create_task(watch_host(self.client, self.journal)),
                ]
                stopping = group.create_task(self.stop.wait())
                await asyncio.wait({stopping, *workers}, return_when=asyncio.FIRST_COMPLETED)
                for task in (stopping, *workers):
                    task.cancel()
        finally:
            # Run outside the TaskGroup because a worker crash cancels the group body.
            await self.supervisor.begin_shutdown()


@contextlib.asynccontextmanager
async def open_task(settings: Settings, *, token: str) -> AsyncGenerator[Task]:
    """Initialize a task's backend, supervisor, and bridge app, cleaning up on exit."""
    paths = TaskPaths.for_task(settings.state_root, settings.task_dir)
    paths.ensure()
    with SupervisorLock(paths.lock):
        async with contextlib.AsyncExitStack() as stack:
            client, login, version = await make_backend(stack, settings)

            journal = Journal(paths.journal)
            if settings.harness == "claude" and (warning := api_key_warning()):
                journal.append("billing_warning", detail=warning)
            notifier = Notifier(
                log_path=paths.notifications,
                task=settings.task_dir.name,
                state_dir=paths.root,
                command=settings.notify_cmd,
                ntfy_topic=settings.ntfy_topic,
                ntfy_server=settings.ntfy_server,
                timeout=settings.notify_timeout,
            )
            stack.push_async_callback(notifier.drain)
            supervisor = Supervisor(
                api=client,
                paths=paths,
                task_dir=settings.task_dir,
                store=StateStore(paths.state, settings.task_dir),
                journal=journal,
                notifier=notifier,
                tracker=ActivityTracker(
                    stall_after=settings.stall_after,
                    quiet_after=settings.quiet_after,
                    text_recent=settings.text_recent,
                ),
                protected=ProtectedFiles(settings.task_dir, tuple(settings.protected)),
                model=ModelRef.parse(settings.model) if settings.model else None,
            )
            await supervisor.start()
            stack.push_async_callback(supervisor.shutdown)
            recorder = stack.enter_context(StreamRecorder(paths.events, settings.max_stream_line))
            stop = asyncio.Event()
            app = create_app(supervisor=supervisor, token=token, on_finish=stop.set, opencode=login)
            yield Task(settings, paths, client, supervisor, recorder, app, login, version, stop)
