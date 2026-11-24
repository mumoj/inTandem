"""Shared FastAPI server and OpenCode host for running supervised tasks.

All tasks run through a ``Hub``: a single FastAPI app with each task's bridge
mounted at ``/t/<task>``, served by uvicorn alongside one shared OpenCode
server. ``inTandem mcp`` runs a hub for the duration of the MCP session, while
``inTandem serve`` runs one for a single manually started task.

Each task writes a manifest pointing to its mounted URL so the CLI, MCP tools,
and web dashboard locate tasks uniformly regardless of which hub owns them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import socket
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from types import FrameType
from typing import Annotated, Any

import anyio.to_thread
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from intandem.bridge.app import dashboard_headers, require_token
from intandem.bridge.manifest import Manifest
from intandem.config import Settings, TaskPaths
from intandem.opencode.host import SharedHost, reap_orphans
from intandem.opencode.server import resolve_binary
from intandem.runtime import open_task

log = logging.getLogger(__name__)


def bind_loopback(port: int) -> socket.socket:
    """Bind a loopback socket up front so the chosen port is known before uvicorn starts."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


class _Embedded(uvicorn.Server):
    """Uvicorn server subclass that leaves signal handling to the caller."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class TaskRoutes:
    """ASGI dispatcher mounted at ``/t/{key}`` that forwards requests to each task's bridge.

    Tasks are added to and removed from ``self.apps`` at runtime so the parent
    router never needs to be mutated while serving.
    """

    def __init__(self) -> None:
        self.apps: dict[str, ASGIApp] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = self.apps.get(scope.get("path_params", {}).get("key", ""))
        if app is None:
            response = JSONResponse({"detail": "no such task, or it has finished"}, 404)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)


class HubTask(BaseModel):
    name: str
    task_dir: str
    dashboard: str


class HubTasks(BaseModel):
    tasks: list[HubTask]


@dataclass
class _Running:
    key: str
    stop: asyncio.Event
    done: asyncio.Task[None]


@dataclass
class Hub:
    """Hosts multiple supervised tasks behind one FastAPI server and shared OpenCode instance."""

    state_root: Path
    new_opencode: Callable[[], SharedHost]
    """Factory that starts the shared OpenCode server on the event loop thread."""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    port: int = 0
    """Listening port (0 picks a free port; updated to the bound port after startup)."""
    app: FastAPI = field(init=False)
    opencode: SharedHost | None = field(default=None, init=False)
    _server: _Embedded | None = field(default=None, init=False)
    _serving: asyncio.Task[None] | None = field(default=None, init=False)
    _tasks: dict[Path, _Running] = field(default_factory=dict[Path, _Running], init=False)
    failures: dict[Path, Exception] = field(default_factory=dict[Path, Exception], init=False)
    """Exceptions recorded for tasks that exited with an error, keyed by task directory."""
    routes: TaskRoutes = field(default_factory=TaskRoutes, init=False)

    def __post_init__(self) -> None:
        self.app = FastAPI(title="inTandem hub", docs_url=None, redoc_url=None, openapi_url=None)
        self.app.state.token = self.token
        self.app.state.hub = self
        self.app.include_router(router)
        self.app.mount("/t/{key}", self.routes)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def dashboard_url(self) -> str:
        return f"{self.url}/ui#token={self.token}"

    async def start(self) -> None:
        """Start the hub HTTP server.

        The shared OpenCode server is started lazily on the first OpenCode task
        so sessions using other harnesses do not require OpenCode.
        """
        sock = bind_loopback(self.port)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="warning", access_log=False, lifespan="off")
        self._server = _Embedded(config)
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._serving.done():
                self._serving.result()  # Re-raise the startup error.
                raise RuntimeError("the hub stopped before it started")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        """Stop all running tasks, then shut down the HTTP server and OpenCode."""
        await asyncio.gather(*(self.stop_task(task_dir) for task_dir in list(self._tasks)))
        if self._server is not None and self._serving is not None:
            self._server.should_exit = True
            await self._serving
        if self.opencode is not None:
            await anyio.to_thread.run_sync(self.opencode.stop)

    def running(self, task_dir: Path) -> bool:
        return task_dir in self._tasks

    async def start_task(
        self,
        task_dir: Path,
        *,
        model: str | None = None,
        protect: list[str] | None = None,
        **options: Any,
    ) -> Manifest:
        """Start supervising ``task_dir`` on this hub, or return its manifest if already running.

        Additional keyword ``options`` are forwarded to ``Settings``.
        """
        paths = TaskPaths.for_task(self.state_root, task_dir)
        if task_dir in self._tasks:
            return Manifest.read(paths.manifest)
        # Omit None values so pydantic-settings can fall back to environment variables
        # like INTANDEM_MODEL and INTANDEM_PROTECTED.
        chosen: dict[str, Any] = {"model": model, "protected": protect, **options}
        settings = Settings(
            task_dir=task_dir,
            state_root=self.state_root,
            **{key: value for key, value in chosen.items() if value is not None},
        )
        if settings.harness == "opencode":
            # Only start or wait for the shared OpenCode server when needed.
            opencode = await self.ready_opencode()
            password = SecretStr(opencode.password)
            update = {"opencode_url": opencode.url, "opencode_password": password}
            settings = settings.model_copy(update=update)
        ready: asyncio.Future[Manifest] = asyncio.get_running_loop().create_future()
        done = asyncio.create_task(self._run(settings, paths, ready))
        try:
            return await ready
        except BaseException:
            done.cancel()
            raise

    async def ready_opencode(self) -> SharedHost:
        """Return the shared OpenCode server, starting or restarting it if needed."""
        if self.opencode is None or (self.opencode.process and not self.opencode.alive):
            self.opencode = self.new_opencode()
        await anyio.to_thread.run_sync(self.opencode.wait_ready, self.opencode.startup_timeout)
        return self.opencode

    async def wait_task(self, task_dir: Path) -> None:
        """Wait until the task for ``task_dir`` exits."""
        running = self._tasks.get(task_dir)
        if running is not None:
            await asyncio.wait({running.done})

    async def stop_task(self, task_dir: Path) -> bool:
        """Stop a running task cleanly. Returns ``False`` if it was not running."""
        running = self._tasks.get(task_dir)
        if running is None:
            return False
        running.stop.set()
        with contextlib.suppress(Exception):
            await running.done
        return True

    def list_tasks(self) -> HubTasks:
        return HubTasks(
            tasks=[
                HubTask(
                    name=task_dir.name,
                    task_dir=str(task_dir),
                    dashboard=f"{self.url}/t/{running.key}/ui#token={self.token}",
                )
                for task_dir, running in sorted(self._tasks.items())
            ]
        )

    async def _run(
        self, settings: Settings, paths: TaskPaths, ready: asyncio.Future[Manifest]
    ) -> None:
        key = paths.root.name
        current = asyncio.current_task()
        assert current is not None
        try:
            async with open_task(settings, token=self.token) as task:
                self.routes.apps[key] = task.app
                self._tasks[settings.task_dir] = _Running(key, task.stop, current)
                try:
                    manifest = task.manifest(
                        host="127.0.0.1", port=self.port, token=self.token, path=f"/t/{key}"
                    )
                    manifest.write(paths.manifest)
                    task.journal.append("bridge_ready", url=manifest.url, hub=self.url)
                    ready.set_result(manifest)
                    await task.run_workers()
                finally:
                    self.routes.apps.pop(key, None)
                    self._tasks.pop(settings.task_dir, None)
                    Manifest.remove(paths.manifest)
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                # Isolate task failures so one crash does not stop other tasks or the hub.
                log.exception("task %s stopped with an error", key)
                self.failures[settings.task_dir] = exc


def get_hub(request: Request) -> Hub:
    hub: Hub = request.app.state.hub
    return hub


router = APIRouter()


@router.get("/tasks", dependencies=[Depends(require_token)])
def read_tasks(hub: Annotated[Hub, Depends(get_hub)]) -> HubTasks:
    return hub.list_tasks()


@router.get("/ui", include_in_schema=False)
def overview_page() -> HTMLResponse:
    """Serve the overview page listing all tasks. Contains no sensitive data itself."""
    page = files("intandem.bridge").joinpath("hub.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers=dashboard_headers())


@contextlib.contextmanager
def stop_on_signals(stop: asyncio.Event) -> Generator[None, None, None]:
    """Set ``stop`` on SIGINT or SIGTERM for the duration of the context.

    Uses ``signal.signal`` rather than the event loop's handler API so any
    previously installed handlers are restored cleanly on exit.
    """
    loop = asyncio.get_running_loop()

    def handler(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(stop.set)

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, original in previous.items():
            signal.signal(sig, original)


async def serve(settings: Settings, *, ready: Callable[[Manifest], None] | None = None) -> None:
    """Run a single task on its own hub until it finishes or receives a signal.

    Uses the same ``Hub`` as ``inTandem mcp`` so manually started tasks behave
    identically. Re-raises the task's exception if it stopped due to an error.
    """
    records = settings.state_root / "hosts"
    reap_orphans(records)

    def new_opencode() -> SharedHost:
        binary = resolve_binary(settings.opencode_bin)
        port: dict[str, Any] = {"port": settings.opencode_port} if settings.opencode_port else {}
        host = SharedHost(binary, log_path=records / f"{os.getpid()}.log", records=records, **port)
        # Runs on the main event loop thread, which lives for the full process lifetime.
        host.start(die_with_parent=True)
        return host

    task_dir = settings.task_dir
    # Pass only explicitly set task options so unset fields still inherit from the environment.
    options = settings.model_dump(
        exclude_unset=True,
        exclude={
            "task_dir",
            "state_root",
            "opencode_bin",
            "opencode_port",
            "opencode_url",
            "opencode_password",
            "bridge_port",
        },
    )
    hub = Hub(settings.state_root, new_opencode, port=settings.bridge_port)
    try:
        await hub.start()
        manifest = await hub.start_task(
            task_dir,
            model=options.pop("model", None),
            protect=options.pop("protected", None),
            **options,
        )
        if ready is not None:
            ready(manifest)
        stop = asyncio.Event()
        with stop_on_signals(stop):
            stopping = asyncio.create_task(stop.wait())
            finished = asyncio.create_task(hub.wait_task(task_dir))
            await asyncio.wait({stopping, finished}, return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
            finished.cancel()
    finally:
        await hub.close()
    if (failure := hub.failures.get(task_dir)) is not None:
        raise failure
