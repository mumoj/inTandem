"""MCP server exposing inTandem's supervisor and worktree controls to a planner.

Once registered (e.g. ``claude mcp add inTandem -- inTandem mcp``), the planner
can start implementers, submit specs, wait for completion, review diffs, and
respond to permission or question prompts. A supervisor is started lazily on
first use for the working directory on this server's hub, shuts down when the
MCP connection closes, and resumes from its state file on the next run.

Multiple implementers can run concurrently via ``spawn_implementer``, each in
its own git worktree and branch, coordinated with ``wait_any`` and
``land_implementer``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import anyio
import anyio.from_thread
import anyio.to_thread
import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from intandem import worktrees
from intandem.backend.errors import BackendError
from intandem.bridge.manifest import Manifest
from intandem.bridge.schemas import (
    DiffOut,
    DirectiveOut,
    MessagesOut,
    Ok,
    OverviewOut,
    ResultOut,
    SpecOut,
    WaitOut,
)
from intandem.cli.client import BridgeClient, CliError
from intandem.config import TaskPaths, default_state_root
from intandem.hub import Hub
from intandem.opencode.host import HostError, SharedHost, reap_orphans
from intandem.opencode.server import resolve_binary
from intandem.supervisor.supervisor import Pending, StatusReport, WaitReason

INSTRUCTIONS = """\
inTandem runs a cheaper implementer model in OpenCode while you plan and review.
You write specs; it writes the code. Do not do its work, and do not take its word.

Loop:
1. submit_spec with one outcome, the files it may touch, and how to check it is done.
   Spec IDs are immutable: a changed requirement is a new ID. Pick the implementer with
   `model` (provider/model) and `agent`; list_models shows what is available. The
   implementer starts on first use in the current project; start_implementer only to
   set defaults or protected files. `harness="claude"` on start_implementer or
   spawn_implementer runs Claude Code instead of OpenCode (models anthropic/<model>);
   `harness="antigravity"` runs the Antigravity CLI, agy (bare agy model IDs; `agent`
   is its mode: default, accept-edits, auto or plan).
2. wait, and act on `reason` (`next_step` says what to do). On `timeout`, wait again.
3. On `result`, review: the result is a claim. Check get_result's diff, read the
   files, run the tests yourself. Then correct (send_directive) or move on (next spec).
4. On `blocked`, get_pending, then reply_permission or answer_question. Never allow an
   edit to a protected file.
5. finish when the task is done.

Give the human the dashboard link from the first reply: it shows the implementer live.
With several implementers, give them the `overview` link instead: one page for all of them.

Parallel work, for independent pieces of one task:
- spawn_implementer once per piece, each with its own name and spec. Each works in its
  own git worktree and branch, so they cannot overwrite each other.
- wait_any instead of wait: it returns as soon as any implementer needs you, and names it.
- Every other tool takes the implementer's name as `workdir`.
- land_implementer: review its diff, then merge it into the current branch or discard it.
  A finished implementer stays ready until it is landed.
