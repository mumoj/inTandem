"""A JSON-RPC 2.0 client for ACP (Agent Client Protocol) over a child's stdio.

``agy acp`` speaks ACP as newline-delimited JSON on stdin and stdout. Both
sides send requests: inTandem asks for ``session/prompt``, and the agent asks
back for ``session/request_permission`` while the prompt is still running. So
the transport reads one stream for three kinds of line:

* a response (``id`` with ``result`` or ``error``) resolves the request it answers,
* a request (``id`` with ``method``) goes to the request handler, in a task of
  its own, and its return value is written back as the response, and
* a notification (``method`` without ``id``) goes to the notification
  handler, inline, so ``session/update`` notifications keep their order.

Only the stdlib: ``asyncio.create_subprocess_exec`` and ``json``. Tests hand
the backend an in-memory ``ACPTransport`` through a ``TransportFactory`` and
never spawn ``agy``.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import shutil
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

from intandem.backend.errors import BackendError

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024
"""Bytes one line may hold. A tool's whole output can arrive in one update."""
STDERR_LINES = 200
"""How much of the agent's stderr is kept, for the error a crash reports."""
CLOSE_GRACE = 5.0
"""Seconds the agent gets to exit after its stdin closes, before it is terminated."""

NotificationHandler = Callable[[str, dict[str, Any]], None]
"""Called with ``(method, params)`` for every notification, in arrival order."""
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[object]]
"""Called with ``(method, params)`` for every request; its return value is the result."""


class ACPError(BackendError):
    """The agent answered a request with a JSON-RPC error."""

    def __init__(self, method: str, code: object, message: str, data: object = None) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.method = method
        self.code = code
        self.data = data


class MethodNotFound(Exception):  # noqa: N818  # The JSON-RPC error's own name.
    """Raised by a request handler for a method it does not serve."""


