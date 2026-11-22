"""``AntigravityBackend``: Antigravity CLI (``agy``), over ACP, as an implementer.

One ``agy acp`` process per backend, started on ``health()`` or the first
``create_session()`` in the task directory and stopped on ``__aexit__``. It
runs on whatever ``agy`` is signed in with; inTandem passes it no credentials.

ACP pushes; the seam pulls. ``session/update`` notifications become events in
one queue that ``events()`` serves. ``session/prompt`` blocks until the turn
ends, so it runs in a background task and ``prompt()`` returns at once. When
the agent asks ``session/request_permission``, the request either gets an
answer at once or parks a future in a pending map until the planner answers
through ``reply_permission``.

**How protected files are guarded.** On ``session/request_permission``: an
edit whose paths match ``Guard.protected`` is always parked for the planner,
even when the guard is unattended. Everything else is allowed at once when the
guard is unattended, otherwise parked too. The backend only ever picks the
agent's ``allow_once`` option, never ``allow_always``: an agent-side "always"
could let a later edit of a protected file through without asking. "always" is
remembered here instead. A mode such as ``auto`` or ``accept-edits`` may stop
the agent asking for edits at all, and a shell command that writes a file is
not an edit; for both, a completed edit reports ``FILE_EDITED`` and the
supervisor's content hash checks the protected files at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from intandem.antigravity import translate
from intandem.antigravity.acp import (
    ACPTransport,
    MethodNotFound,
    TransportFactory,
    default_transport,
)
from intandem.antigravity.guard import (
    command,
    edit_paths,
    preview_metadata,
    protected_paths,
    tool_name,
)
from intandem.backend.diff import git_diff, git_head
from intandem.backend.errors import BackendError, NotFoundError
from intandem.backend.events import BackendEvent, EventType
from intandem.backend.guard import Guard
from intandem.backend.jsonpath import dig, dig_list, dig_str
from intandem.backend.models import (
    FileDiff,
    Health,
    Message,
    ModelRef,
    PermissionReply,
    PermissionRequest,
    QuestionRequest,
    Session,
    SessionStatus,
    TokenUsage,
)

log = logging.getLogger(__name__)

PROVIDERS = frozenset({"antigravity", "google"})
"""Prefixes a model may carry; the agent itself takes bare model IDs."""
PROTOCOL_VERSION = 1
INIT_TIMEOUT = 60.0
"""Seconds ``initialize`` may take before the agent counts as unresponsive."""

CANCELLED: dict[str, Any] = {"outcome": {"outcome": "cancelled"}}


@dataclass
class _Pending:
    session_id: str
    future: asyncio.Future[tuple[PermissionReply, str | None]]
    protected: list[str] = field(default_factory=list[str])
    """For a protected edit: the paths it would write."""


@dataclass
class _Session:
    id: str
    """The agent's own session id, which is inTandem's too."""
    title: str
    guard: Guard
    base: str | None
    """The commit the task directory was at when the session started, for ``diff``."""
    translator: translate.Translator
    available_modes: tuple[str, ...] = ()
    mode: str | None = None
    model: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    aborting: bool = False
    closed: bool = False
    epoch: int = 0
    """Bumped when the session is closed, so a queued turn cannot run on it."""
    always: set[str] = field(default_factory=set[str])
    """Tools the planner allowed for the rest of the session."""
    always_protected: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    """(tool, path) pairs the planner allowed for the rest of the session."""
    transcript: list[Message] = field(default_factory=list[Message])
    tokens: TokenUsage = field(default_factory=TokenUsage)


class AntigravityBackend:
    """``ImplementerBackend`` over ``agy acp``. One instance serves one task directory."""

    def __init__(
        self,
        cwd: Path,
        *,
        cli: str = "agy",
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.cwd = cwd.expanduser().resolve()
        self.cli = cli
        self._factory = transport_factory or default_transport
        self._transport: ACPTransport | None = None
        self._starting = asyncio.Lock()
        self._sessions: dict[str, _Session] = {}
        self._permissions: dict[str, tuple[PermissionRequest, _Pending]] = {}
        self._events: asyncio.Queue[BackendEvent | None] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._version: str | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close every session, stop the agent and end the event stream."""
        for _, pending in list(self._permissions.values()):
            if not pending.future.done():
                pending.future.cancel()
        for session in self._sessions.values():
            await self._close_session(session)
        for task in list(self._tasks):
            task.cancel()
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.close()
        self._events.put_nowait(None)

    # ---- the seam ------------------------------------------------------------

    async def health(self) -> Health:
        """The agent's name and version. Raises ``BackendError`` once it has died."""
        if self._transport is not None and not self._transport.alive:
            raise BackendError(f"`{self.cli} acp` is no longer running")
        await self._connected()
        return Health(healthy=True, version=self._version or "unknown")

    async def create_session(self, title: str, guard: Guard) -> Session:
        """A new ACP session in the task directory.

        A task runs one spec at a time, and the supervisor aborts a spec before
        starting the next, so older sessions are closed here. Their
        transcripts and totals stay readable.
        """
        transport = await self._connected(restart=True)
        for older in self._sessions.values():
            await self._close_session(older)
        result = await transport.request("session/new", {"cwd": str(self.cwd), "mcpServers": []})
        session_id = dig_str(result, "sessionId")
        if not session_id:
            raise BackendError(f"session/new returned no sessionId: {str(result)[:300]}")
        modes = tuple(
            mode_id
            for m in dig_list(result, "modes", "availableModes")
            if (mode_id := dig_str(m, "id"))
        )
        session = _Session(
            id=session_id,
            title=title,
            guard=guard,
            base=await git_head(self.cwd),
            translator=translate.Translator(session_id),
            available_modes=modes,
            mode=dig_str(result, "modes", "currentModeId"),
            model=dig_str(result, "models", "currentModelId"),
        )
        self._sessions[session_id] = session
        native = {"type": "session_created", "result": result}
        self._emit(
            BackendEvent(
                type=EventType.SESSION_CREATED,
                native_type="antigravity.session_created",
                session_id=session_id,
                model=session.model,
                native=native,
                raw=translate.dumps(native),
            )
        )
        return self._info(session)

    async def get_session(self, session_id: str) -> Session:
        return self._info(self._session(session_id))

    async def guard_subagent(self, session_id: str, guard: Guard, inherited: object = None) -> None:
        """Nothing to do: an agy subagent shares its parent's ACP session.

        Its tool calls reach ``session/request_permission`` on the same session,
        so the session's guard covers them, and the content hash backstops it.
        """
        self._session(session_id)

    async def session_status(self) -> dict[str, SessionStatus]:
        return {
            session.id: SessionStatus(type="busy" if session.busy else "idle")
            for session in self._sessions.values()
        }

    async def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        transcript = list(self._session(session_id).transcript)
        return transcript[-limit:] if limit else transcript

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None:
        """Queue a prompt and return at once. The turn runs in the background.

        ``model`` is a bare agy model ID, or one prefixed ``antigravity/`` or
        ``google/``. ``agent`` is one of the session's modes, such as
        ``default`` or ``plan``; ``None`` keeps the current one.
        """
        session = self._session(session_id)
        if session.closed:
            raise BackendError(f"antigravity session {session_id} was closed: start a new one")
        model_id = _model_id(model)
        if agent is not None and agent not in session.available_modes:
            valid = ", ".join(session.available_modes) or "none"
            raise BackendError(f"no antigravity mode {agent!r}: the session offers {valid}")
        session.aborting = False
        session.transcript.append(translate.user_message(text, f"msg_{next(self._ids)}"))
        self._set_busy(session, True)
        self._spawn(self._turn(session, text, model_id, agent, session.epoch))

    async def abort(self, session_id: str) -> bool:
        session = self._session(session_id)
        self._settle_pending(session_id, "aborted")
        if not session.busy:
            return False
        session.aborting = True
        transport = self._transport
        if transport is None or not transport.alive:
            return True
        try:
            await transport.notify("session/cancel", {"sessionId": session.id})
        except BackendError as exc:
            raise BackendError(f"session/cancel failed: {exc}") from exc
        return True

    async def permissions(self) -> list[PermissionRequest]:
        return [request for request, _ in self._permissions.values()]

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool:
        entry = self._permissions.get(request_id)
        if entry is None or entry[1].future.done():
            raise NotFoundError(f"no pending permission {request_id}")
        request, pending = entry
        session = self._sessions.get(pending.session_id)
        if reply == "always" and session is not None:
            if pending.protected:
                session.always_protected.update((request.permission, p) for p in pending.protected)
            else:
                session.always.add(request.permission)
        pending.future.set_result((reply, message))
        return True

    async def questions(self) -> list[QuestionRequest]:
        """None: ACP has no question requests; the agent asks in its own text."""
        return []

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        return False

    async def reject_question(self, request_id: str) -> bool:
        return False

    async def diff(self, session_id: str) -> list[FileDiff]:
        """What changed in the task directory since the session started, from git."""
        session = self._session(session_id)
        return await git_diff(self.cwd, session.base)

    async def events(self) -> AsyncIterator[BackendEvent]:
        """The backend's own event queue. Returns once the backend is closed."""
        yield BackendEvent(type=EventType.CONNECTED, native_type="antigravity.connected", raw="{}")
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)  # Any later reader ends too.

    # ---- the agent -------------------------------------------------------------

    async def _connected(self, *, restart: bool = False) -> ACPTransport:
        """The running agent, started (or, with ``restart``, restarted) and initialized."""
        async with self._starting:
            transport = self._transport
            if transport is not None and transport.alive:
                return transport
            if transport is not None and not restart:
                raise BackendError(f"`{self.cli} acp` is no longer running")
            if transport is not None:
                with contextlib.suppress(Exception):
                    await transport.close()
                for session in self._sessions.values():
                    self._retire(session)
            transport = self._factory(self.cli, self.cwd)
            transport.set_notification_handler(self._on_notification)
            transport.set_request_handler(self._on_request)
            await transport.start()
            try:
                result = await asyncio.wait_for(
                    transport.request(
                        "initialize",
                        {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
                    ),
                    timeout=INIT_TIMEOUT,
                )
            except TimeoutError as exc:
                with contextlib.suppress(Exception):
                    await transport.close()
                raise BackendError(f"`{self.cli} acp` did not answer initialize") from exc
            except BackendError:
                with contextlib.suppress(Exception):
                    await transport.close()
                raise
            name = dig_str(result, "agentInfo", "name") or "Antigravity"
            version = dig_str(result, "agentInfo", "version")
            self._version = f"{name} {version}" if version else name
            self._transport = transport
            return transport

    async def _turn(
        self, session: _Session, text: str, model: str | None, agent: str | None, epoch: int
    ) -> None:
        async with session.lock:
            if session.epoch != epoch:
                return
            if session.aborting:
                self._turn_aborted(session)
                return
            self._set_busy(session, True)
            try:
                transport = await self._connected()
                if model is not None and model != session.model:
                    params = {"sessionId": session.id, "modelId": model}
                    await transport.request("session/set_model", params)
                    session.model = model
                if agent is not None and agent != session.mode:
                    await transport.request(
                        "session/set_mode", {"sessionId": session.id, "modeId": agent}
                    )
                    session.mode = agent
                if session.epoch != epoch:
                    return
                if session.aborting:
                    self._turn_aborted(session)
                    return
                prompt = [{"type": "text", "text": text}]
                result = await transport.request(
                    "session/prompt", {"sessionId": session.id, "prompt": prompt}
                )
            except BackendError as exc:
                if session.epoch == epoch:
                    log.warning("antigravity prompt failed: %s", exc)
                    self._turn_failed(session, exc)
                return
            self._turn_done(session, result, retired=session.epoch != epoch)

    def _turn_done(self, session: _Session, result: object, *, retired: bool = False) -> None:
        session.tokens = translate.add_usage(
            session.tokens, translate.token_usage(dig(result, "usage"))
        )
        message = session.translator.finish(result, f"msg_{next(self._ids)}", session.model)
        session.transcript.append(message)
        if retired:
            return  # Closed under the turn: what it did is kept, but it is over already.
        stop = dig_str(result, "stopReason") or "end_turn"
        native = {"type": "prompt_result", "sessionId": session.id, "result": result}
        if stop == "cancelled" or session.aborting:
            self._turn_aborted(session, native)
            return
        session.aborting = False
        self._set_busy(session, False)
        self._emit(
            BackendEvent(
                type=EventType.IDLE,
                native_type="antigravity.prompt_result",
                session_id=session.id,
                native=native,
                raw=translate.dumps(native),
            )
        )

    def _turn_aborted(self, session: _Session, native: dict[str, Any] | None = None) -> None:
        session.aborting = False
        self._set_busy(session, False)
        body = native or {"type": "aborted", "sessionId": session.id}
        self._emit(
            BackendEvent(
                type=EventType.ERROR,
                native_type="antigravity.aborted",
                session_id=session.id,
                error_name="MessageAbortedError",
                error_message="the turn was cancelled",
                aborted=True,
                native=body,
                raw=translate.dumps(body),
            )
        )

    def _turn_failed(self, session: _Session, exc: BaseException) -> None:
        was_aborting = session.aborting
        session.aborting = False
        self._set_busy(session, False)
        native = {"type": "error", "sessionId": session.id, "error": str(exc)[:2000]}
        self._emit(
            BackendEvent(
                type=EventType.ERROR,
                native_type="antigravity.error",
                session_id=session.id,
                error_name=type(exc).__name__,
                error_message=str(exc)[:2000],
                aborted=was_aborting,
                native=native,
                raw=translate.dumps(native),
            )
        )

    def _set_busy(self, session: _Session, busy: bool) -> None:
        if session.busy == busy:
            return
        session.busy = busy
        native = {"type": "status", "sessionId": session.id, "busy": busy}
        self._emit(
            BackendEvent(
                type=EventType.STATUS,
                native_type="antigravity.status",
                session_id=session.id,
                status="busy" if busy else "idle",
                model=session.model,
                native=native,
                raw=translate.dumps(native),
            )
        )

    async def _close_session(self, session: _Session) -> None:
        """Stop the session's turn and close it on the agent; its transcript stays."""
        if session.closed:
            return
        was_busy = session.busy
        self._retire(session)
        transport = self._transport
        if transport is None or not transport.alive:
            return
        with contextlib.suppress(BackendError):
            if was_busy:
                await transport.notify("session/cancel", {"sessionId": session.id})
            await transport.request("session/close", {"sessionId": session.id})

    def _retire(self, session: _Session) -> None:
        session.epoch += 1
        session.closed = True
        session.aborting = False
        self._settle_pending(session.id, "closed")
        self._set_busy(session, False)

    # ---- what the agent sends ------------------------------------------------

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            native = {"type": "notification", "method": method, "params": params}
            self._emit(
                BackendEvent(
                    type=EventType.OTHER,
                    native_type=f"antigravity.{method}",
                    native=native,
                    raw=translate.dumps(native),
                )
            )
            return
        session = self._sessions.get(dig_str(params, "sessionId") or "")
        if session is None:
            return
        for event in session.translator.update(params):
            self._emit(event)

    async def _on_request(self, method: str, params: dict[str, Any]) -> object:
        if method == "session/request_permission":
            return await self._request_permission(params)
        raise MethodNotFound(method)

    async def _request_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self._sessions.get(dig_str(params, "sessionId") or "")
        if session is None or session.closed:
            return CANCELLED
        call = dig(params, "toolCall")
        options = dig_list(params, "options")
        tool = tool_name(call)
        hits = protected_paths(call, self.cwd, session.guard.protected)
        hits = [path for path in hits if (tool, path) not in session.always_protected]
        if not hits and (session.guard.unattended or tool in session.always):
            return _choose(options, "once")
        shell = command(call)
        patterns = hits or edit_paths(call) or ([shell] if shell else [])
        metadata: dict[str, Any] = {
            "input": dig(call, "rawInput")
            if dig(call, "rawInput") is not None
            else dig(call, "args"),
            "tool_call_id": dig_str(call, "toolCallId"),
            "kind": dig_str(call, "kind"),
            "options": options,
            **preview_metadata(call),
        }
        if hits:
            metadata["protected"] = hits
        if title := dig_str(call, "title"):
            metadata["title"] = title
        try:
            reply, _ = await self._ask_permission(session, tool, patterns, metadata)
        except asyncio.CancelledError:
            # An abort or close cancels the future; anything cancelling this task stops it.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise
            return CANCELLED
        return _choose(options, reply)

    async def _ask_permission(
        self, session: _Session, tool: str, patterns: list[str], metadata: dict[str, Any]
    ) -> tuple[PermissionReply, str | None]:
        request = PermissionRequest(
            id=f"per_{next(self._ids)}",
            session_id=session.id,
            permission=tool,
            patterns=patterns,
            metadata=metadata,
        )
        pending = _Pending(
            session.id,
            asyncio.get_running_loop().create_future(),
            protected=list(metadata.get("protected", [])),
        )
        self._permissions[request.id] = (request, pending)
        native = {"type": "permission", **request.model_dump(mode="json")}
        self._emit(
            BackendEvent(
                type=EventType.PERMISSION_ASKED,
                native_type="antigravity.permission",
                session_id=session.id,
                request_id=request.id,
                permission=tool,
                patterns=patterns,
                native=native,
                raw=translate.dumps(native),
            )
        )
        reply: str | None = "cancelled"
        try:
            answered = await pending.future
            reply = answered[0]
            return answered
        finally:
            self._permissions.pop(request.id, None)
            self._emit(self._settled(session.id, request.id, reply))

    def _settle_pending(self, session_id: str, message: str) -> None:
        """Cancel whatever the session is waiting on, as an abort should."""
        for _, pending in list(self._permissions.values()):
            if pending.session_id == session_id and not pending.future.done():
                pending.future.cancel(message)

    @staticmethod
    def _settled(session_id: str, request_id: str, reply: str | None) -> BackendEvent:
        native = {"type": "permission.replied", "request_id": request_id, "reply": reply}
        return BackendEvent(
            type=EventType.PERMISSION_REPLIED,
            native_type="antigravity.permission.replied",
            session_id=session_id,
            request_id=request_id,
            native=native,
            raw=translate.dumps(native),
        )

    # ---- helpers ---------------------------------------------------------------

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"no such antigravity session {session_id}")
        return session

    @staticmethod
    def _info(session: _Session) -> Session:
        return Session.model_validate(
            {
                "id": session.id,
                "title": session.title,
                "cost": 0.0,
                "tokens": session.tokens,
                "model": session.model,
                "mode": session.mode,
            }
        )

    def _emit(self, event: BackendEvent) -> None:
        self._events.put_nowait(event)

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _model_id(model: ModelRef | None) -> str | None:
    """The bare agy model ID for ``model``; ``antigravity/`` and ``google/`` are dropped."""
    if model is None:
        return None
    if model.provider_id is None:
        return model.model_id
    if model.provider_id in PROVIDERS:
        return model.model_id
    raise BackendError(
        f"the antigravity harness runs agy's own models, not {model.provider_id!r} ones: "
        "use a bare model ID from `agy models`, e.g. gemini-4-neon-high, or antigravity/<model>"
    )


_KINDS: dict[str, tuple[str, ...]] = {
    "once": ("allow_once", "allow_always"),
    "always": ("allow_once", "allow_always"),
    "reject": ("reject_once", "reject_always"),
}


def _choose(options: list[object], reply: str) -> dict[str, Any]:
    """The reply to ``session/request_permission`` that picks the option ``reply`` means.

    ``allow_once`` first, even for "always": the planner's "always" is kept by
    the backend, so the agent goes on asking and protected edits still stop.
    """
    for kind in _KINDS.get(reply, ()):
        for option in options:
            if dig_str(option, "kind") == kind and (option_id := dig_str(option, "optionId")):
                return {"outcome": {"outcome": "selected", "optionId": option_id}}
    return CANCELLED