"""

NEXT_STEP: dict[WaitReason, str] = {
    WaitReason.RESULT: "Review: get_result, then read the changed files and run the tests.",
    WaitReason.TURN_ENDED: "The turn ended without a result: read get_messages, then "
    "send_directive or say.",
    WaitReason.BLOCKED: "get_pending, then reply_permission or answer_question.",
    WaitReason.REASONING_STALL: "Read get_messages and look for a contradiction in the spec.",
    WaitReason.HUNG: "Check status; abort and re-prompt if it is stuck.",
    WaitReason.ERROR: "Provider error: check status and get_messages; it may need a "
    "different model.",
    WaitReason.PROTECTED_VIOLATION: "A protected file changed and the spec stopped. "
    "Restore the file before the next spec.",
    WaitReason.NO_SPEC: "Nothing to wait for: submit_spec first.",
    WaitReason.STOPPED: "The supervisor is stopping.",
    WaitReason.TIMEOUT: "Still working: wait again.",
}

# MCP clients enforce tool-call timeouts (e.g. 60 s in Codex), so wait returns
# early and the planner retries. Because wait is level-triggered, nothing is missed.
DEFAULT_WAIT = 50.0


@dataclass
class _Connection:
    """State scoped to the active MCP connection."""

    hub: Hub | None = None


_CONNECTION = _Connection()


def _new_opencode() -> SharedHost:
    records = _state_root() / "hosts"
    binary = resolve_binary(os.environ.get("INTANDEM_OPENCODE_BIN", "opencode"))
    host = SharedHost(binary, log_path=records / f"{os.getpid()}.log", records=records)
    # Runs on the main event loop thread, which lives for the full process lifetime.
    host.start(die_with_parent=True)
    return host


@contextlib.asynccontextmanager
async def _lifespan(_server: MCPServer[None]) -> AsyncGenerator[None]:
    """Start the hub when the MCP server starts and shut it down on exit."""
    reap_orphans(_state_root() / "hosts")
    hub = Hub(_state_root(), _new_opencode)
    await hub.start()
    _CONNECTION.hub = hub
    try:
        yield
    finally:
        _CONNECTION.hub = None
        await hub.close()


def _manifests() -> list[Manifest]:
    """Return manifests for all currently running supervisors on this machine."""
    found: list[Manifest] = []
    for path in sorted(_state_root().glob("*/manifest.json")):
        try:
            manifest = Manifest.read(path)
        except (OSError, ValueError):
            continue
        if manifest.alive:
            found.append(manifest)
    return found


server = MCPServer("inTandem", instructions=INSTRUCTIONS, lifespan=_lifespan)

Workdir = Annotated[
    str | None,
    Field(
        description="The project directory, or the name of a spawned implementer. "
        "Default: this project."
    ),
]
Name = Annotated[
    str,
    Field(description="Short implementer name: lower-case letters, digits, dashes."),
]
Harness = Annotated[
    Literal["opencode", "claude", "antigravity"] | None,
    Field(
        description="What runs the implementer: opencode (default), claude for Claude "
        "Code on the user's own `claude` login, or antigravity for the Antigravity CLI "
        "(`agy acp`). claude takes anthropic/<model> and no agent; antigravity takes a "
        "bare agy model ID and an agy mode as `agent`."
    ),
]
REVIEW_CHARS = 40_000
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _state_root() -> Path:
    configured = os.environ.get("INTANDEM_STATE_ROOT")
    return Path(configured).expanduser() if configured else default_state_root()


def _base() -> Path:
    configured = os.environ.get("INTANDEM_WORKDIR")
    return Path(configured) if configured else Path.cwd()


def _task_dir(workdir: str | None) -> Path:
    base = _base()
    path = (base / workdir if workdir else base).expanduser().resolve()
    if path.is_dir():
        return path
    if workdir and worktrees.NAME.fullmatch(workdir):
        # Fall back to looking up a spawned implementer by worktree name.
        with contextlib.suppress(worktrees.GitError):
            repo = worktrees.repo_root(base)
            return worktrees.find_worktree(repo, workdir).path
    raise ToolError(f"no such directory or implementer: {workdir or path}")


def _repo(workdir: str | None) -> Path:
    try:
        return worktrees.repo_root(_task_dir(workdir))
    except worktrees.GitError as exc:
        raise ToolError(str(exc)) from exc


@contextlib.contextmanager
def _git_errors() -> Generator[None, None, None]:
    try:
        yield
    except worktrees.GitError as exc:
        raise ToolError(str(exc)) from exc


def _stop(task_dir: Path, timeout: float = 30.0) -> bool:
    """Stop the supervisor for ``task_dir`` and wait for exit. Returns ``False`` if not running."""
    manifest = _running(task_dir)
    if manifest is None:
        return False
    with contextlib.suppress(CliError), contextlib.closing(BridgeClient(manifest)) as client:
        client.post("/finish", Ok)
    path = TaskPaths.for_task(_state_root(), task_dir).manifest
    deadline = time.monotonic() + timeout
    # Wait for the manifest to be removed, since the hub PID stays alive after a task stops.
    while path.exists() and manifest.alive and time.monotonic() < deadline:
        time.sleep(0.25)
    if path.exists() and manifest.alive:
        raise ToolError(f"the implementer in {task_dir} did not stop within {timeout:.0f}s")
    return True


def _archive_state(task_dir: Path) -> Path | None:
    """Archive a finished implementer's state directory."""
    return worktrees.archive_state(_state_root(), task_dir)


