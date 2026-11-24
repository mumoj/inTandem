"""Authenticated loopback HTTP API wrapping a task's supervisor.

Both the planner and CLI/dashboard users interact through these endpoints, so
all actions are recorded in the same journal. The bridge does not proxy the
underlying backend directly; it exposes only explicit control operations.

``/ui`` and ``/ui/review`` are the only unauthenticated routes. They serve
static HTML containing no data; the client-side script reads the bearer token
from the URL fragment (which browsers do not send to the server) and calls the
authenticated endpoints.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated

import anyio.to_thread
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from intandem import __version__, worktrees
from intandem.backend.diff import git_diff
from intandem.backend.errors import BackendError, NotFoundError
from intandem.bridge import live
from intandem.bridge.schemas import (
    AnswerIn,
    DiffOut,
    DirectiveIn,
    DirectiveOut,
    FilePatchOut,
    JournalOut,
    LandIn,
    LandInfoOut,
    LandOut,
    MessageOut,
    MessagesOut,
    Ok,
    OpenCodeOut,
    OverviewOut,
    PermissionIn,
    PromptIn,
    ResultOut,
    RetractIn,
    SpecIn,
    SpecOut,
    WaitOut,
)
from intandem.supervisor.state import SpecRecord
from intandem.supervisor.supervisor import (
    CostReport,
    Pending,
    StatusReport,
    Supervisor,
    SupervisorError,
)

MAX_BODY_BYTES = 64 * 1024

_bearer = HTTPBearer(auto_error=False, description="The token from the manifest.")


def get_supervisor(request: Request) -> Supervisor:
    supervisor: Supervisor | None = getattr(request.app.state, "supervisor", None)
    if supervisor is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "supervisor is starting")
    return supervisor


def require_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected: str = request.app.state.token
    given = credentials.credentials if credentials else ""
    # Use constant-time comparison to avoid timing leaks on the bearer token.
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or wrong bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


SupervisorDep = Annotated[Supervisor, Depends(get_supervisor)]


class BodyLimit:
    """ASGI middleware that rejects oversized request bodies before parsing.

    Implemented as raw ASGI rather than ``BaseHTTPMiddleware`` to avoid extra
    task wrapping and request buffering.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            length = Headers(scope=scope).get("content-length")
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                response = JSONResponse(
                    {"detail": f"request body over {MAX_BODY_BYTES} bytes"},
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _spec_out(supervisor: Supervisor, record: SpecRecord) -> SpecOut:
    return SpecOut(
        spec_id=record.spec_id,
        session_id=record.session_id,
        status=record.status,
        result_path=str(supervisor.result_path(record)),
    )


router = APIRouter(dependencies=[Depends(require_token)])


@router.get("/status")
async def read_status(supervisor: SupervisorDep) -> StatusReport:
    return supervisor.status()


@router.get("/overview")
async def read_overview(supervisor: SupervisorDep) -> OverviewOut:
    return await live.overview(supervisor)


@router.get("/live", response_class=StreamingResponse)
async def read_live(
    request: Request,
    supervisor: SupervisorDep,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """Stream server-sent events for overview, journal, messages, diff, and cost updates.

    Pass ``after`` with a previously returned journal offset to resume without
    re-reading older entries.
    """
    return StreamingResponse(
        live.stream(supervisor, request.is_disconnected, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/journal")
def read_journal(  # Sync handler so FastAPI runs blocking file I/O in its threadpool.
    supervisor: SupervisorDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(gt=0, le=1000)] = 200,
) -> JournalOut:
    entries, offset = supervisor.journal.read_from(after, limit)
    return JournalOut(entries=entries, next=offset)


@router.get("/opencode")
async def read_opencode(request: Request) -> OpenCodeOut:
    """Return the OpenCode server URL and credentials for this task, if applicable."""
    login: OpenCodeLogin | None = request.app.state.opencode
    return OpenCodeOut(url=login.url if login else None, password=login.password if login else None)


@router.get("/wait")
async def wait(
    supervisor: SupervisorDep,
    seconds: Annotated[float, Query(alias="timeout", gt=0, le=3600)] = 600,
) -> WaitOut:
    """Wait until the supervisor needs attention or ``timeout`` seconds elapse."""
    attention = await supervisor.wait(seconds)
    return WaitOut(reason=attention.reason, detail=attention.detail, status=supervisor.status())


@router.get("/messages")
async def read_messages(
    supervisor: SupervisorDep, limit: Annotated[int, Query(gt=0, le=500)] = 20
) -> MessagesOut:
    messages = await supervisor.messages(limit)
    record = supervisor.state.current
    assert record is not None  # Guaranteed by supervisor.messages() above.
    return MessagesOut(session_id=record.session_id, messages=[MessageOut.of(m) for m in messages])


@router.get("/pending")
async def read_pending(supervisor: SupervisorDep) -> Pending:
    return await supervisor.pending()


@router.get("/result")
async def read_result(supervisor: SupervisorDep) -> ResultOut:
    record = supervisor.state.current
    path = str(supervisor.result_path(record)) if record else None
    return ResultOut(path=path, result=supervisor.result())


@router.get("/diff")
async def read_diff(supervisor: SupervisorDep) -> DiffOut:
    return DiffOut(files=await supervisor.diff())


@router.get("/diff/file")
async def read_file_diff(
    supervisor: SupervisorDep, path: Annotated[str, Query(min_length=1, max_length=1024)]
) -> FilePatchOut:
    base: str | None = None
    linked = await anyio.to_thread.run_sync(worktrees.worktree_for_dir, supervisor.task_dir)
    if linked is not None:
        base, _ = await anyio.to_thread.run_sync(worktrees.merge_base, linked[0], linked[1])
    try:
        patch, truncated = await supervisor.file_diff(path, base=base)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return FilePatchOut(file=path, patch=patch, truncated=truncated)


@router.get("/land")
async def read_land(supervisor: SupervisorDep) -> LandInfoOut:
    """Inspect whether this task's worktree can be merged into the main repository."""
    linked = await anyio.to_thread.run_sync(worktrees.worktree_for_dir, supervisor.task_dir)
    busy = supervisor.tracker.busy
    if linked is None:
        files_list = (
            await supervisor.diff()
            if supervisor.state.current is not None
            else await git_diff(supervisor.task_dir)
        )
        return LandInfoOut(
            is_worktree=False,
            busy=busy,
            can_merge=False,
            reason="This task is already in the main working tree; no separate branch to merge.",
            files=files_list,
        )
    repo, wt = linked
    target = await anyio.to_thread.run_sync(worktrees.target_branch, repo)
    base, moved = await anyio.to_thread.run_sync(worktrees.merge_base, repo, wt)
    files_list = await git_diff(supervisor.task_dir, base)
    reason = ""
    if busy:
        reason = "Implementer is still working; wait for it to finish or stop the turn first."
    elif not target:
        reason = f"{repo} is on a detached HEAD; check out a branch there before merging."
    elif not files_list:
        reason = "No changes to merge yet."
    return LandInfoOut(
        is_worktree=True,
        worktree_name=wt.name,
        branch=wt.branch,
        target_branch=target,
        repo_root=str(repo),
        repo_head_moved=moved,
        busy=busy,
        can_merge=not reason,
        reason=reason,
        default_message=f"inTandem: {wt.name}",
        files=files_list,
    )


@router.post("/land")
async def land_worktree(body: LandIn, request: Request, supervisor: SupervisorDep) -> LandOut:
    """Merge a finished implementer's linked worktree into the main repo, or discard it."""
    linked = await anyio.to_thread.run_sync(worktrees.worktree_for_dir, supervisor.task_dir)
    if linked is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this task is not in a linked git worktree; nothing to merge or discard",
        )
    repo, wt = linked
    if body.action == "merge" and supervisor.tracker.busy:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{wt.name} is still working; wait for it or stop the turn first",
        )
    await supervisor.capture_evidence()
    on_finish: Callable[[], None] = request.app.state.on_finish
    try:
        if body.action == "discard":
            await anyio.to_thread.run_sync(worktrees.remove, repo, wt)
            supervisor.journal.append("worktree_landed", disposition="discard", branch=wt.branch)
            on_finish()
            return LandOut(
                action="discard",
                name=wt.name,
                branch=wt.branch,
                discarded=True,
                evidence=str(supervisor.paths.root),
            )
        commit_msg = (body.message or "").strip() or f"inTandem: {wt.name}"
        committed = await anyio.to_thread.run_sync(worktrees.commit_all, wt, commit_msg)
        into = await anyio.to_thread.run_sync(worktrees.merge, repo, wt, f"Merge {wt.branch}")
        await anyio.to_thread.run_sync(worktrees.remove, repo, wt)
    except worktrees.GitError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    supervisor.journal.append(
        "worktree_landed", disposition="merge", branch=wt.branch, merged_into=into
    )
    on_finish()
    return LandOut(
        action="merge",
        name=wt.name,
        branch=wt.branch,
        merged_into=into,
        committed_leftovers=committed,
        evidence=str(supervisor.paths.root),
    )


@router.get("/cost")
async def read_cost(supervisor: SupervisorDep) -> CostReport:
    return await supervisor.cost()


@router.post("/spec")
async def submit_spec(body: SpecIn, supervisor: SupervisorDep) -> SpecOut:
    """Submit a spec in a fresh session (idempotent for matching ID, text, model, and agent)."""
    record = await supervisor.submit_spec(
        body.spec_id, body.text, model=body.model, agent=body.agent
    )
    return _spec_out(supervisor, record)


@router.post("/directive")
async def send_directive(body: DirectiveIn, supervisor: SupervisorDep) -> DirectiveOut:
    directive = await supervisor.directive(body.spec_id, body.directive_id, body.action, body.text)
    return DirectiveOut(
        spec_id=body.spec_id,
        directive_id=directive.directive_id,
        action=directive.action,
        retracted=directive.retracted,
    )


@router.post("/retract")
async def retract(body: RetractIn, supervisor: SupervisorDep) -> DirectiveOut:
    directive = await supervisor.retract(body.spec_id, body.directive_id)
    return DirectiveOut(
        spec_id=body.spec_id,
        directive_id=directive.directive_id,
        action=directive.action,
        retracted=directive.retracted,
    )


@router.post("/prompt", status_code=status.HTTP_202_ACCEPTED)
async def prompt(body: PromptIn, supervisor: SupervisorDep) -> SpecOut:
    record = await supervisor.say(body.text)
    return _spec_out(supervisor, record)


@router.post("/permission")
async def reply_permission(body: PermissionIn, supervisor: SupervisorDep) -> Ok:
    await supervisor.reply_permission(body.id, body.reply, body.message)
    return Ok(detail=f"{body.id} {body.reply}")


@router.post("/answer")
async def answer(body: AnswerIn, supervisor: SupervisorDep) -> Ok:
    await supervisor.answer(body.id, None if body.reject else body.answers)
    return Ok(detail=f"{body.id} {'rejected' if body.reject else 'answered'}")


@router.post("/abort")
async def abort(supervisor: SupervisorDep) -> Ok:
    record = await supervisor.abort()
    return Ok(detail=f"aborted the turn on {record.spec_id}; the session stays open")


@router.post("/finish")
async def finish(request: Request) -> Ok:
    """Trigger a clean supervisor shutdown."""
    on_finish: Callable[[], None] = request.app.state.on_finish
    on_finish()
    return Ok(detail="stopping")


@dataclass(frozen=True, slots=True)
class OpenCodeLogin:
    url: str
    password: str


def dashboard_headers() -> dict[str, str]:
    """Security headers for the static dashboard pages."""
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
        ),
    }


