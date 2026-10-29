"""One supervised task: build every part, run its workers, stop them together.

A task has no server of its own. ``open_task`` builds its supervisor and bridge
app, attached to the hub's shared OpenCode, and the hub mounts the app beside
the others. Its workers run in one process, on the hub's event loop, under one
``TaskGroup``:

* the event pump (OpenCode's stream into the supervisor),
* the monitor (result file, protected files, stall checks on a timer), and
* the host watch (stops the task once the shared OpenCode stops answering).

If any of them dies unexpectedly the group cancels the rest, and the exit
stack below frees the task's OpenCode instance and releases the lock on the way
out.
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

from tandem.bridge.app import OpenCodeLogin, create_app
from tandem.bridge.manifest import Manifest
from tandem.config import Settings, TaskPaths
from tandem.notify import Notifier
from tandem.opencode.client import OpenCodeClient, OpenCodeError
from tandem.opencode.events import StreamRecorder
from tandem.opencode.models import ModelRef
from tandem.opencode.server import Version, require_supported
from tandem.supervisor.activity import ActivityTracker
from tandem.supervisor.journal import Journal
from tandem.supervisor.protected import ProtectedFiles
from tandem.supervisor.state import StateStore, SupervisorLock
from tandem.supervisor.supervisor import Supervisor

log = logging.getLogger(__name__)

# Events worth flushing the recorder for immediately; the rest are batched.
_FLUSH_ON = frozenset(
    {"permission.asked", "question.asked", "session.error", "session.idle", "server.connected"}
)


async def pump_events(
    client: OpenCodeClient,
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
        except OpenCodeError as exc:
            supervisor.journal.append("stream_failed", error=str(exc), retry_in=delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


async def _connect(
    stack: contextlib.AsyncExitStack, settings: Settings
) -> tuple[OpenCodeLogin, OpenCodeClient]:
    """A client for the shared OpenCode server, scoped to this task's directory."""
    if not (settings.opencode_url and settings.opencode_password):
        raise ValueError("a task needs the shared OpenCode server: opencode_url and its password")
    password = settings.opencode_password.get_secret_value()
    client = await stack.enter_async_context(
        OpenCodeClient(settings.opencode_url, password, directory=str(settings.task_dir))
    )
    # Free this task's instance on the way out; the server is not ours to stop.
    stack.push_async_callback(_dispose, client)
    return OpenCodeLogin(settings.opencode_url, password), client


async def _dispose(client: OpenCodeClient) -> None:
    with contextlib.suppress(OpenCodeError):
        await client.dispose()


async def watch_host(
    client: OpenCodeClient, journal: Journal, *, interval: float = 5.0, misses: int = 3
) -> None:
    """Return once the shared server stops answering, which stops this supervisor.

    The shared server belongs to the hub, and goes when it does. Without its
    server a supervisor can only fail, so it stops cleanly instead, and resumes
    from its state file when it is next started.
    """
    missed = 0
    while missed < misses:
        await asyncio.sleep(interval)
        try:
            await client.health()
            missed = 0
        except OpenCodeError:
            missed += 1
    journal.append("host_lost", missed=missed)


@dataclass
class Task:
    """One supervised task: its supervisor, its workers and its bridge app.

    It has no server of its own: the hub mounts its app beside the others.
    """

    settings: Settings
    paths: TaskPaths
    client: OpenCodeClient
    supervisor: Supervisor
    recorder: StreamRecorder
    app: FastAPI
    login: OpenCodeLogin
    version: Version
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
            opencode_port=httpx.URL(self.login.url).port,
            opencode_version=str(self.version),
            opencode_password=SecretStr(self.login.password),
        )

    async def run_workers(self) -> None:
        """Pump events and run the monitor until ``stop``, then begin shutting down.

        A worker that dies takes the others with it: its exception leaves the
        task group, as it should, rather than leaving a supervisor half alive.
        """
        async with asyncio.TaskGroup() as group:
            workers = [
                group.create_task(pump_events(self.client, self.supervisor, self.recorder)),
                group.create_task(self.supervisor.monitor(self.settings.poll_interval)),
                group.create_task(watch_host(self.client, self.journal)),
            ]
            stopping = group.create_task(self.stop.wait())
            await asyncio.wait({stopping, *workers}, return_when=asyncio.FIRST_COMPLETED)
            await self.supervisor.begin_shutdown()
            for task in (stopping, *workers):
                task.cancel()


@contextlib.asynccontextmanager
async def open_task(settings: Settings, *, token: str) -> AsyncGenerator[Task]:
    """Build every part of one task; on exit, stop them and capture evidence."""
    paths = TaskPaths.for_task(settings.state_root, settings.task_dir)
    paths.ensure()
    with SupervisorLock(paths.lock):
        async with contextlib.AsyncExitStack() as stack:
            login, client = await _connect(stack, settings)
            # The host checked this when it started; a replaced host may differ.
            version = require_supported(Version.parse((await client.health()).version))

            journal = Journal(paths.journal)
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