def _running(task_dir: Path) -> Manifest | None:
    path = TaskPaths.for_task(_state_root(), task_dir).manifest
    try:
        manifest = Manifest.read(path)
    except (OSError, ValueError):
        return None
    return manifest if manifest.alive else None


def _spawn(
    task_dir: Path, model: str | None, protect: list[str] | None, harness: str | None = None
) -> Manifest:
    """Start a supervisor for ``task_dir`` on the active hub."""
    hub = _CONNECTION.hub
    if hub is None:
        raise ToolError("the hub is not running: the MCP connection is starting or closing")
    start = functools.partial(
        hub.start_task, task_dir, model=model, protect=protect, harness=harness
    )
    try:
        return anyio.from_thread.run(start)
    except (HostError, BackendError, RuntimeError, ValueError) as exc:
        raise ToolError(f"the implementer failed to start: {exc}") from exc


@contextlib.contextmanager
def _bridge(
    workdir: str | None, *, start: bool = False, harness: str | None = None
) -> Generator[BridgeClient, None, None]:
    """Yield a ``BridgeClient`` for ``workdir``, starting a supervisor if ``start`` is True."""
    task_dir = _task_dir(workdir)
    manifest = _running(task_dir)
    if manifest is None:
        if not start:
            raise ToolError(f"no implementer running in {task_dir}; submit_spec starts one")
        manifest = _spawn(task_dir, None, None, harness)
    client = BridgeClient(manifest)
    try:
        yield client
    except CliError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        client.close()


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _links(manifest: Manifest) -> dict[str, str]:
    """Return the task dashboard URL and, when available, the hub overview URL."""
    links = {"dashboard": manifest.dashboard_url}
    if (hub := _CONNECTION.hub) is not None:
        links["overview"] = hub.dashboard_url
    return links


# ---- starting ---------------------------------------------------------------


@server.tool(annotations=WRITE)
def start_implementer(
    workdir: Workdir = None,
    model: Annotated[
        str | None, Field(description="Default provider/model for specs that do not name one.")
    ] = None,
    protect: Annotated[
        list[str] | None,
        Field(description="Globs, relative to workdir, the implementer must not edit."),
    ] = None,
    harness: Harness = None,
) -> dict[str, Any]:
    """Start the implementer for a project, or report the one already running.

    Optional: submit_spec starts one with OpenCode's defaults. Use this to set a
    default model or protected files; both are fixed until the implementer is
    finished, though each spec can still pick its own model.
    """
    task_dir = _task_dir(workdir)
    manifest = _running(task_dir)
    already = manifest is not None
    if manifest is None:
        manifest = _spawn(task_dir, model, protect, harness)
    note = (
        "Already running; its harness, default model and protected files are unchanged. "
        "Pass `model` per spec, or finish and start again to change them."
        if already and (model or protect or harness)
        else ""
    )
    return {
        "task_dir": str(task_dir),
        "already_running": already,
        "dashboard": manifest.dashboard_url,
        "note": note,
    }