class ACPTransport(Protocol):
    """One ACP connection: what ``AntigravityBackend`` calls."""

    async def start(self) -> None:
        """Start the agent. Raises ``BackendError`` if it cannot run."""
        ...

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a request and wait for its result. Raises ``BackendError`` on an error."""
        ...

    async def notify(self, method: str, params: dict[str, Any]) -> None: ...

    async def close(self) -> None:
        """Stop the agent. Every request still waiting fails with ``BackendError``."""
        ...

    @property
    def alive(self) -> bool: ...

    def set_notification_handler(self, handler: NotificationHandler) -> None: ...

    def set_request_handler(self, handler: RequestHandler) -> None: ...


TransportFactory = Callable[[str, Path], ACPTransport]
"""Builds a transport from ``(cli, cwd)``. The default runs ``[cli, "acp"]``."""


class StdioACPTransport:
    """``[cli, "acp"]`` as a child process, spoken to over its stdin and stdout."""

    def __init__(self, cli: str, cwd: Path) -> None:
        self.cli = cli
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._handling: set[asyncio.Task[None]] = set()
        self._stderr: deque[str] = deque(maxlen=STDERR_LINES)
        self._write_lock = asyncio.Lock()
        self._on_notification: NotificationHandler = _ignore
        self._on_request: RequestHandler | None = None
        self._closed = False

    @property
    def alive(self) -> bool:
        return (
            not self._closed
            and self._process is not None
            and self._process.returncode is None
            and self._reader is not None
            and not self._reader.done()
        )

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        self._on_notification = handler

    def set_request_handler(self, handler: RequestHandler) -> None:
        self._on_request = handler

    def stderr_tail(self, lines: int = 20) -> str:
        return "\n".join(list(self._stderr)[-lines:])

    async def start(self) -> None:
        if self._process is not None:
            return
        binary = shutil.which(self.cli)
        if binary is None:
            raise BackendError(
                f"the Antigravity CLI ({self.cli}) is not on PATH: install Antigravity, "
                "sign in with `agy`, or set --antigravity-bin"
            )
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary,
                "acp",
                cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LIMIT,
            )
        except OSError as exc:
            raise BackendError(f"could not start `{self.cli} acp`: {exc}") from exc
        self._reader = asyncio.create_task(self._read())
        self._stderr_reader = asyncio.create_task(self._drain_stderr())

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if not self.alive:
            raise BackendError(self._dead_message())
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = (method, future)
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        finally:
            self._waiting.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.alive:
            raise BackendError(self._dead_message())
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in list(self._handling):
            task.cancel()
        process = self._process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=CLOSE_GRACE)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=CLOSE_GRACE)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        for task in (self._reader, self._stderr_reader):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_waiting(BackendError(f"`{self.cli} acp` was closed"))

    # ---- the wire -------------------------------------------------------------

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise BackendError(self._dead_message())
        line = json.dumps(message, separators=(",", ":")) + "\n"
        try:
            async with self._write_lock:
                process.stdin.write(line.encode())
                await process.stdin.drain()
        except (ConnectionError, RuntimeError) as exc:
            raise BackendError(f"`{self.cli} acp` stopped reading: {exc}") from exc

    async def _read(self) -> None:
        process = self._process
        assert process is not None
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                self._dispatch(line)
        except (ValueError, asyncio.LimitOverrunError) as exc:
            log.warning("agy acp sent a line too long to read: %s", exc)
        except ConnectionError as exc:
            log.warning("agy acp stream failed: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=1.0)
            self._fail_waiting(BackendError(self._dead_message()))

    def _dispatch(self, line: bytes) -> None:
        try:
            decoded: object = json.loads(line)
        except ValueError:
            log.debug("agy acp: not JSON: %r", line[:200])
            return
        if not isinstance(decoded, dict):
            return
        message = cast(dict[str, Any], decoded)
        method = message.get("method")
        has_id = "id" in message and message["id"] is not None
        params_value = message.get("params")
        params = cast(dict[str, Any], params_value) if isinstance(params_value, dict) else {}
        if isinstance(method, str) and has_id:
            task = asyncio.create_task(self._answer(message["id"], method, params))
            self._handling.add(task)
            task.add_done_callback(self._handling.discard)
        elif isinstance(method, str):
            try:
                self._on_notification(method, params)
            except Exception:
                log.exception("ACP notification handler failed on %s", method)
        elif has_id:
            self._resolve(message)

    def _resolve(self, message: dict[str, Any]) -> None:
        request_id = message["id"]
        entry = self._waiting.get(request_id) if isinstance(request_id, int) else None
        if entry is None:
            return
        method, future = entry
        if future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            body = cast(dict[str, Any], error)
            future.set_exception(
                ACPError(method, body.get("code"), str(body.get("message", "")), body.get("data"))
            )
        else:
            future.set_result(message.get("result"))

    async def _answer(self, request_id: object, method: str, params: dict[str, Any]) -> None:
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        handler = self._on_request
        try:
            if handler is None:
                raise MethodNotFound(method)
            reply["result"] = await handler(method, params)
        except asyncio.CancelledError:
            raise
        except MethodNotFound:
            reply["error"] = {"code": -32601, "message": f"method not found: {method}"}
        except Exception as exc:
            log.exception("ACP request handler failed on %s", method)
            reply["error"] = {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}
        with contextlib.suppress(BackendError):
            await self._write(reply)

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None
        assert process.stderr is not None
        with contextlib.suppress(ValueError, ConnectionError, asyncio.LimitOverrunError):
            while line := await process.stderr.readline():
                text = line.decode(errors="replace").rstrip()
                self._stderr.append(text)
                log.debug("agy: %s", text)

    def _fail_waiting(self, error: BackendError) -> None:
        for _, future in list(self._waiting.values()):
            if not future.done():
                future.set_exception(error)

    def _dead_message(self) -> str:
        code = self._process.returncode if self._process is not None else None
        tail = self.stderr_tail()
        message = f"`{self.cli} acp` is not running"
        if code is not None:
            message += f" (exit code {code})"
        return f"{message}: {tail}" if tail else message


def _ignore(method: str, params: dict[str, Any]) -> None:
    del method, params


def default_transport(cli: str, cwd: Path) -> ACPTransport:
    return StdioACPTransport(cli, cwd)
