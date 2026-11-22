"""The Antigravity backend, against an in-memory stand-in for ``agy acp``.

``FakeAgent`` is an ``ACPTransport`` that answers requests the way agy 1.5.14
does, and lets each test push the ``session/update`` notifications and
``session/request_permission`` requests the real agent would send. A few tests
run ``StdioACPTransport`` against a small Python script speaking ACP on
stdio, so the wire itself is covered too. ``test_antigravity_e2e.py`` covers
the real CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from intandem import mcp_server, runtime
from intandem.antigravity import AntigravityBackend
from intandem.antigravity.acp import NotificationHandler, RequestHandler, StdioACPTransport
from intandem.antigravity.guard import edit_paths, preview_metadata, protected_paths
from intandem.antigravity.translate import Translator, token_usage
from intandem.backend.errors import BackendError, NotFoundError
from intandem.backend.events import BackendEvent, EventType
from intandem.backend.guard import Guard
from intandem.backend.models import ModelRef
from intandem.config import Settings

MODES = [
    {"id": "default", "name": "Default"},
    {"id": "accept-edits", "name": "Accept edits"},
    {"id": "auto", "name": "Auto"},
    {"id": "plan", "name": "Plan"},
]
OPTIONS = [
    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]
USAGE = {
    "inputTokens": 11205,
    "outputTokens": 538,
    "cachedReadTokens": 6515,
    "cachedWriteTokens": 0,
    "thoughtTokens": 492,
    "totalTokens": 18258,
}


class FakeAgent:
    """``agy acp`` without the process: it answers requests the way agy does."""

    def __init__(self, cli: str, cwd: Path) -> None:
        self.cli = cli
        self.cwd = cwd
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notified: list[tuple[str, dict[str, Any]]] = []
        self.turns: dict[str, asyncio.Future[Any]] = {}
        self.started = False
        self.closed = False
        self.dead = False
        self._on_notification: NotificationHandler | None = None
        self._on_request: RequestHandler | None = None
        self._sessions = 0

    @property
    def alive(self) -> bool:
        return self.started and not self.closed and not self.dead

    def set_notification_handler(self, handler: NotificationHandler) -> None:
        self._on_notification = handler

    def set_request_handler(self, handler: RequestHandler) -> None:
        self._on_request = handler

    async def start(self) -> None:
        self.started = True

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        if not self.alive:
            raise BackendError("`agy acp` is not running")
        self.requests.append((method, params))
        match method:
            case "initialize":
                return {
                    "protocolVersion": 1,
                    "agentInfo": {"name": "Antigravity ACP Server", "version": "1.5.14"},
                    "agentCapabilities": {"loadSession": True},
                }
            case "session/new":
                self._sessions += 1
                return {
                    "sessionId": f"sess-{self._sessions}",
                    "models": {
                        "availableModels": [{"modelId": "gemini-4-neon-high", "name": "Neon"}],
                        "currentModelId": "gemini-4-neon-high",
                    },
                    "modes": {"currentModeId": "default", "availableModes": MODES},
                    "configOptions": [],
                }
            case "session/prompt":
                turn: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
                self.turns[params["sessionId"]] = turn
                return await turn
            case _:
                return {}

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        self.notified.append((method, params))
        turn = self.turns.get(params["sessionId"]) if method == "session/cancel" else None
        if turn is not None and not turn.done():
            turn.set_result({"stopReason": "cancelled"})

    async def close(self) -> None:
        self.closed = True
        self._fail(BackendError("`agy acp` was closed"))

    # -- helpers for tests ------------------------------------------------------

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def update(self, session_id: str, **update: Any) -> None:
        assert self._on_notification is not None
        self._on_notification("session/update", {"sessionId": session_id, "update": update})

    async def ask(
        self, session_id: str, tool_call: dict[str, Any], options: list[Any] | None = None
    ) -> Any:
        assert self._on_request is not None
        params = {
            "sessionId": session_id,
            "toolCall": tool_call,
            "options": OPTIONS if options is None else options,
        }
        return await self._on_request("session/request_permission", params)

    def finish(self, session_id: str, result: dict[str, Any] | None = None) -> None:
        self.turns[session_id].set_result(result or {"stopReason": "end_turn", "usage": USAGE})

    def crash(self) -> None:
        self.dead = True
        self._fail(BackendError("`agy acp` is not running (exit code 1): boom"))

    def _fail(self, error: BackendError) -> None:
        for turn in self.turns.values():
            if not turn.done():
                turn.set_exception(error)


def edit_call(path: str, **fields: Any) -> dict[str, Any]:
    return {
        "toolCallId": "call-1",
        "name": "write_file",
        "kind": "edit",
        "title": f"Write {path}",
        "status": "pending",
        "locations": [{"path": path}],
        "rawInput": {"TargetFile": path, "CodeContent": "new text\n"},
        **fields,
    }


SHELL_CALL = {
    "toolCallId": "call-2",
    "name": "run_command",
    "kind": "execute",
    "title": "Run pytest",
    "rawInput": {"CommandLine": "uv run pytest -q", "Cwd": "/t"},
}


@pytest.fixture
def agents() -> list[FakeAgent]:
    return []


@pytest.fixture
def make_backend(task_dir: Path, agents: list[FakeAgent]) -> Callable[..., AntigravityBackend]:
    def build(**options: Any) -> AntigravityBackend:
        def factory(cli: str, cwd: Path) -> FakeAgent:
            agent = FakeAgent(cli, cwd)
            agents.append(agent)
            return agent

        return AntigravityBackend(task_dir, transport_factory=factory, **options)

    return build


@pytest.fixture
async def backend(
    make_backend: Callable[..., AntigravityBackend],
) -> AsyncIterator[AntigravityBackend]:
    async with make_backend() as built:
        yield built


async def until(stream: AsyncIterator[BackendEvent], kind: EventType) -> BackendEvent:
    async with asyncio.timeout(2):
        async for event in stream:
            if event.type is kind:
                return event
    raise AssertionError(f"the stream ended before {kind}")


async def eventually(check: Callable[[], bool]) -> None:
    """Poll: the condition is the backend's private state, with no event to wait on."""
    async with asyncio.timeout(2):
        while not check():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def started(
    backend: AntigravityBackend,
    agents: list[FakeAgent],
    guard: Guard | None = None,
    model: str | None = "gemini-4-neon-high",
) -> tuple[str, FakeAgent]:
    """A session with one prompt sent and its turn running."""
    session = await backend.create_session("t", guard or Guard())
    await backend.prompt(session.id, "Do the spec.", ModelRef.parse(model) if model else None)
    await eventually(lambda: bool(agents) and session.id in agents[-1].turns)
    return session.id, agents[-1]


