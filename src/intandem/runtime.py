"""One supervised task: build every part, run its workers, stop them together.

A task has no server of its own. ``open_task`` builds its supervisor and bridge
app, attached to its harness backend (the hub's shared OpenCode 1.x/2.x server
or a Claude Code backend), and the hub mounts the app beside the others. Its
workers run in one process, on the hub's event loop, under one ``TaskGroup``:

* the event pump (the backend's stream into the supervisor),
* the monitor (result file, protected files, stall checks on a timer), and
* the host watch (stops the task once the backend stops answering).

If any of them dies unexpectedly the group cancels the rest, and the exit
stack below closes the backend and releases the lock on the way out.
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

# Events worth flushing the recorder for immediately; the rest are batched.
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
    """Feed the event stream to the supervisor, reconnecting with backoff, forever."""
    delay = 1.0
    while True:
        try:
            async for event in client.events():
                delay = 1.0
                recorder.write(event.raw, flush=event.type in _FLUSH_ON)
                try:
                    await supervisor.handle_event(event)
                except Exception:
                    # A bug in one handler must not stop the stream being read:
                    # everything downstream, including the next failure report,
                    # would wait behind it.
                    log.exception("event handler failed on %s", event.type)
            supervisor.journal.append("stream_closed")
        except BackendError as exc:
            supervisor.journal.append("stream_failed", error=str(exc), retry_in=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


OpenCodeBackend = OpenCodeClient | OpenCode2Client
"""A client for whichever OpenCode major the shared server runs."""


async def _connect(
    stack: contextlib.AsyncExitStack, settings: Settings
) -> tuple[OpenCodeLogin, OpenCodeBackend, Version]:
    """A client for the shared OpenCode server, scoped to this task's directory.

    The server's own report decides which API to speak, so a host upgraded
    from 1.x to 2.x between two runs is driven correctly by the next one.
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
    # Free this task's instance on the way out; the server is not ours to stop.
    stack.push_async_callback(_dispose, client)
    return OpenCodeLogin(settings.opencode_url, password), client, version


async def make_backend(
    stack: contextlib.AsyncExitStack, settings: Settings
) -> tuple[ImplementerBackend, OpenCodeLogin | None, Version | None]:
    """The task's implementer backend for ``settings.harness``, plus OpenCode's login and version.

    The claude harness runs a ``claude`` CLI per session and needs no shared
    server, so it has no OpenCode login or version to hand the dashboard.
    """
    if settings.harness == "claude":
        # Imported here: the claude extra is optional.
        from intandem.claude.backend import ClaudeCodeBackend

        backend = ClaudeCodeBackend(settings.task_dir, cli=settings.claude_bin)
        await stack.enter_async_context(backend)
        await backend.health()  # No CLI on PATH fails the start, not the first spec.
        return backend, None, None
    login, client, version = await _connect(stack, settings)
    return client, login, version


async def _dispose(client: OpenCodeBackend) -> None:
    with contextlib.suppress(OpenCodeError):
        await client.dispose()


async def watch_host(
    client: ImplementerBackend, journal: Journal, *, interval: float = 5.0, misses: int = 3
) -> None:
    """Return once the backend stops answering, which stops this supervisor.

    Without its backend a supervisor can only fail, so it stops cleanly instead,
    and resumes from its state file when it is next started.
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
    """One supervised task: its supervisor, its workers and its bridge app.

    It has no server of its own: the hub mounts its app beside the others.
    """

    settings: Settings
    paths: TaskPaths
    client: ImplementerBackend
    supervisor: Supervisor
    recorder: StreamRecorder
    app: FastAPI
    login: OpenCodeLogin | None
    """``None`` for a harness with no OpenCode server, such as claude."""
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
        """Pump events and run the monitor until ``stop``, then begin shutting down.

        A worker that dies takes the others with it: its exception leaves the
        task group, as it should, rather than leaving a supervisor half alive.
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
            # Here, not in the group: a crashed worker cancels the group's body.
            await self.supervisor.begin_shutdown()


@contextlib.asynccontextmanager
async def open_task(settings: Settings, *, token: str) -> AsyncGenerator[Task]:
    """Build every part of one task; on exit, stop them and capture evidence."""
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
