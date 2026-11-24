"""``ImplementerBackend`` adapter for Claude Code via the Claude Agent SDK.

Each session runs a ``ClaudeSDKClient`` backed by the local ``claude`` CLI in
the task directory, using the user's existing CLI authentication.

Because the SDK pushes messages and permission callbacks while the supervisor
interface is pull-based, each session reads messages into an event queue served
by ``events()`` and stores pending permission/question futures in maps resolved
by ``reply_permission`` and ``reply_question``.

Protected-file enforcement uses a ``PreToolUse`` hook rather than relying solely
on ``can_use_tool``: ``can_use_tool`` is bypassed when a user's CLI rules or
permission mode already allow a tool, whereas ``PreToolUse`` intercepts every
tool call (including subagent calls). Matching edits to ``Edit``/``Write``/
``MultiEdit``/``NotebookEdit`` pause on a pending permission future with
``on_failure="block"``. Non-protected calls fall through to ``can_use_tool``.
Shell commands that write protected files are caught by the supervisor's
content-hash checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

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
    QuestionInfo,
    QuestionOption,
    QuestionRequest,
    Session,
    SessionStatus,
    TokenUsage,
)
from intandem.claude import translate
from intandem.claude.guard import edit_paths, protected_paths
from intandem.claude.sdk import (
    ClientFactory,
    SDKClient,
    api_key_warning,
    default_client,
    require_sdk,
)

if TYPE_CHECKING:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        HookContext,
        HookInput,
        HookJSONOutput,
        PermissionResult,
        ToolPermissionContext,
    )

log = logging.getLogger(__name__)

PROVIDERS = frozenset({"anthropic", "claude"})
QUESTION_TOOL = "AskUserQuestion"
HOOK_TIMEOUT = 7 * 24 * 3600.0
"""Timeout (seconds) for a protected-edit permission hook before blocking."""


@dataclass
class _Pending:
    session_id: str
    future: asyncio.Future[Any]
    protected: list[str] = field(default_factory=list[str])
    """Protected paths targeted by this edit request, if any."""


@dataclass
class _Session:
    id: str
    title: str
    guard: Guard
    sdk_session_id: str
    """UUID used by the ``claude`` CLI for this session's transcript on disk."""
    base: str | None
    """HEAD commit when the session was created, used as the baseline for ``diff``."""
    translator: translate.Translator
    client: SDKClient | None = None
    reader: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    model: str | None = None
    connected_before: bool = False
    """True after the first connection so reconnects resume the existing CLI session."""
    busy: bool = False
    aborting: bool = False
    epoch: int = 0
    """Incremented on disconnect so an in-flight connect cannot attach to a closed session."""
    always: set[str] = field(default_factory=set[str])
    """Tool names approved with ``always`` for the remainder of the session."""
    always_protected: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    """``(tool, path)`` pairs approved with ``always`` for the remainder of the session."""
    transcript: list[Message] = field(default_factory=list[Message])
    cost: float = 0.0
    tokens: TokenUsage = field(default_factory=TokenUsage)
    live_usage: dict[str, TokenUsage] = field(default_factory=dict[str, TokenUsage])
    """Per-message token counts during an active turn, cleared when ``ResultMessage`` arrives."""


