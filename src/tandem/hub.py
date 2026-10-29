"""Every supervised task, behind one server and one OpenCode.

The hub is the only way tandem runs a task: one FastAPI app with each task's
bridge mounted at ``/t/<task>``, one uvicorn, and one shared OpenCode server.
``tandem mcp`` runs one for the planner's session; ``tandem serve`` runs one of
its own, for a task started by hand.

A task's manifest points at its mount, so the CLI, the MCP tools and the
dashboard find every task the same way, whichever hub it runs on.
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

from tandem.bridge.app import dashboard_headers, require_token
from tandem.bridge.manifest import Manifest
from tandem.config import Settings, TaskPaths
from tandem.opencode.host import SharedHost, reap_orphans
from tandem.opencode.server import resolve_binary
from tandem.runtime import open_task

log = logging.getLogger(__name__)


def bind_loopback(port: int) -> socket.socket:
    """Bind the hub's socket ourselves so its port is known before serving."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    return sock


class _Embedded(uvicorn.Server):
    """uvicorn without signal handlers of its own: the process's owner handles them."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class TaskRoutes:
    """The ASGI app at ``/t/{key}``: hands each request to that task's bridge.

    Mounted once. Tasks come and go in a dict, so the router itself never
    changes while it is serving.
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
    """One FastAPI server and one OpenCode server for many supervised tasks."""

    state_root: Path
    new_opencode: Callable[[], SharedHost]
    """Starts the shared OpenCode server. Called on the event loop's thread."""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    port: int = 0
    """Where to listen; 0 picks a free port. The bound port once started."""
    app: FastAPI = field(init=False)
    opencode: SharedHost | None = field(default=None, init=False)
    _server: _Embedded | None = field(default=None, init=False)
    _serving: asyncio.Task[None] | None = field(default=None, init=False)
    _tasks: dict[Path, _Running] = field(default_factory=dict[Path, _Running], init=False)
    failures: dict[Path, Exception] = field(default_factory=dict[Path, Exception], init=False)
    """Why each task that stopped with an error stopped, by task directory."""
    routes: TaskRoutes = field(default_factory=TaskRoutes, init=False)

    def __post_init__(self) -> None:
        self.app = FastAPI(title="tandem hub", docs_url=None, redoc_url=None, openapi_url=None)
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
        """Start the shared OpenCode server in the background, then serve at once.

        OpenCode takes seconds to start; the first task waits for it, so the
        planner's connection is not held up.
        """
        self.opencode = self.new_opencode()
        sock = bind_loopback(self.port)
        self.port = sock.getsockname()[1]
        config = uvicorn.Config(self.app, log_level="warning", access_log=False, lifespan="off")
        self._server = _Embedded(config)
        self._serving = asyncio.create_task(self._server.serve(sockets=[sock]))
        while not self._server.started:
            if self._serving.done():
                self._serving.result()  # re-raise whatever stopped it
                raise RuntimeError("the hub stopped before it started")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        """Stop every task, capturing its evidence, then the server."""
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
        """Supervise ``task_dir``, mounted on this server. Idempotent.

        ``options`` are further ``Settings`` fields, such as ``notify_cmd``.
        """
        paths = TaskPaths.for_task(self.state_root, task_dir)
        if task_dir in self._tasks:
            return Manifest.read(paths.manifest)
        opencode = await self._ready_opencode()
        # Only what was asked for: an explicit None would hide TANDEM_MODEL and
        # TANDEM_PROTECTED, since pydantic-settings prefers arguments to the environment.
        chosen: dict[str, Any] = {"model": model, "protected": protect, **options}
        settings = Settings(
            task_dir=task_dir,
            state_root=self.state_root,
            opencode_url=opencode.url,
            opencode_password=SecretStr(opencode.password),
            **{key: value for key, value in chosen.items() if value is not None},
        )
        ready: asyncio.Future[Manifest] = asyncio.get_running_loop().create_future()
        done = asyncio.create_task(self._run(settings, paths, ready))
        try:
            return await ready
        except BaseException:
            done.cancel()
            raise

    async def _ready_opencode(self) -> SharedHost:
        """The shared server, started again if it died; its tasks stopped with it."""
        if self.opencode is None or (self.opencode.process and not self.opencode.alive):
            self.opencode = self.new_opencode()
        await anyio.to_thread.run_sync(self.opencode.wait_ready, self.opencode.startup_timeout)
        return self.opencode

    async def wait_task(self, task_dir: Path) -> None:
        """Return once the task in ``task_dir`` has stopped, for whatever reason."""
        running = self._tasks.get(task_dir)
        if running is not None:
            await asyncio.wait({running.done})

    async def stop_task(self, task_dir: Path) -> bool:
        """Stop one task as ``/finish`` would. False if it was not running."""
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
                # One task failing must not take the others, or the hub, with it.
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
    """Every task of the session. Public, holds no data: see ``tandem.bridge.app``."""
    page = files("tandem.bridge").joinpath("hub.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers=dashboard_headers())


@contextlib.contextmanager
def stop_on_signals(stop: asyncio.Event) -> Generator[None, None, None]:
    """Turn SIGINT/SIGTERM into ``stop.set()`` while the block runs.

    ``signal.signal`` rather than the loop's handler, so the previous handlers
    come back exactly as they were, whoever installed them.
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
    """``tandem serve``: one task on a hub of its own, until it finishes or a signal.

    The same hub ``tandem mcp`` runs, so a task started by hand behaves exactly
    like one a planner started. It returns when the task stops, through
    ``/finish``, a lost OpenCode, or SIGINT/SIGTERM, and raises what stopped it
    if that was an error.
    """
    records = settings.state_root / "hosts"
    reap_orphans(records)
    binary = resolve_binary(settings.opencode_bin)

    def new_opencode() -> SharedHost:
        port: dict[str, Any] = {"port": settings.opencode_port} if settings.opencode_port else {}
        host = SharedHost(binary, log_path=records / f"{os.getpid()}.log", records=records, **port)
        # Called on the event loop's thread, which lives as long as this process.
        host.start(die_with_parent=True)
        return host

    task_dir = settings.task_dir
    # The task's own options, as given: the hub reads the rest from the
    # environment, as it does for a task the MCP server starts.
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