def _dashboard() -> HTMLResponse:
    page = files("intandem.bridge").joinpath("dashboard.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers=dashboard_headers())


def _review() -> HTMLResponse:
    page = files("intandem.bridge").joinpath("review.html").read_text(encoding="utf-8")
    return HTMLResponse(page, headers=dashboard_headers())


def _error(status_code: int) -> Callable[[Request, Exception], Awaitable[Response]]:
    async def handler(_request: Request, exc: Exception) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=status_code)

    return handler


def create_app(
    *,
    supervisor: Supervisor | None = None,
    token: str,
    on_finish: Callable[[], None] = lambda: None,
    opencode: OpenCodeLogin | None = None,
) -> FastAPI:
    app = FastAPI(
        title="inTandem bridge",
        version=__version__,
        description="Drive a supervised OpenCode implementer. Every call needs the bearer token.",
    )
    app.state.supervisor = supervisor
    app.state.token = token
    app.state.on_finish = on_finish
    app.state.opencode = opencode
    app.add_middleware(BodyLimit)
    app.add_exception_handler(SupervisorError, _error(status.HTTP_409_CONFLICT))
    # Starlette resolves handlers by exception MRO, so NotFoundError maps to 404
    # despite inheriting from BackendError.
    app.add_exception_handler(BackendError, _error(status.HTTP_502_BAD_GATEWAY))
    app.add_exception_handler(NotFoundError, _error(status.HTTP_404_NOT_FOUND))
    app.include_router(router)
    app.add_api_route("/ui", _dashboard, methods=["GET"], include_in_schema=False)
    app.add_api_route("/ui/review", _review, methods=["GET"], include_in_schema=False)
    return app