class ClaudeCodeBackend:
    """``ImplementerBackend`` backed by Claude Code for a single task directory."""

    def __init__(
        self,
        cwd: Path,
        *,
        cli: str = "claude",
        client_factory: ClientFactory | None = None,
        partial_messages: bool = True,
    ) -> None:
        require_sdk()
        self.cwd = cwd.expanduser().resolve()
        self.cli = cli
        self._factory = client_factory or default_client
        self._partial = partial_messages
        self._sessions: dict[str, _Session] = {}
        self._permissions: dict[str, tuple[PermissionRequest, _Pending]] = {}
        self._questions: dict[str, tuple[QuestionRequest, _Pending]] = {}
        self._events: asyncio.Queue[BackendEvent | None] = asyncio.Queue()
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task[None]] = set()
        self._version: str | None = None
        if (warning := api_key_warning()) is not None:
            log.warning("%s", warning)

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
        """Cancel pending requests, disconnect all session CLIs, and close the event stream."""
        for pending in [p for _, p in [*self._permissions.values(), *self._questions.values()]]:
            if not pending.future.done():
                pending.future.cancel()
        for task in list(self._tasks):
            task.cancel()
        for session in self._sessions.values():
            await self._disconnect(session)
        self._events.put_nowait(None)

    # ---- ImplementerBackend protocol -----------------------------------------

    async def health(self) -> Health:
        if self._version is None:
            self._version = await self._cli_version()
        return Health(healthy=True, version=self._version)

    async def create_session(self, title: str, guard: Guard) -> Session:
        """Create a new session (its CLI process starts on the first prompt).

        Disconnects any earlier sessions' CLIs while retaining their transcripts
        and usage totals for reporting.
        """
        for older in self._sessions.values():
            await self._disconnect(older)
        session_id = f"ses_{uuid.uuid4().hex}"
        session = _Session(
            id=session_id,
            title=title,
            guard=guard,
            sdk_session_id=str(uuid.uuid4()),
            base=await git_head(self.cwd),
            translator=translate.Translator(session_id),
        )
        self._sessions[session_id] = session
        self._emit(
            BackendEvent(
                type=EventType.SESSION_CREATED,
                native_type="claude.session_created",
                session_id=session_id,
            )
        )
        return self._info(session)

    async def get_session(self, session_id: str) -> Session:
        return self._info(self._session(session_id))

    async def guard_subagent(self, session_id: str, guard: Guard, inherited: object = None) -> None:
        """No-op because Claude Code subagents share the parent CLI session and hooks."""
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
        """Queue a prompt asynchronously and run the turn in a background task."""
        session = self._session(session_id)
        if agent is not None:
            raise BackendError("the claude harness has no agents: leave `agent` unset")
        if model is not None and model.provider_id not in (None, *PROVIDERS):
            raise BackendError(
                f"the claude harness runs Anthropic models only, not {model.provider_id!r}: "
                "use a Claude model name or anthropic/<model>, e.g. claude-sonnet-4-5"
            )
        session.aborting = False
        session.transcript.append(translate.user_message(text, f"msg_{next(self._ids)}"))
        self._set_busy(session, True)
        self._spawn(self._send(session, text, model.model_id if model else None, session.epoch))

    async def abort(self, session_id: str) -> bool:
        session = self._session(session_id)
        self._settle_pending(session_id, "aborted")
        if not session.busy:
            return False
        session.aborting = True
        if session.client is None:
            return True
        try:
            await session.client.interrupt()
        except Exception as exc:
            raise BackendError(f"interrupt failed: {type(exc).__name__}: {exc}") from exc
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
        return [request for request, _ in self._questions.values()]

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        pending = self._pending_question(request_id)
        pending.future.set_result(answers)
        return True

    async def reject_question(self, request_id: str) -> bool:
        pending = self._pending_question(request_id)
        pending.future.set_result(None)
        return True

    async def diff(self, session_id: str) -> list[FileDiff]:
        """Return git diff stats since the session's baseline commit, including untracked files."""
        session = self._session(session_id)
        return await git_diff(self.cwd, session.base)

    async def events(self) -> AsyncIterator[BackendEvent]:
        """Yield queued backend events until ``aclose()`` is called."""
        yield BackendEvent(type=EventType.CONNECTED, native_type="claude.connected", raw="{}")
        while (event := await self._events.get()) is not None:
            yield event
        self._events.put_nowait(None)  # Re-queue sentinel for any subsequent readers.

    # ---- turns -----------------------------------------------------------------

    async def _send(self, session: _Session, text: str, model: str | None, epoch: int) -> None:
        async with session.lock:
            if session.epoch != epoch:
                return
            if session.aborting:
                self._turn_aborted(session)
                return
            try:
                client = await self._connected(session, model, epoch)
                if client is None:
                    if session.aborting:
                        self._turn_aborted(session)
                    return
                if model is not None and model != session.model:
                    await client.set_model(model)
                    session.model = model
                if session.epoch != epoch:
                    return
                if session.aborting:
                    self._turn_aborted(session)
                    return
                await client.query(text)
            except Exception as exc:
                log.warning("claude prompt failed: %s", exc)
                self._turn_failed(session, exc)

    async def _connected(
        self, session: _Session, model: str | None, epoch: int
    ) -> SDKClient | None:
        if session.client is not None:
            return session.client
        session.model = model
        client = self._factory(self._options(session, model))
        await client.connect()
        if session.epoch != epoch or session.aborting:
            with contextlib.suppress(Exception):
                await client.disconnect()
            return None
        session.client = client
        session.connected_before = True
        session.reader = asyncio.create_task(self._read(session, client))
        return client

    async def _read(self, session: _Session, client: SDKClient) -> None:
        try:
            async for message in client.receive_messages():
                self._on_message(session, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("claude message stream failed: %s", exc)
            if session.client is client:
                session.client = None
            self._turn_failed(session, exc)
            return
        if session.client is client:
            session.client = None
        if session.busy:
            self._turn_failed(session, BackendError("the claude CLI closed its message stream"))

    def _on_message(self, session: _Session, message: object) -> None:
        native = translate.to_native(message)
        if not isinstance(native, dict):
            return
        body: dict[str, Any] = native  # pyright: ignore[reportUnknownVariableType]
        kind = dig_str(body, "type")
        if kind in ("assistant", "stream_event") and not session.busy:
            # A follow-up prompt queued behind the previous turn has begun.
            self._set_busy(session, True)
        if kind == "assistant":
            entry = translate.assistant_message(body, f"msg_{next(self._ids)}")
            session.transcript.append(entry)
            if entry.info.tokens is not None:
                key = dig_str(body, "message_id") or dig_str(body, "uuid") or entry.info.id
                session.live_usage[key] = entry.info.tokens
        elif kind == "user":
            _attach_tool_results(session.transcript, body)
        elif kind == "result":
            session.busy = False
            cost = dig(body, "total_cost_usd")
            if isinstance(cost, int | float):
                # total_cost_usd is cumulative for the session, whereas usage is per turn.
                session.cost = float(cost)
            turn = translate.token_usage(dig(body, "usage"))
            session.tokens = translate.add_usage(session.tokens, turn)
            session.live_usage.clear()
        for event in session.translator.translate(body, aborting=session.aborting):
            self._emit(event)
        if kind == "result":
            session.aborting = False

    def _turn_aborted(self, session: _Session) -> None:
        session.busy = False
        session.aborting = False
        self._emit(
            BackendEvent(
                type=EventType.STATUS,
                native_type="claude.aborted",
                session_id=session.id,
                status="idle",
            )
        )
        self._emit(
            BackendEvent(
                type=EventType.ERROR,
                native_type="claude.aborted",
                session_id=session.id,
                error_name="MessageAbortedError",
                error_message="the turn was interrupted",
                aborted=True,
                raw=translate.dumps({"type": "aborted"}),
            )
        )

    def _turn_failed(self, session: _Session, exc: BaseException) -> None:
        was_aborting = session.aborting
        session.busy = False
        session.aborting = False
        self._emit(
            BackendEvent(
                type=EventType.STATUS,
                native_type="claude.error",
                session_id=session.id,
                status="idle",
            )
        )
        self._emit(
            BackendEvent(
                type=EventType.ERROR,
                native_type="claude.error",
                session_id=session.id,
                error_name=type(exc).__name__,
                error_message=str(exc)[:2000],
                aborted=was_aborting,
                raw=translate.dumps({"type": "error", "error": str(exc)[:2000]}),
            )
        )

    def _set_busy(self, session: _Session, busy: bool) -> None:
        if session.busy == busy:
            return
        session.busy = busy
        self._emit(
            BackendEvent(
                type=EventType.STATUS,
                native_type="claude.status",
                session_id=session.id,
                status="busy" if busy else "idle",
                raw=translate.dumps({"type": "status", "busy": busy}),
            )
        )

    def _options(self, session: _Session, model: str | None) -> ClaudeAgentOptions:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        async def can_use_tool(
            tool: str, tool_input: dict[str, Any], context: ToolPermissionContext
        ) -> PermissionResult:
            return await self._can_use_tool(session, tool, tool_input, context)

        async def pre_tool_use(
            hook_input: HookInput, tool_use_id: str | None, context: HookContext
        ) -> HookJSONOutput:
            return await self._pre_tool_use(session, hook_input, tool_use_id)

        return ClaudeAgentOptions(
            cwd=self.cwd,
            cli_path=self._cli_path(),
            model=model,
            # Assign an explicit session UUID so we can resume it after reconnecting.
            session_id=None if session.connected_before else session.sdk_session_id,
            resume=session.sdk_session_id if session.connected_before else None,
            # Force "default" mode so local CLI settings cannot auto-approve or
            # bypass permissions without reaching our callbacks.
            permission_mode="default",
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [
                    HookMatcher(
                        matcher=None,
                        hooks=[pre_tool_use],
                        timeout=HOOK_TIMEOUT,
                        on_failure="block",
                    )
                ]
            },
            include_partial_messages=self._partial,
            stderr=lambda line: log.debug("claude: %s", line),
        )

    async def _disconnect(self, session: _Session) -> None:
        session.epoch += 1
        async with session.lock:
            client, session.client = session.client, None
            if client is not None or session.busy:
                session.aborting = False
                self._set_busy(session, False)
            if session.reader is not None:
                session.reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await session.reader
                session.reader = None
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.disconnect()

    # ---- permissions and questions -------------------------------------------

    async def _pre_tool_use(
        self, session: _Session, hook_input: HookInput, tool_use_id: str | None
    ) -> HookJSONOutput:
        """Pause edits to protected paths until the planner approves or denies them."""
        if hook_input["hook_event_name"] != "PreToolUse":
            return {}
        tool = hook_input["tool_name"]
        tool_input = hook_input["tool_input"]
        hits = protected_paths(tool, tool_input, self.cwd, session.guard.protected)
        hits = [path for path in hits if (tool, path) not in session.always_protected]
        if not hits:
            return {}
        metadata = {
            "protected": hits,
            "tool_use_id": tool_use_id,
            "input": tool_input,
            **_preview_metadata(tool, tool_input),
        }
        reply, message = await self._ask_permission(session, tool, hits, metadata)
        allowed = reply in ("once", "always")
        reason = (
            "allowed by the planner"
            if allowed
            else message or f"{', '.join(hits)} is protected and the planner refused this edit"
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": reason,
            }
        }

    async def _can_use_tool(
        self,
        session: _Session,
        tool: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResult:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if tool == QUESTION_TOOL:
            answers = await self._ask_question(session, tool_input)
            if answers is None:
                return PermissionResultDeny(
                    message="The planner declined to answer. Carry on with your best judgement."
                )
            return PermissionResultAllow(updated_input={**tool_input, "answers": answers})
        hits = protected_paths(tool, tool_input, self.cwd, session.guard.protected)
        hits = [path for path in hits if (tool, path) not in session.always_protected]
        # Backup check in case a protected edit bypassed the PreToolUse hook.
        if not hits and (tool in session.always or session.guard.unattended):
            return PermissionResultAllow()
        patterns = hits or edit_paths(tool, tool_input) or _command(tool_input)
        metadata: dict[str, Any] = {
            "input": tool_input,
            "tool_use_id": context.tool_use_id,
            **_preview_metadata(tool, tool_input),
        }
        if hits:
            metadata["protected"] = hits
        if context.title:
            metadata["title"] = context.title
        reply, message = await self._ask_permission(session, tool, patterns, metadata)
        if reply in ("once", "always"):
            return PermissionResultAllow()
        return PermissionResultDeny(message=message or "The planner refused this tool call.")

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
                native_type="claude.permission",
                session_id=session.id,
                request_id=request.id,
                permission=tool,
                patterns=patterns,
                native=native,
                raw=translate.dumps(native),
            )
        )
        answered: tuple[PermissionReply, str | None] = ("reject", "cancelled")
        try:
            answered = await pending.future
        finally:
            self._permissions.pop(request.id, None)
            reply = answered[0]
            self._emit(self._settled(EventType.PERMISSION_REPLIED, session.id, request.id, reply))
        return answered

    async def _ask_question(
        self, session: _Session, tool_input: dict[str, Any]
    ) -> dict[str, str] | None:
        """Pause an ``AskUserQuestion`` call and return answers keyed by question, or ``None``."""
        infos = [
            QuestionInfo(
                question=dig_str(q, "question") or "",
                header=dig_str(q, "header") or "",
                options=[
                    QuestionOption(
                        label=dig_str(o, "label") or "", description=dig_str(o, "description") or ""
                    )
                    for o in dig_list(q, "options")
                ],
                multiple=dig(q, "multiSelect") is True,
            )
            for q in dig_list(tool_input, "questions")
        ]
        request_id = f"que_{next(self._ids)}"
        request = QuestionRequest(id=request_id, session_id=session.id, questions=infos)
        pending = _Pending(session.id, asyncio.get_running_loop().create_future())
        self._questions[request.id] = (request, pending)
        native = {"type": "question", **request.model_dump(mode="json")}
        first = infos[0] if infos else None
        self._emit(
            BackendEvent(
                type=EventType.QUESTION_ASKED,
                native_type="claude.question",
                session_id=session.id,
                request_id=request.id,
                question=(first.question or first.header) if first else "",
                native=native,
                raw=translate.dumps(native),
            )
        )
        answers: list[list[str]] | None = None
        try:
            answers = await pending.future
        finally:
            self._questions.pop(request.id, None)
            kind = EventType.QUESTION_REJECTED if answers is None else EventType.QUESTION_REPLIED
            self._emit(self._settled(kind, session.id, request.id, None))
        if answers is None:
            return None
        return {
            info.question: ", ".join(answer)
            for info, answer in zip(infos, answers, strict=False)
            if answer
        }

    def _pending_question(self, request_id: str) -> _Pending:
        entry = self._questions.get(request_id)
        if entry is None or entry[1].future.done():
            raise NotFoundError(f"no pending question {request_id}")
        return entry[1]

    def _settle_pending(self, session_id: str, message: str) -> None:
        """Reject any pending permission or question futures for ``session_id``."""
        for _, pending in list(self._permissions.values()):
            if pending.session_id == session_id and not pending.future.done():
                pending.future.set_result(("reject", message))
        for _, pending in list(self._questions.values()):
            if pending.session_id == session_id and not pending.future.done():
                pending.future.set_result(None)

    @staticmethod
    def _settled(
        kind: EventType, session_id: str, request_id: str, reply: str | None
    ) -> BackendEvent:
        native = {"type": kind.value, "request_id": request_id, "reply": reply}
        return BackendEvent(
            type=kind,
            native_type=f"claude.{kind.value}",
            session_id=session_id,
            request_id=request_id,
            native=native,
            raw=translate.dumps(native),
        )

    # ---- helpers ---------------------------------------------------------------

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise NotFoundError(f"no such claude session {session_id}")
        return session

    @staticmethod
    def _info(session: _Session) -> Session:
        tokens = session.tokens
        for usage in session.live_usage.values():
            tokens = translate.add_usage(tokens, usage)
        return Session.model_validate(
            {
                "id": session.id,
                "title": session.title,
                "cost": session.cost,
                "tokens": tokens,
                "claude_session_id": session.sdk_session_id,
            }
        )

    def _emit(self, event: BackendEvent) -> None:
        self._events.put_nowait(event)

    def _spawn(self, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cli_path(self) -> str:
        found = shutil.which(self.cli)
        if found is None:
            raise BackendError(
                f"the claude CLI ({self.cli}) is not on PATH: install Claude Code, "
                "then sign in with `claude auth login`"
            )
        return found

    async def _cli_version(self) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                self._cli_path(),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(process.communicate(), timeout=30)
        except (OSError, TimeoutError) as exc:
            raise BackendError(f"`{self.cli} --version` failed: {exc}") from exc
        if process.returncode != 0:
            raise BackendError(f"`{self.cli} --version` failed: {err.decode(errors='replace')}")
        return out.decode(errors="replace").strip()


def _command(tool_input: object) -> list[str]:
    command = dig_str(tool_input, "command")
    return [command] if command else []


def _attach_tool_results(transcript: list[Message], body: dict[str, Any]) -> None:
    """Attach ``tool_result`` status and output onto matching tool parts in ``transcript``."""
    results: dict[str, tuple[str, str]] = {}
    for block in dig_list(body, "content"):
        if dig_str(block, "type") not in ("tool_result", "server_tool_result"):
            continue
        call_id = dig_str(block, "tool_use_id")
        if not call_id:
            continue
        status = "error" if dig(block, "is_error") is True else "completed"
        results[call_id] = (status, _result_text(dig(block, "content")))
    if not results:
        return
    for message in reversed(transcript):
        for part in message.parts:
            if part.type == "tool" and part.call_id in results:
                status, output = results.pop(part.call_id)
                part.status = status
                part.output = output
        if not results:
            return


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in cast(list[object], content):
            if isinstance(item, str):
                pieces.append(item)
            elif text := dig_str(item, "text"):
                pieces.append(text)
        return "\n".join(pieces)
    return "" if content is None else translate.dumps(content)


def _preview_metadata(tool: str, tool_input: object) -> dict[str, str]:
    """Extract a ``command`` or ``diff`` preview for the dashboard permission card."""
    if command := dig_str(tool_input, "command"):
        return {"command": command}
    diff = _edit_preview(tool, tool_input)
    return {"diff": diff} if diff else {}


def _edit_preview(tool: str, tool_input: object) -> str:
    if tool == "Edit":
        old = dig_str(tool_input, "old_string") or ""
        new = dig_str(tool_input, "new_string") or ""
        if old or new:
            return _unified(old, new)
    if tool == "MultiEdit":
        chunks = [
            _unified(dig_str(e, "old_string") or "", dig_str(e, "new_string") or "")
            for e in dig_list(tool_input, "edits")
        ]
        return "\n---\n".join(c for c in chunks if c)
    if tool == "Write":
        content = dig_str(tool_input, "content") or ""
        if content:
            return "\n".join(f"+{line}" for line in content.splitlines()[:60])
    if tool == "NotebookEdit":
        source = dig_str(tool_input, "new_source") or ""
        if source:
            return "\n".join(f"+{line}" for line in source.splitlines()[:60])
    return ""


def _unified(old: str, new: str) -> str:
    removed = [f"-{line}" for line in old.splitlines()[:30]]
    added = [f"+{line}" for line in new.splitlines()[:30]]
    return "\n".join([*removed, *added])