@server.tool(annotations=READ)
def list_models(harness: Harness = None) -> dict[str, Any]:
    """The models and agents a harness can run, for submit_spec's `model` and `agent`.

    A listed model can still be refused by the provider account: a ChatGPT login,
    for one, accepts only some OpenAI models. A spec that fails that way returns
    `error` from wait; resubmit under a new Spec ID with another model.
    """
    chosen = harness or os.environ.get("INTANDEM_HARNESS", "opencode")
    if chosen == "claude":
        return {
            "harness": "claude",
            "models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
            "agents": [],
            "hint": "The claude harness runs Anthropic models on your `claude` CLI login "
            "(bare model names or anthropic/<model>; aliases like sonnet/opus also work). "
            "Leave `agent` unset.",
        }
    if chosen == "antigravity":
        return _antigravity_models()
    host: SharedHost | None = None
    if _CONNECTION.hub is not None:
        try:
            host = anyio.from_thread.run(_CONNECTION.hub.ready_opencode)
        except (HostError, FileNotFoundError, RuntimeError) as exc:
            raise ToolError(f"could not start OpenCode: {exc}") from exc
    try:
        binary = (
            host.binary
            if host is not None
            else resolve_binary(os.environ.get("INTANDEM_OPENCODE_BIN", "opencode"))
        )
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    models = _opencode_lines([binary, "models"])
    agents: list[dict[str, str]]
    if host is not None and host.version is not None and host.version.major == 2:
        # OpenCode 2.x removed `opencode agent list`, so fetch agents from its HTTP API.
        agents = _agents_from(host)
    else:
        agents = []
        for line in _opencode_lines([binary, "agent", "list"]):
            name, _, mode = line.partition(" (")
            if mode.endswith(")") and name.isidentifier():
                agents.append({"name": name, "mode": mode[:-1]})
    return {
        "harness": "opencode",
        "models": [m for m in models if "/" in m],
        "agents": agents,
        "hint": "Primary agents (build, plan) run a spec; subagents are what the "
        "implementer can delegate to itself through its task tool.",
    }


ANTIGRAVITY_MODES = ("default", "accept-edits", "auto", "plan")