# ---- health -------------------------------------------------------------------


async def test_health_initializes_the_agent_once(
    backend: AntigravityBackend, agents: list[FakeAgent], task_dir: Path
) -> None:
    health = await backend.health()
    assert health.healthy
    assert health.version == "Antigravity ACP Server 1.5.14"
    await backend.health()
    [agent] = agents
    assert agent.methods() == ["initialize"]
    assert agent.requests[0][1] == {"protocolVersion": 1, "clientCapabilities": {}}
    assert agent.cwd == task_dir


async def test_health_fails_once_the_agent_has_died(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    await backend.health()
    agents[0].crash()
    with pytest.raises(BackendError, match="no longer running"):
        await backend.health()


async def test_a_missing_cli_is_a_clear_error(task_dir: Path) -> None:
    async with AntigravityBackend(task_dir, cli="no-such-agy-binary") as backend:
        with pytest.raises(BackendError, match="not on PATH"):
            await backend.health()


async def test_close_stops_the_agent(make_backend: Callable[..., AntigravityBackend]) -> None:
    async with make_backend() as backend:
        await backend.create_session("t", Guard())
    assert backend._transport is not None  # pyright: ignore[reportPrivateUsage]
    assert not backend._transport.alive  # pyright: ignore[reportPrivateUsage]


# ---- sessions and turns ---------------------------------------------------------


async def test_a_session_is_created_in_the_task_directory(
    backend: AntigravityBackend, agents: list[FakeAgent], task_dir: Path
) -> None:
    stream = backend.events()
    assert (await anext(stream)).type is EventType.CONNECTED
    session = await backend.create_session("spec", Guard())
    created = await until(stream, EventType.SESSION_CREATED)
    assert created.session_id == session.id == "sess-1"
    [agent] = agents
    assert agent.requests[-1] == ("session/new", {"cwd": str(task_dir), "mcpServers": []})
    assert (await backend.get_session(session.id)).title == "spec"
    with pytest.raises(NotFoundError):
        await backend.get_session("sess-missing")


async def test_a_prompt_runs_in_the_background_and_streams_its_turn(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents, model="gemini-3.8-flash-low")
    busy = await until(stream, EventType.STATUS)
    assert (busy.session_id, busy.status) == (session_id, "busy")
    assert (await backend.session_status())[session_id].type == "busy"
    assert ("session/set_model", {"sessionId": session_id, "modelId": "gemini-3.8-flash-low"}) in (
        agent.requests
    )
    assert agent.requests[-1] == (
        "session/prompt",
        {"sessionId": session_id, "prompt": [{"type": "text", "text": "Do the spec."}]},
    )

    agent.update(
        session_id, sessionUpdate="agent_thought_chunk", content={"type": "text", "text": "Hm"}
    )
    thought = await until(stream, EventType.PART_DELTA)
    assert thought.part is not None
    assert (thought.part.kind, thought.delta, thought.delta_field) == ("reasoning", "Hm", "text")
    for chunk in ("Do", "ne."):
        agent.update(
            session_id, sessionUpdate="agent_message_chunk", content={"type": "text", "text": chunk}
        )
    text = await until(stream, EventType.PART_DELTA)
    assert text.part is not None
    assert (text.part.kind, text.delta) == ("text", "Do")
    assert text.native_type == "antigravity.agent_message_chunk"
    assert '"sessionUpdate":"agent_message_chunk"' in text.raw

    agent.finish(session_id)
    idle = await until(stream, EventType.IDLE)
    assert idle.session_id == session_id
    assert (await backend.session_status())[session_id].type == "idle"

    session = await backend.get_session(session_id)
    assert session.tokens is not None
    assert (session.tokens.input, session.tokens.output, session.tokens.reasoning) == (
        11205,
        538,
        492,
    )
    assert (session.tokens.cache.read, session.tokens.cache.write) == (6515, 0)
    messages = await backend.messages(session_id)
    assert [(m.info.role, m.text()) for m in messages] == [
        ("user", "Do the spec."),
        ("assistant", "Done."),
    ]
    assert [p.type for p in messages[1].parts] == ["reasoning", "text"]
    assert messages[1].info.tokens is not None
    assert messages[1].info.tokens.output == 538
    assert [m.info.role for m in await backend.messages(session_id, limit=1)] == ["assistant"]


async def test_usage_adds_up_across_turns(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents)
    agent.finish(session_id)
    await until(stream, EventType.IDLE)
    await backend.prompt(session_id, "More.", None)
    await eventually(lambda: not agent.turns[session_id].done())
    agent.finish(session_id)
    await until(stream, EventType.IDLE)
    tokens = (await backend.get_session(session_id)).tokens
    assert tokens is not None
    assert (tokens.input, tokens.reasoning, tokens.cache.read) == (22410, 984, 13030)


@pytest.mark.parametrize(
    ("model", "sent"),
    [
        ("gemini-4-neon-high", None),  # already current: no set_model
        ("claude-sonnet-5-5-high", "claude-sonnet-5-5-high"),
        ("antigravity/gemini-3.8-flash-low", "gemini-3.8-flash-low"),
        ("google/gemini-3.8-flash-low", "gemini-3.8-flash-low"),
    ],
)
async def test_bare_and_prefixed_models_are_accepted(
    backend: AntigravityBackend, agents: list[FakeAgent], model: str, sent: str | None
) -> None:
    _, agent = await started(backend, agents, model=model)
    models = [p["modelId"] for m, p in agent.requests if m == "session/set_model"]
    assert models == ([sent] if sent else [])


async def test_a_model_from_another_provider_is_rejected(backend: AntigravityBackend) -> None:
    session = await backend.create_session("t", Guard())
    with pytest.raises(BackendError, match="agy models"):
        await backend.prompt(session.id, "hi", ModelRef.parse("openai/gpt-5"))
    assert await backend.messages(session.id) == []


async def test_an_agent_switches_the_mode(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    session = await backend.create_session("t", Guard())
    await backend.prompt(session.id, "Plan it.", None, agent="plan")
    await eventually(lambda: bool(agents[-1].turns))
    agent = agents[-1]
    assert ("session/set_mode", {"sessionId": session.id, "modeId": "plan"}) in agent.requests
    agent.finish(session.id)
    await until(stream, EventType.IDLE)
    # The same mode again is not sent again; no agent keeps the current one.
    await backend.prompt(session.id, "Again.", None, agent="plan")
    await eventually(lambda: not agent.turns[session.id].done())
    assert agent.methods().count("session/set_mode") == 1


async def test_an_unknown_mode_is_rejected_with_the_valid_ones(
    backend: AntigravityBackend,
) -> None:
    session = await backend.create_session("t", Guard())
    with pytest.raises(BackendError, match="default, accept-edits, auto, plan"):
        await backend.prompt(session.id, "hi", None, agent="build")


async def test_a_failed_prompt_is_an_error(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents)
    agent.turns[session_id].set_exception(BackendError("session/prompt failed (-32603): quota"))
    error = await until(stream, EventType.ERROR)
    assert not error.aborted
    assert "quota" in error.error_message
    assert (await backend.session_status())[session_id].type == "idle"


async def test_a_crash_mid_turn_is_an_error(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    _, agent = await started(backend, agents)
    agent.crash()
    error = await until(stream, EventType.ERROR)
    assert "exit code 1" in error.error_message
    with pytest.raises(BackendError):
        await backend.health()
    # A new session starts a new agent.
    await backend.create_session("again", Guard())
    assert len(agents) == 2


async def test_a_new_session_closes_the_last_one(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    first, agent = await started(backend, agents)
    second = await backend.create_session("next", Guard())
    assert ("session/cancel", {"sessionId": first}) in agent.notified
    assert ("session/close", {"sessionId": first}) in agent.requests
    assert second.id != first
    # The cancelled turn is kept, but reports nothing: the session is over.
    assert [m.info.role for m in await backend.messages(first)] == ["user", "assistant"]
    assert (await backend.session_status())[first].type == "idle"
    with pytest.raises(BackendError, match="closed"):
        await backend.prompt(first, "More.", None)


async def test_abort_cancels_the_turn(backend: AntigravityBackend, agents: list[FakeAgent]) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents)
    assert await backend.abort(session_id)
    assert agent.notified == [("session/cancel", {"sessionId": session_id})]
    error = await until(stream, EventType.ERROR)
    assert error.aborted
    assert (await backend.session_status())[session_id].type == "idle"
    assert not await backend.abort(session_id)


# ---- translation --------------------------------------------------------------


def test_tool_calls_become_parts_and_a_completed_edit_is_a_file_edited() -> None:
    translator = Translator("s")
    [running] = translator.update(
        {"sessionId": "s", "update": {"sessionUpdate": "tool_call", **edit_call("/t/a.py")}}
    )
    assert running.type is EventType.PART_UPDATED
    assert running.part is not None
    assert (running.part.kind, running.part.tool, running.part.running) == (
        "tool",
        "write_file",
        True,
    )
    assert running.part.detail == "write_file (running): /t/a.py"

    update = {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "completed"}
    part, edited = translator.update({"sessionId": "s", "update": update})
    assert part.part is not None
    assert (part.part.running, part.part.detail) == (False, "write_file (completed): /t/a.py")
    assert edited.type is EventType.FILE_EDITED
    assert edited.native_type == "antigravity.tool_call_update"


def test_a_failed_or_non_edit_tool_is_not_a_file_edited() -> None:
    translator = Translator("s")
    translator.update(
        {"sessionId": "s", "update": {"sessionUpdate": "tool_call", **edit_call("a")}}
    )
    failed = {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "failed"}
    [part] = translator.update({"sessionId": "s", "update": failed})
    assert part.part is not None
    assert part.part.detail.startswith("write_file (error)")

    read = {"sessionUpdate": "tool_call", "toolCallId": "r", "name": "view_file", "kind": "read"}
    translator.update({"sessionId": "s", "update": read})
    done = {"sessionUpdate": "tool_call_update", "toolCallId": "r", "status": "completed"}
    assert [e.type for e in translator.update({"sessionId": "s", "update": done})] == [
        EventType.PART_UPDATED
    ]


def test_a_long_command_detail_is_bounded() -> None:
    call = {**SHELL_CALL, "rawInput": {"CommandLine": "uv   run\npytest " + "x" * 400}}
    [event] = Translator("s").update(
        {"sessionId": "s", "update": {"sessionUpdate": "tool_call", **call}}
    )
    assert event.part is not None
    assert event.part.detail.startswith("run_command (running): uv run pytest x")
    assert len(event.part.detail) <= len("run_command (running): ") + 240


@pytest.mark.parametrize(
    "kind",
    ["usage_update", "session_info_update", "available_commands_update", "config_option_update"],
)
def test_other_updates_are_recorded_not_errors(kind: str) -> None:
    params = {"sessionId": "s", "update": {"sessionUpdate": kind, "used": 1}}
    [event] = Translator("s").update(params)
    assert event.type is EventType.OTHER
    assert event.native_type == f"antigravity.{kind}"
    assert json.loads(event.raw) == params


def test_usage_maps_to_neutral_tokens() -> None:
    tokens = token_usage(USAGE)
    assert (tokens.input, tokens.output, tokens.reasoning) == (11205, 538, 492)
    assert (tokens.cache.read, tokens.cache.write) == (6515, 0)
    assert token_usage(None).input == 0


# ---- the guard ------------------------------------------------------------------


def test_edit_paths_come_from_locations_raw_input_and_args() -> None:
    call = {
        "name": "replace_file_content",
        "locations": [{"path": "/t/a.py"}],
        "rawInput": {"TargetFile": "/t/b.py"},
        "args": {"path": "c.py", "file_path": "/t/a.py"},
    }
    assert edit_paths(call) == ["/t/a.py", "/t/b.py", "c.py"]
    assert edit_paths({"name": "view_file", "locations": [{"path": "/t/a.py"}]}) == []
    assert edit_paths({"kind": "edit", "rawInput": {"Path": "d.py"}}) == ["d.py"]


def test_protected_paths_match_wide(task_dir: Path) -> None:
    (task_dir / "docs").mkdir()
    (task_dir / "docs" / "a.md").write_text("x")
    patterns = ("instruction.md", "docs/**")
    for path, hit in [
        (str(task_dir / "instruction.md"), ["instruction.md"]),
        ("instruction.md", ["instruction.md"]),
        ("docs/a.md", ["docs/a.md"]),
        ("docs/new/b.md", ["docs/new/b.md"]),  # does not exist yet
        ("src/main.py", []),
        ("/elsewhere/instruction.md", []),  # outside the task
    ]:
        assert protected_paths(edit_call(path), task_dir, patterns) == hit, path
    assert protected_paths(edit_call("instruction.md"), task_dir, ()) == []


def test_preview_diff_from_diff_content_or_raw_input() -> None:
    content = [{"type": "diff", "path": "/t/a.py", "oldText": "old\n", "newText": "new\n"}]
    diff = preview_metadata({"kind": "edit", "content": content})["diff"]
    assert diff.splitlines() == ["--- a//t/a.py", "+++ b//t/a.py", "@@ -1 +1 @@", "-old", "+new"]

    chunks = {"ReplacementChunks": [{"TargetContent": "a", "ReplacementContent": "b"}]}
    replaced = preview_metadata({"name": "multi_replace_file_content", "rawInput": chunks})
    assert replaced["diff"].endswith("-a\n+b")

    written = preview_metadata(edit_call("n.py"))["diff"]
    assert written.splitlines()[0] == "--- /dev/null"
    assert written.endswith("+new text")

    assert preview_metadata(SHELL_CALL) == {"command": "uv run pytest -q"}
    assert preview_metadata({"name": "view_file"}) == {}


# ---- permissions ----------------------------------------------------------------


async def test_a_protected_edit_waits_for_the_planner_even_unattended(
    backend: AntigravityBackend, agents: list[FakeAgent], task_dir: Path
) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents, Guard(protected=("instruction.md",)))
    content = [{"type": "diff", "path": "instruction.md", "oldText": "a\n", "newText": "b\n"}]
    call = edit_call(str(task_dir / "instruction.md"), content=content)
    asking = asyncio.create_task(agent.ask(session_id, call))

    asked = await until(stream, EventType.PERMISSION_ASKED)
    assert (asked.permission, asked.patterns) == ("write_file", ["instruction.md"])
    assert asked.session_id == session_id
    [request] = await backend.permissions()
    assert request.metadata["protected"] == ["instruction.md"]
    assert request.metadata["diff"].endswith("-a\n+b")
    assert not asking.done()

    assert asked.request_id is not None
    assert await backend.reply_permission(asked.request_id, "reject", "Leave it.")
    assert await asking == {"outcome": {"outcome": "selected", "optionId": "reject-once"}}
    replied = await until(stream, EventType.PERMISSION_REPLIED)
    assert replied.request_id == asked.request_id
    assert await backend.permissions() == []
    with pytest.raises(NotFoundError):
        await backend.reply_permission(asked.request_id, "once")


async def test_once_allows_one_protected_edit(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    session_id, agent = await started(backend, agents, Guard(protected=("instruction.md",)))
    asking = asyncio.create_task(agent.ask(session_id, edit_call("instruction.md")))
    await eventually(lambda: bool(backend._permissions))  # pyright: ignore[reportPrivateUsage]
    [request] = await backend.permissions()
    await backend.reply_permission(request.id, "once")
    assert await asking == {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    # Once is once: the next edit asks again.
    again = asyncio.create_task(agent.ask(session_id, edit_call("instruction.md")))
    await eventually(lambda: bool(backend._permissions))  # pyright: ignore[reportPrivateUsage]
    again.cancel()


async def test_always_covers_that_tool_on_that_path_only(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    session_id, agent = await started(backend, agents, Guard(protected=("docs/**",)))
    asking = asyncio.create_task(agent.ask(session_id, edit_call("docs/a.md")))
    await eventually(lambda: bool(backend._permissions))  # pyright: ignore[reportPrivateUsage]
    [request] = await backend.permissions()
    await backend.reply_permission(request.id, "always")
    # The agent's own "always" is never picked: it could wave a later protected edit through.
    assert await asking == {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    assert await agent.ask(session_id, edit_call("docs/a.md")) == {
        "outcome": {"outcome": "selected", "optionId": "allow-once"}
    }
    widened = asyncio.create_task(agent.ask(session_id, edit_call("docs/b.md")))
    await eventually(lambda: bool(backend._permissions))  # pyright: ignore[reportPrivateUsage]
    widened.cancel()


async def test_an_unattended_session_allows_routine_tools_at_once(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    session_id, agent = await started(backend, agents, Guard(protected=("instruction.md",)))
    allowed = {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    assert await agent.ask(session_id, edit_call("src.py")) == allowed
    assert await agent.ask(session_id, SHELL_CALL) == allowed
    assert await backend.permissions() == []


async def test_an_attended_session_parks_routine_tools_and_always_remembers(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents, Guard(unattended=False))
    asking = asyncio.create_task(agent.ask(session_id, SHELL_CALL))
    asked = await until(stream, EventType.PERMISSION_ASKED)
    assert (asked.permission, asked.patterns) == ("run_command", ["uv run pytest -q"])
    [request] = await backend.permissions()
    assert request.metadata["command"] == "uv run pytest -q"
    await backend.reply_permission(request.id, "always")
    assert (await asking)["outcome"]["optionId"] == "allow-once"
    assert (await agent.ask(session_id, SHELL_CALL))["outcome"]["optionId"] == "allow-once"


async def test_a_reject_without_a_reject_option_cancels(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    session_id, agent = await started(backend, agents, Guard(unattended=False))
    asking = asyncio.create_task(agent.ask(session_id, SHELL_CALL, options=OPTIONS[:1]))
    await eventually(lambda: bool(backend._permissions))  # pyright: ignore[reportPrivateUsage]
    [request] = await backend.permissions()
    await backend.reply_permission(request.id, "reject")
    assert await asking == {"outcome": {"outcome": "cancelled"}}


async def test_abort_cancels_the_turn_and_its_pending_permissions(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    stream = backend.events()
    session_id, agent = await started(backend, agents, Guard(protected=("instruction.md",)))
    asking = asyncio.create_task(agent.ask(session_id, edit_call("instruction.md")))
    await until(stream, EventType.PERMISSION_ASKED)
    assert await backend.abort(session_id)
    assert await asking == {"outcome": {"outcome": "cancelled"}}
    assert (await until(stream, EventType.PERMISSION_REPLIED)).session_id == session_id
    assert await backend.permissions() == []
    assert (await until(stream, EventType.ERROR)).aborted


async def test_a_permission_for_an_unknown_session_is_cancelled(
    backend: AntigravityBackend, agents: list[FakeAgent]
) -> None:
    await started(backend, agents)
    assert await agents[-1].ask("sess-other", SHELL_CALL) == {"outcome": {"outcome": "cancelled"}}


# ---- the rest of the seam -------------------------------------------------------


def write_text(path: Path, text: str) -> None:
    path.write_text(text)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def test_diff_is_git_since_the_session_started(
    make_backend: Callable[..., AntigravityBackend], task_dir: Path
) -> None:
    git(task_dir, "init", "-q")
    git(task_dir, "add", ".")
    git(task_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    async with make_backend() as backend:
        session = await backend.create_session("t", Guard())
        (task_dir / "instruction.md").write_text("Make the tests pass.\nAnd more.\n")
        (task_dir / "new.py").write_text("a = 1\nb = 2\n")
        diffs = {d.file: d for d in await backend.diff(session.id)}
    assert (diffs["instruction.md"].additions, diffs["instruction.md"].status) == (1, "modified")
    assert (diffs["new.py"].additions, diffs["new.py"].status) == (2, "added")


async def test_questions_and_subagents_are_no_ops(backend: AntigravityBackend) -> None:
    session = await backend.create_session("t", Guard())
    assert await backend.questions() == []
    assert not await backend.reply_question("que_1", [["a"]])
    assert not await backend.reject_question("que_1")
    await backend.guard_subagent(session.id, Guard(protected=("instruction.md",)))
    assert await backend.diff(session.id) == []  # not a git repository


# ---- the stdio transport, against a scripted agent ---------------------------------


SCRIPT = r"""
import json, sys

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    message = json.loads(line)
    method, params = message.get("method"), message.get("params", {})
    if method == "initialize":
        sys.stderr.write("starting\n")
        send({"jsonrpc": "2.0", "id": message["id"],
              "result": {"protocolVersion": 1, "agentInfo": {"name": "Scripted", "version": "9"}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"sessionId": "s1",
              "modes": {"currentModeId": "default", "availableModes": [{"id": "default"}]}}})
    elif method == "session/prompt":
        if params["prompt"][0]["text"] == "crash":
            sys.stderr.write("fatal: out of quota\n")
            sys.exit(3)
        big = "x" * 300_000
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1",
              "update": {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": big}}}})
        send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission",
              "params": {"sessionId": "s1", "options": [
                  {"optionId": "ok", "kind": "allow_once"}],
                  "toolCall": {"toolCallId": "t", "name": "run_command", "kind": "execute",
                               "rawInput": {"CommandLine": "ls"}}}})
        reply = json.loads(sys.stdin.readline())
        assert reply["id"] == "perm-1", reply
        chosen = reply["result"]["outcome"]["optionId"]
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1",
              "update": {"sessionUpdate": "agent_message_chunk",
                         "content": {"type": "text", "text": chosen}}}})
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"stopReason": "end_turn",
              "usage": {"inputTokens": 3, "outputTokens": 2}}})
    elif method == "session/bad":
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "nope"}})
    elif "id" in message:
        send({"jsonrpc": "2.0", "id": message["id"], "result": {}})
"""


@pytest.fixture
def scripted_agy(tmp_path: Path) -> str:
    """An ``agy`` whose ``acp`` subcommand is ``SCRIPT``."""
    script = tmp_path / "agent.py"
    script.write_text(SCRIPT)
    binary = tmp_path / "agy"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n')
    binary.chmod(0o755)
    return str(binary)


async def test_the_stdio_transport_speaks_ndjson_both_ways(
    scripted_agy: str, task_dir: Path
) -> None:
    async with AntigravityBackend(task_dir, cli=scripted_agy) as backend:
        assert (await backend.health()).version == "Scripted 9"
        stream = backend.events()
        session = await backend.create_session("t", Guard())
        await backend.prompt(session.id, "go", None)
        await until(stream, EventType.IDLE)
        messages = await backend.messages(session.id)
        transport = backend._transport  # pyright: ignore[reportPrivateUsage]
        assert isinstance(transport, StdioACPTransport)
        with pytest.raises(BackendError, match="nope"):
            await transport.request("session/bad", {})
    reply = messages[-1].text()
    assert len(reply) == 300_000 + len("ok")  # one line well past asyncio's 64 KiB default
    assert reply.endswith("ok")  # the unattended guard picked allow_once
    assert not transport.alive


async def test_the_stdio_transport_reports_a_crash_with_stderr(
    scripted_agy: str, task_dir: Path
) -> None:
    async with AntigravityBackend(task_dir, cli=scripted_agy) as backend:
        stream = backend.events()
        session = await backend.create_session("t", Guard())
        await backend.prompt(session.id, "crash", None)
        error = await until(stream, EventType.ERROR)
        assert "exit code 3" in error.error_message
        assert "out of quota" in error.error_message
        with pytest.raises(BackendError, match="no longer running"):
            await backend.health()


# ---- wiring -------------------------------------------------------------------


def test_settings_take_the_antigravity_harness(task_dir: Path) -> None:
    settings = Settings(task_dir=task_dir, harness="antigravity", model="gemini-4-neon-high")
    assert (settings.harness, settings.antigravity_bin) == ("antigravity", "agy")


async def test_the_factory_builds_an_antigravity_backend_without_opencode(
    task_dir: Path, scripted_agy: str
) -> None:
    settings = Settings(task_dir=task_dir, harness="antigravity", antigravity_bin=scripted_agy)
    async with contextlib.AsyncExitStack() as stack:
        backend, login, version = await runtime.make_backend(stack, settings)
        assert isinstance(backend, AntigravityBackend)
        assert backend.cli == scripted_agy  # pyright: ignore[reportAttributeAccessIssue]
        assert (await backend.health()).version == "Scripted 9"
    assert (login, version) == (None, None)


def test_serve_passes_the_antigravity_options(
    monkeypatch: pytest.MonkeyPatch, task_dir: Path, tmp_path: Path
) -> None:
    from intandem.cli.main import app

    seen: list[Settings] = []

    async def fake_serve(settings: Settings, **_: Any) -> None:
        seen.append(settings)

    monkeypatch.setattr("intandem.hub.serve", fake_serve)
    result = CliRunner().invoke(
        app,
        [
            "--state-root",
            str(tmp_path / "state"),
            "serve",
            str(task_dir),
            "--harness",
            "antigravity",
            "--antigravity-bin",
            "/opt/agy",
        ],
    )
    assert result.exit_code == 0, result.output
    [settings] = seen
    assert (settings.harness, settings.antigravity_bin) == ("antigravity", "/opt/agy")
    # GitHub Actions makes typer colour its help, splitting option names with escapes.
    help_text = re.sub(r"\x1b\[[0-9;]*m", "", CliRunner().invoke(app, ["serve", "--help"]).output)
    assert "--antigravity-bin" in help_text
    assert "antigravity" in help_text


def test_list_models_for_antigravity_runs_agy_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "agy"
    binary.write_text(
        "#!/bin/sh\n"
        '[ "$1" = models ] || exit 2\n'
        "printf 'Fetching available models...\\r'\n"
        "printf 'gemini-4-neon-high\\tGemini 4 Neon (High)\\n'\n"
        "printf 'gemini-3.8-flash-low\\tGemini 3.8 Flash (Low)\\n\\n'\n"
        "printf 'claude-sonnet-5-5-high\\tClaude Sonnet 5.5 (High)\\n'\n"
    )
    binary.chmod(0o755)
    monkeypatch.setenv("INTANDEM_ANTIGRAVITY_BIN", str(binary))
    out = mcp_server.list_models(harness="antigravity")
    assert out["harness"] == "antigravity"
    assert out["models"] == ["gemini-4-neon-high", "gemini-3.8-flash-low", "claude-sonnet-5-5-high"]
    assert [a["name"] for a in out["agents"]] == ["default", "accept-edits", "auto", "plan"]
    assert out["hint"]


def test_list_models_for_antigravity_reports_a_missing_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    monkeypatch.setenv("INTANDEM_ANTIGRAVITY_BIN", "/no/such/agy")
    with pytest.raises(ToolError, match="could not run"):
        mcp_server.list_models(harness="antigravity")


# ---- the whole stack ------------------------------------------------------------


async def test_full_stack_hub_bridge_and_supervisor_with_antigravity_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from intandem.hub import Hub

    work = (tmp_path / "repo").resolve()
    work.mkdir()
    (work / "instruction.md").write_text("Do not touch.\n")
    git(work, "init", "-q")
    git(work, "add", ".")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    built: list[FakeAgent] = []

    def factory(cli: str, cwd: Path) -> FakeAgent:
        agent = FakeAgent(cli, cwd)
        built.append(agent)
        return agent

    real_init = AntigravityBackend.__init__

    def patched_init(self: AntigravityBackend, cwd: Path, **kwargs: Any) -> None:
        kwargs["transport_factory"] = factory
        real_init(self, cwd, **kwargs)

    monkeypatch.setattr(AntigravityBackend, "__init__", patched_init)

    def no_opencode() -> Any:
        raise AssertionError("OpenCode must not start for an antigravity task")

    hub = Hub(tmp_path / "state", no_opencode)
    await hub.start()
    try:
        manifest = await hub.start_task(
            work,
            harness="antigravity",
            antigravity_bin="agy-test",
            model="antigravity/gemini-3.8-flash-low",
            protect=["instruction.md"],
            poll_interval=0.05,
        )
        assert hub.opencode is None
        assert manifest.opencode_port is None
        [agent] = built
        assert (agent.cli, agent.cwd) == ("agy-test", work)
        headers = {"Authorization": f"Bearer {manifest.token.get_secret_value()}"}
        async with httpx.AsyncClient(base_url=manifest.url, headers=headers, timeout=10) as http:
            assert (await http.get("/opencode")).json()["url"] is None
            spec = await http.post("/spec", json={"spec_id": "S-1", "text": "Write hello.txt."})
            assert spec.status_code == 200
            result_path = Path(spec.json()["result_path"])

            await eventually(lambda: bool(agent.turns))
            [session_id] = agent.turns
            models = [p["modelId"] for m, p in agent.requests if m == "session/set_model"]
            assert models == ["gemini-3.8-flash-low"]

            # 1. A protected edit is parked even though the guard is unattended.
            content = [
                {
                    "type": "diff",
                    "path": str(work / "instruction.md"),
                    "oldText": "Do not touch.\n",
                    "newText": "Touched.\n",
                }
            ]
            call = edit_call(str(work / "instruction.md"), content=content)
            blocked = asyncio.create_task(agent.ask(session_id, call))
            waited = (await http.get("/wait", params={"timeout": 5})).json()
            assert waited["reason"] == "blocked"
            [perm] = (await http.get("/pending")).json()["permissions"]
            assert perm["patterns"] == ["instruction.md"]
            assert perm["metadata"]["diff"].endswith("-Do not touch.\n+Touched.")
            reply = await http.post(
                "/permission",
                json={"id": perm["id"], "reply": "reject", "message": "Leave it alone."},
            )
            assert reply.status_code == 200
            assert (await blocked)["outcome"]["optionId"] == "reject-once"

            # 2. The implementer writes hello.txt and its result file, then ends the turn.
            agent.update(
                session_id, sessionUpdate="tool_call", **edit_call(str(work / "hello.txt"))
            )
            write_text(work / "hello.txt", "hi\n")
            agent.update(
                session_id,
                sessionUpdate="tool_call_update",
                toolCallId="call-1",
                status="completed",
            )
            write_text(
                result_path,
                json.dumps({"spec_id": "S-1", "verdict": "done", "summary": "wrote hello.txt"}),
            )
            agent.update(
                session_id,
                sessionUpdate="agent_message_chunk",
                content={"type": "text", "text": "Done."},
            )
            agent.finish(session_id)

            done = (await http.get("/wait", params={"timeout": 5})).json()
            assert done["reason"] == "result"
            assert done["detail"] == "done"
            assert (work / "instruction.md").read_text() == "Do not touch.\n"

            diff = (await http.get("/diff")).json()["files"]
            assert [f["file"] for f in diff] == ["hello.txt"]
            cost = (await http.get("/cost")).json()
            assert cost["total_tokens"]["input"] == 11205
            assert cost["total_tokens"]["reasoning"] == 492
            assert cost["total_tokens"]["cache"]["read"] == 6515

            assert (await http.post("/finish")).status_code == 200
    finally:
        await hub.close()
    assert hub.opencode is None