def _antigravity_models() -> dict[str, Any]:
    """Run ``agy models`` and return available model IDs along with Antigravity modes."""
    binary = os.environ.get("INTANDEM_ANTIGRAVITY_BIN", "agy")
    try:
        done = subprocess.run(
            [binary, "models"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"could not run {binary}: {exc}") from exc
    if done.returncode != 0:
        raise ToolError(f"{binary} models failed: {done.stderr.strip()[:300]}")
    return {
        "harness": "antigravity",
        "models": parse_antigravity_models(done.stdout),
        "agents": [{"name": mode, "mode": "mode"} for mode in ANTIGRAVITY_MODES],
        "hint": "The antigravity harness runs agy on its own sign-in. Pass a bare model ID "
        "(or antigravity/<model>); `agent` picks agy's mode. Protected edits always stop "
        "for you, but auto and accept-edits may skip agy's asking; the content hash "
        "still catches them.",
    }


def parse_antigravity_models(stdout: str) -> list[str]:
    """Extract model IDs from ``agy models`` output, ignoring any carriage-return spinner prefix."""
    models: list[str] = []
    for line in stdout.splitlines():
        tokens = line.rsplit("\r", 1)[-1].split()
        if tokens and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", tokens[0]) and "-" in tokens[0]:
            models.append(tokens[0])
    return list(dict.fromkeys(models))


def _agents_from(host: SharedHost) -> list[dict[str, str]]:
    """Fetch visible agents from an OpenCode 2 server as ``{name, mode}`` dicts."""
    try:
        response = httpx.get(f"{host.url}/api/agent", auth=("opencode", host.password), timeout=30)
        response.raise_for_status()
        listed: object = response.json().get("data", [])
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise ToolError(f"could not list OpenCode's agents: {exc}") from exc
    agents: list[dict[str, str]] = []
    for item in cast(list[object], listed) if isinstance(listed, list) else []:
        agent = cast(dict[str, object], item) if isinstance(item, dict) else {}
        name, mode = agent.get("id"), agent.get("mode")
        if isinstance(name, str) and isinstance(mode, str) and not agent.get("hidden"):
            agents.append({"name": name, "mode": mode})
    return agents


def _opencode_lines(argv: list[str]) -> list[str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ToolError(f"could not run {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        raise ToolError(f"{' '.join(argv)} failed: {done.stderr.strip()[:300]}")
    return [line for line in done.stdout.splitlines() if line and not line.startswith(" ")]


@server.tool(annotations=READ)
def list_implementers() -> list[dict[str, Any]]:
    """Every implementer running on this machine, one per project or worktree."""
    found: list[dict[str, Any]] = []
    for manifest in _manifests():
        entry: dict[str, Any] = {"task_dir": manifest.task_dir, "dashboard": manifest.dashboard_url}
        with contextlib.suppress(CliError), contextlib.closing(BridgeClient(manifest)) as client:
            status = client.get("/status", StatusReport)
            entry |= {"spec_id": status.spec_id, "activity": status.activity.activity}
        found.append(entry)
    return found


@server.tool(annotations=READ)
def dashboard(workdir: Workdir = None) -> str:
    """The browser link that shows the implementer live, for the human."""
    with _bridge(workdir) as client:
        return client.manifest.dashboard_url


# ---- parallel implementers ----------------------------------------------------


@server.tool(annotations=WRITE)
def spawn_implementer(
    name: Name,
    text: Annotated[str, Field(description="Its spec: one outcome, files, how to check.")],
    model: Annotated[str | None, Field(description="provider/model for this implementer.")] = None,
    agent: Annotated[str | None, Field(description="OpenCode agent, e.g. build.")] = None,
    spec_id: Annotated[
        str, Field(description="Immutable spec id within this implementer.")
    ] = "S-1",
    protect: Annotated[
        list[str] | None, Field(description="Globs this implementer must not edit.")
    ] = None,
    base: Annotated[
        str | None, Field(description="Commit or branch to start from. Default: HEAD.")
    ] = None,
    workdir: Workdir = None,
    harness: Harness = None,
) -> dict[str, Any]:
    """Start one of several implementers, in its own git worktree and branch, on a spec.

    Retrying with the same name is safe: the worktree and supervisor are reused and
    the spec is idempotent. Use the name as `workdir` in every other tool.
    """
    repo = _repo(workdir)
    with _git_errors():
        fresh = all(w.name != name for w in worktrees.list_worktrees(repo))
        worktree = worktrees.create_worktree(repo, _state_root(), name, base)
        if fresh and _running(worktree.path) is None:
            _archive_state(worktree.path)
        dirty = bool(worktrees.git(repo, "status", "--porcelain", "--untracked-files=no").strip())
    manifest = _running(worktree.path) or _spawn(worktree.path, None, protect, harness)
    body: dict[str, Any] = {"spec_id": spec_id, "text": text}
    body |= {key: value for key, value in (("model", model), ("agent", agent)) if value}
    with contextlib.closing(BridgeClient(manifest)) as client:
        try:
            out = client.post("/spec", SpecOut, body)
        except CliError as exc:
            raise ToolError(str(exc)) from exc
    return _dump(out) | {
        "name": name,
        "branch": worktree.branch,
        "worktree": str(worktree.path),
        **_links(manifest),
        "note": "The repository has uncommitted changes to tracked files; this implementer "
        "starts from "
        f"{base or 'HEAD'} without them."
        if dirty
        else "",
    }


class ImplementerState(BaseModel):
    """Status summary for a spawned implementer returned by ``wait_any``."""

    name: str
    state: str
    """``working``, ``stopped``, or a ``WaitReason`` value (e.g. ``result``, ``blocked``)."""
    spec_id: str | None = None
    detail: str = ""
    next_step: str = ""

    @property
    def needs_you(self) -> bool:
        return self.state not in ("working", "stopped")


class WaitAnyOut(BaseModel):
    reason: Literal["ready", "all_stopped", "timeout"]
    ready: list[ImplementerState]
    """All implementers that currently need attention."""
    working: list[str]
    stopped: list[str]


def _observe(worktree: worktrees.Worktree) -> ImplementerState:
    manifest = _running(worktree.path)
    if manifest is None:
        return ImplementerState(name=worktree.name, state="stopped")
    try:
        with contextlib.closing(BridgeClient(manifest, timeout=10)) as client:
            overview = client.get("/overview", OverviewOut)
    except CliError as exc:
        return ImplementerState(name=worktree.name, state="stopped", detail=str(exc))
    reason = overview.attention
    return ImplementerState(
        name=worktree.name,
        state=reason or "working",
        spec_id=overview.status.spec_id,
        detail=overview.attention_detail,
        next_step=NEXT_STEP.get(reason, "") if reason else "",
    )


def _survey(repo: Path, names: list[str] | None) -> list[ImplementerState]:
    with _git_errors():
        spawned = worktrees.list_worktrees(repo)
    if names is not None:
        spawned = [w for w in spawned if w.name in names]
    if not spawned:
        raise ToolError("no spawned implementers; spawn_implementer first")
    return [_observe(worktree) for worktree in spawned]


def _describe(state: ImplementerState) -> str:
    match state.state:
        case "working":
            return f"{state.name} is working on {state.spec_id}"
        case "stopped":
            return f"{state.name} stopped"
        case "result" | "turn_ended":
            return f"{state.name} finished {state.spec_id}: {state.state}"
        case _:
            return f"{state.name} needs you: {state.state}"


@server.tool(annotations=READ)
async def wait_any(
    ctx: Context,
    # Keep timeout as a tool parameter rather than an anyio cancel scope so we can
    # return the observed state when the deadline expires.
    timeout: Annotated[  # noqa: ASYNC109
        float, Field(gt=0, le=3600, description="Seconds to block. Keep under your client's limit.")
    ] = DEFAULT_WAIT,
    names: Annotated[
        list[str] | None, Field(description="Only these implementers. Default: all spawned.")
    ] = None,
    workdir: Workdir = None,
) -> WaitAnyOut:
    """Block until any spawned implementer needs you, or `timeout` passes.

    Each implementer that finishes, blocks or stops is announced as it happens, as a
    progress message on this call. The reply lists every implementer that needs
    attention. Level-triggered: a finished implementer stays in `ready` until it is
    landed or given a new spec.
    """
    repo = _repo(workdir)
    started = anyio.current_time()
    seen: dict[str, str] = {}
    while True:
        states = await anyio.to_thread.run_sync(_survey, repo, names)
        elapsed = anyio.current_time() - started
        for state in states:
            if seen.get(state.name, "working") != state.state:
                message = _describe(state)
                await ctx.report_progress(min(elapsed, timeout), timeout, message)
            seen[state.name] = state.state
        ready = [s for s in states if s.needs_you]
        working = [s.name for s in states if s.state == "working"]
        if ready or not working or elapsed >= timeout:
            return WaitAnyOut(
                reason="ready" if ready else "all_stopped" if not working else "timeout",
                ready=ready,
                working=working,
                stopped=[s.name for s in states if s.state == "stopped"],
            )
        await anyio.sleep(1.0)


@server.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def land_implementer(
    name: Name,
    action: Annotated[
        Literal["review", "merge", "discard"],
        Field(
            description="review: show what merging would bring in. merge: commit its work and "
            "merge it into the repository's current branch. discard: throw its work away."
        ),
    ] = "review",
    message: Annotated[
        str | None, Field(description="Commit message for its work, when merging.")
    ] = None,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Review a spawned implementer's work, then merge it or discard it.

    merge and discard stop its supervisor and remove its worktree and branch. Review
    first: the diff is the evidence, not the implementer's result.
    """
    repo = _repo(workdir)
    with _git_errors():
        worktree = worktrees.find_worktree(repo, name)
        manifest = _running(worktree.path)
        status: StatusReport | None = None
        if manifest is not None:
            with contextlib.suppress(CliError), contextlib.closing(BridgeClient(manifest)) as c:
                status = c.get("/status", StatusReport)
        if action == "review":
            return worktrees.review(repo, worktree, REVIEW_CHARS) | {
                "spec_id": status.spec_id if status else None,
                "result": _dump(status.result) if status and status.result else None,
                "running": manifest is not None,
            }
        if action == "merge" and status is not None and status.activity.busy:
            raise ToolError(f"{name} is still working; wait for it, or abort it first")
        _stop(worktree.path)
        if action == "discard":
            worktrees.remove(repo, worktree)
            return {"name": name, "discarded": True, "evidence": _evidence(worktree)}
        committed = worktrees.commit_all(worktree, message or f"inTandem: {name}")
        into = worktrees.merge(repo, worktree, f"Merge {worktree.branch}")
        worktrees.remove(repo, worktree)
        return {
            "name": name,
            "merged_into": into,
            "committed_leftovers": committed,
            "evidence": _evidence(worktree),
        }


def _evidence(worktree: worktrees.Worktree) -> str | None:
    archived = _archive_state(worktree.path)
    return str(archived) if archived else None


# ---- specs ------------------------------------------------------------------


@server.tool(annotations=WRITE)
def submit_spec(
    spec_id: Annotated[
        str, Field(description="Immutable id, e.g. S-1. A changed spec is a new id.")
    ],
    text: Annotated[
        str, Field(description="The spec: one outcome, files it may touch, how to check.")
    ],
    model: Annotated[
        str | None, Field(description="Model for this spec, e.g. openai/gpt-5.6-luna.")
    ] = None,
    agent: Annotated[
        str | None, Field(description="OpenCode agent to run it, e.g. build. Default: OpenCode's.")
    ] = None,
    workdir: Workdir = None,
    harness: Harness = None,
) -> dict[str, Any]:
    """Hand the implementer a spec in a fresh session, starting the implementer if needed.

    Resending the same id, text, model and agent is a no-op, so retrying is safe.
    """
    body: dict[str, Any] = {"spec_id": spec_id, "text": text}
    body |= {key: value for key, value in (("model", model), ("agent", agent)) if value}
    with _bridge(workdir, start=True, harness=harness) as client:
        out = client.post("/spec", SpecOut, body)
        return _dump(out) | _links(client.manifest)


@server.tool(annotations=READ)
def wait(
    timeout: Annotated[
        float, Field(gt=0, le=3600, description="Seconds to block. Keep under your client's limit.")
    ] = DEFAULT_WAIT,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Block until the implementer needs you, or `timeout` passes. Call again on `timeout`."""
    with _bridge(workdir) as client:
        out = client.get("/wait", WaitOut, {"timeout": timeout}, timeout=timeout + 30)
    return {
        "reason": out.reason,
        "detail": out.detail,
        "next_step": NEXT_STEP.get(out.reason, ""),
        "spec_id": out.status.spec_id,
        "activity": _dump(out.status.activity),
    }


@server.tool(annotations=READ)
def status(workdir: Workdir = None) -> dict[str, Any]:
    """The active spec, its model and agent, and what the implementer is doing now."""
    with _bridge(workdir) as client:
        return _dump(client.get("/status", StatusReport)) | {
            "dashboard": client.manifest.dashboard_url
        }


@server.tool(annotations=READ)
def get_result(workdir: Workdir = None) -> dict[str, Any]:
    """The implementer's result for the active spec, and the files its session changed.

    The result is the implementer's own claim. Read the files and run the checks yourself.
    """
    with _bridge(workdir) as client:
        result = client.get("/result", ResultOut)
        diff = client.get("/diff", DiffOut)
    return {"result": _dump(result)["result"], "changed_files": _dump(diff)["files"]}


@server.tool(annotations=READ)
def get_messages(
    limit: Annotated[int, Field(gt=0, le=50, description="How many recent messages.")] = 3,
    workdir: Workdir = None,
) -> dict[str, Any]:
    """The implementer's last few messages: what it said and which tools it ran."""
    with _bridge(workdir) as client:
        return _dump(client.get("/messages", MessagesOut, {"limit": limit}))


@server.tool(annotations=WRITE)
def send_directive(
    spec_id: Annotated[str, Field(description="The active spec this directive belongs to.")],
    directive_id: Annotated[str, Field(description="Immutable id, e.g. D-1.")],
    text: Annotated[str, Field(description="What to change, or why to stop.")],
    action: Annotated[
        Literal["correct", "stop"],
        Field(description="correct: fix and carry on. stop: abort the turn and wait."),
    ] = "correct",
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Correct the implementer's work on the active spec, or stop it."""
    body = {
        "spec_id": spec_id,
        "directive_id": directive_id,
        "action": "CORRECT" if action == "correct" else "STOP_AND_WAIT",
        "text": text,
    }
    with _bridge(workdir) as client:
        return _dump(client.post("/directive", DirectiveOut, body))


@server.tool(annotations=WRITE)
def say(
    text: Annotated[str, Field(description="A free-form message to the implementer.")],
    workdir: Workdir = None,
) -> dict[str, Any]:
    """Send a free-form message to the active spec's session. Prefer send_directive."""
    with _bridge(workdir) as client:
        return _dump(client.post("/prompt", SpecOut, {"text": text}))


# ---- unblocking -------------------------------------------------------------


@server.tool(annotations=READ)
def get_pending(workdir: Workdir = None) -> dict[str, Any]:
    """Permission requests and questions waiting on a decision, including the diff."""
    with _bridge(workdir) as client:
        return _dump(client.get("/pending", Pending))


@server.tool(annotations=WRITE)
def reply_permission(
    decision: Annotated[
        Literal["allow", "always", "deny"],
        Field(description="allow once, always (rest of the session), or deny."),
    ],
    message: Annotated[
        str | None, Field(description="Why, sent to the implementer. Useful with deny.")
    ] = None,
    request_id: Annotated[
        str | None, Field(description="Which request; needed only when several are pending.")
    ] = None,
    workdir: Workdir = None,
) -> str:
    """Release a permission request. Never allow an edit to a protected file."""
    reply = {"allow": "once", "always": "always", "deny": "reject"}[decision]
    with _bridge(workdir) as client:
        target = request_id or _only(client.get("/pending", Pending).permissions, "permission")
        body = {"id": target, "reply": reply, "message": message}
        return client.post("/permission", Ok, body).detail


@server.tool(annotations=WRITE)
def answer_question(
    answers: Annotated[
        list[list[str]] | None,
        Field(description="Chosen option labels, one list per question asked."),
    ] = None,
    reject: Annotated[bool, Field(description="Decline the question instead.")] = False,
    request_id: Annotated[
        str | None, Field(description="Which question; needed only when several are pending.")
    ] = None,
    workdir: Workdir = None,
) -> str:
    """Answer the implementer's question by option labels, or decline it."""
    if reject == bool(answers):
        raise ToolError("give either answers or reject=true")
    with _bridge(workdir) as client:
        target = request_id or _only(client.get("/pending", Pending).questions, "question")
        body: dict[str, Any] = {"id": target}
        body |= {"reject": True} if reject else {"answers": answers}
        return client.post("/answer", Ok, body).detail


def _only(requests: list[Any], kind: str) -> str:
    if len(requests) == 1:
        return str(requests[0].id)
    if not requests:
        raise ToolError(f"no {kind} is pending")
    ids = ", ".join(str(r.id) for r in requests)
    raise ToolError(f"{len(requests)} {kind}s pending ({ids}); pass request_id")


@server.tool(annotations=WRITE)
def abort(workdir: Workdir = None) -> str:
    """Stop the implementer's current turn. The session stays open."""
    with _bridge(workdir) as client:
        return client.post("/abort", Ok).detail


@server.tool(annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False))
def finish(workdir: Workdir = None) -> str:
    """Stop the implementer for this project when the task is done. Evidence is kept."""
    with _bridge(workdir) as client:
        return client.post("/finish", Ok).detail


def main() -> None:
    # Suppress per-request INFO logs from httpx so they do not flood MCP stderr logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
