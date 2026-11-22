"""The Claude Code backend, against an in-memory stand-in for the SDK client.

The SDK's own message and result types are real; only ``ClaudeSDKClient`` is
faked, so no CLI runs and nothing leaves the machine. ``test_claude_e2e.py``
covers the real CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from intandem import runtime
from intandem.backend.errors import BackendError, NotFoundError
from intandem.backend.events import BackendEvent, EventType
from intandem.backend.guard import Guard
from intandem.backend.models import ModelRef
from intandem.claude import api_key_warning
from intandem.claude.backend import ClaudeCodeBackend
from intandem.claude.translate import Translator, to_native
from intandem.config import Settings


class FakeSDKClient:
    """``ClaudeSDKClient`` without the CLI: tests push the messages it would read."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self.options = options
        self.queries: list[str] = []
        self.models: list[str | None] = []
        self.interrupts = 0
        self.connected = False
        self.inbox: asyncio.Queue[object | None] = asyncio.Queue()

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.queries.append(prompt)

    async def receive_messages(self) -> AsyncIterator[object]:
        while (message := await self.inbox.get()) is not None:
            yield message

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def set_model(self, model: str | None = None) -> None:
        self.models.append(model)

    async def disconnect(self) -> None:
        self.connected = False

    # -- helpers for tests ------------------------------------------------------

    async def can_use_tool(self, tool: str, tool_input: dict[str, Any]) -> Any:
        assert self.options.can_use_tool is not None
        return await self.options.can_use_tool(
            tool, tool_input, ToolPermissionContext(tool_use_id="toolu_1")
        )

    async def pre_tool_use(self, tool: str, tool_input: dict[str, Any]) -> Any:
        assert self.options.hooks is not None
        [matcher] = self.options.hooks["PreToolUse"]
        [hook] = matcher.hooks
        hook_input: Any = {
            "hook_event_name": "PreToolUse",
            "session_id": "cli",
            "transcript_path": "/dev/null",
            "cwd": str(self.options.cwd),
            "tool_name": tool,
            "tool_input": tool_input,
            "tool_use_id": "toolu_1",
        }
        return await hook(hook_input, "toolu_1", {"signal": None})


def result(**fields: Any) -> ResultMessage:
    base: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 10,
        "duration_api_ms": 8,
        "is_error": False,
        "num_turns": 1,
        "session_id": "cli",
    }
    return ResultMessage(**(base | fields))


def assistant(*blocks: Any, usage: dict[str, Any] | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-sonnet-4-5", usage=usage)


@pytest.fixture
def clients() -> list[FakeSDKClient]:
    return []


@pytest.fixture
def make_backend(task_dir: Path, clients: list[FakeSDKClient]) -> Callable[..., ClaudeCodeBackend]:
    def build(**options: Any) -> ClaudeCodeBackend:
        def factory(sdk_options: ClaudeAgentOptions) -> FakeSDKClient:
            client = FakeSDKClient(sdk_options)
            clients.append(client)
            return client

        # Any executable will do as the CLI: the fake never runs it.
        return ClaudeCodeBackend(task_dir, cli=sys.executable, client_factory=factory, **options)

    return build


@pytest.fixture
async def backend(
    make_backend: Callable[..., ClaudeCodeBackend],
) -> AsyncIterator[ClaudeCodeBackend]:
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
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient], guard: Guard | None = None
) -> tuple[str, FakeSDKClient]:
    """A session with one prompt sent, so its client is connected."""
    session = await backend.create_session("t", guard or Guard())
    await backend.prompt(session.id, "Do the spec.", ModelRef.parse("anthropic/claude-sonnet-4-5"))
    await eventually(lambda: bool(clients) and bool(clients[-1].queries))
    return session.id, clients[-1]


# ---- translation --------------------------------------------------------------


def test_assistant_text_and_tool_use_become_parts_with_a_bounded_detail() -> None:
    message = assistant(
        TextBlock(text="Running the tests."),
        ToolUseBlock(id="toolu_1", name="Bash", input={"command": "uv   run\npytest " + "x" * 400}),
    )
    native = to_native(message)
    text, tool = Translator("ses_1").translate(native)
    assert text.type is EventType.PART_UPDATED
    assert text.part is not None
    assert text.part.kind == "text"
    assert tool.part is not None
    assert tool.part.running
    assert (tool.part.call_id, tool.part.tool) == ("toolu_1", "Bash")
    assert tool.part.detail.startswith("Bash (running): uv run pytest x")
    assert len(tool.part.detail) <= len("Bash (running): ") + 240
    assert text.native["type"] == "assistant"
    assert text.native["content"][1]["type"] == "tool_use"
    assert '"type":"assistant"' in text.raw
    assert tool.session_id == "ses_1"


def test_a_tool_result_finishes_the_call_and_an_edit_reports_a_file_edited() -> None:
    translator = Translator("ses_1")
    use = ToolUseBlock(id="toolu_2", name="Edit", input={"file_path": "/t/a.py"})
    translator.translate(to_native(assistant(use)))
    done = UserMessage(content=[ToolResultBlock(tool_use_id="toolu_2", content="ok")])
    part, edited = translator.translate(to_native(done))
    assert part.part is not None
    assert (part.part.tool, part.part.running) == ("Edit", False)
    assert part.part.detail == "Edit (completed): /t/a.py"
    assert edited.type is EventType.FILE_EDITED


def test_a_failed_tool_result_is_not_an_edit() -> None:
    translator = Translator("ses_1")
    translator.translate(to_native(assistant(ToolUseBlock(id="t", name="Write", input={}))))
    failed = UserMessage(content=[ToolResultBlock(tool_use_id="t", content="no", is_error=True)])
    [part] = translator.translate(to_native(failed))
    assert part.part is not None
    assert part.part.detail == "Write (error)"


def test_a_text_delta_is_a_part_delta() -> None:
    delta = StreamEvent(
        uuid="u",
        session_id="cli",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
    )
    [event] = Translator("ses_1").translate(to_native(delta))
    assert (event.type, event.delta_field) == (EventType.PART_DELTA, "text")


def test_a_result_ends_the_turn_idle_or_with_an_error() -> None:
    idle, done = Translator("ses_1").translate(to_native(result()))
    assert (idle.type, idle.status, done.type) == (EventType.STATUS, "idle", EventType.IDLE)

    failed = result(subtype="error_during_execution", is_error=True, errors=["boom"])
    _, error = Translator("ses_1").translate(to_native(failed))
    assert error.type is EventType.ERROR
    assert (error.error_name, error.error_message, error.aborted) == (
        "error_during_execution",
        "boom",
        False,
    )


def test_an_interrupted_result_is_an_abort() -> None:
    _, error = Translator("ses_1").translate(to_native(result(terminal_reason="aborted_tools")))
    assert error.type is EventType.ERROR
    assert error.aborted


def test_an_unknown_message_is_other_not_an_error() -> None:
    [event] = Translator("ses_1").translate({"type": "system", "subtype": "init", "data": {}})
    assert event.type is EventType.OTHER
    assert event.native_type == "claude.system"


# ---- sessions and turns -------------------------------------------------------


async def test_a_prompt_runs_in_the_background_and_streams_its_turn(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient], task_dir: Path
) -> None:
    stream = backend.events()
    assert (await anext(stream)).type is EventType.CONNECTED
    session_id, client = await started(backend, clients)

    busy = await until(stream, EventType.STATUS)
    assert (busy.session_id, busy.status) == (session_id, "busy")
    assert client.options.cwd == task_dir
    assert client.options.model == "claude-sonnet-4-5"
    assert client.options.permission_mode == "default"
    assert client.queries == ["Do the spec."]
    assert (await backend.session_status())[session_id].type == "busy"

    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}
    await client.inbox.put(assistant(TextBlock(text="Done."), usage=usage))
    await client.inbox.put(result(total_cost_usd=0.25, usage=usage))
    idle = await until(stream, EventType.IDLE)
    assert idle.session_id == session_id

    session = await backend.get_session(session_id)
    assert session.cost == 0.25
    assert session.tokens is not None
    assert (session.tokens.input, session.tokens.output, session.tokens.cache.read) == (100, 20, 5)
    roles = [(m.info.role, m.text()) for m in await backend.messages(session_id)]
    assert roles == [("user", "Do the spec."), ("assistant", "Done.")]
    assert (await backend.session_status())[session_id].type == "idle"


async def test_the_next_prompt_reuses_the_client_and_switches_model(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    session_id, client = await started(backend, clients)
    await backend.prompt(session_id, "Next.", ModelRef.parse("anthropic/claude-opus-4-1"))
    await eventually(lambda: len(client.queries) == 2)
    assert len(clients) == 1
    assert client.models == ["claude-opus-4-1"]


async def test_an_agent_is_rejected(backend: ClaudeCodeBackend) -> None:
    session = await backend.create_session("t", Guard())
    with pytest.raises(BackendError, match="no agents"):
        await backend.prompt(session.id, "hi", None, agent="build")


async def test_a_model_from_another_provider_is_rejected(backend: ClaudeCodeBackend) -> None:
    session = await backend.create_session("t", Guard())
    with pytest.raises(BackendError, match="Anthropic models only"):
        await backend.prompt(session.id, "hi", ModelRef.parse("openai/gpt-5"))


async def test_an_unknown_session_is_not_found(backend: ClaudeCodeBackend) -> None:
    with pytest.raises(NotFoundError):
        await backend.get_session("ses_missing")


async def test_abort_interrupts_the_turn_and_reports_an_abort(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    stream = backend.events()
    session_id, client = await started(backend, clients)
    assert await backend.abort(session_id)
    assert client.interrupts == 1
    await client.inbox.put(result())
    error = await until(stream, EventType.ERROR)
    assert error.aborted
    assert (await backend.session_status())[session_id].type == "idle"


async def test_abort_refuses_what_the_session_waits_on(
    make_backend: Callable[..., ClaudeCodeBackend], clients: list[FakeSDKClient]
) -> None:
    async with make_backend() as backend:
        session_id, client = await started(backend, clients, Guard(unattended=False))
        asking = asyncio.create_task(client.can_use_tool("Bash", {"command": "ls"}))
        await eventually(lambda: bool(backend._permissions))
        await backend.abort(session_id)
        assert isinstance(await asking, PermissionResultDeny)
        assert await backend.permissions() == []


# ---- permissions ----------------------------------------------------------------


async def test_a_permission_is_parked_until_the_planner_replies(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    stream = backend.events()
    session_id, client = await started(backend, clients, Guard(unattended=False))
    asking = asyncio.create_task(client.can_use_tool("Bash", {"command": "rm -rf build"}))

    asked = await until(stream, EventType.PERMISSION_ASKED)
    assert asked.request_id is not None
    assert asked.request_id.startswith("per_")
    assert (asked.permission, asked.patterns) == ("Bash", ["rm -rf build"])
    [request] = await backend.permissions()
    assert (request.id, request.session_id) == (asked.request_id, session_id)
    assert not asking.done()

    assert await backend.reply_permission(asked.request_id, "once")
    assert isinstance(await asking, PermissionResultAllow)
    replied = await until(stream, EventType.PERMISSION_REPLIED)
    assert replied.request_id == asked.request_id
    assert await backend.permissions() == []
    with pytest.raises(NotFoundError):
        await backend.reply_permission(asked.request_id, "once")


async def test_a_rejected_permission_denies_with_the_planners_message(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    _, client = await started(backend, clients, Guard(unattended=False))
    asking = asyncio.create_task(client.can_use_tool("Bash", {"command": "ls"}))
    await eventually(lambda: bool(backend._permissions))
    [request] = await backend.permissions()
    await backend.reply_permission(request.id, "reject", "Use the test runner instead.")
    denied = await asking
    assert isinstance(denied, PermissionResultDeny)
    assert denied.message == "Use the test runner instead."


async def test_always_allows_that_tool_for_the_rest_of_the_session(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    _, client = await started(backend, clients, Guard(unattended=False))
    asking = asyncio.create_task(client.can_use_tool("Bash", {"command": "ls"}))
    await eventually(lambda: bool(backend._permissions))
    [request] = await backend.permissions()
    await backend.reply_permission(request.id, "always")
    assert isinstance(await asking, PermissionResultAllow)

    again = await client.can_use_tool("Bash", {"command": "pwd"})
    assert isinstance(again, PermissionResultAllow)
    assert await backend.permissions() == []
    # Only that tool.
    other = asyncio.create_task(client.can_use_tool("WebFetch", {"url": "https://x"}))
    await eventually(lambda: bool(backend._permissions))
    other.cancel()


async def test_an_unattended_session_allows_routine_tools_at_once(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient], task_dir: Path
) -> None:
    _, client = await started(backend, clients, Guard(protected=("instruction.md",)))
    allowed = await client.can_use_tool("Write", {"file_path": str(task_dir / "src.py")})
    assert isinstance(allowed, PermissionResultAllow)
    assert await backend.permissions() == []


# ---- the guard ------------------------------------------------------------------


async def test_an_edit_to_a_protected_file_waits_for_the_planner(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient], task_dir: Path
) -> None:
    stream = backend.events()
    _, client = await started(backend, clients, Guard(protected=("instruction.md",)))
    target = {"file_path": str(task_dir / "instruction.md"), "old_string": "a", "new_string": "b"}
    hooked = asyncio.create_task(client.pre_tool_use("Edit", target))

    asked = await until(stream, EventType.PERMISSION_ASKED)
    assert (asked.permission, asked.patterns) == ("Edit", ["instruction.md"])
    assert not hooked.done()
    assert asked.request_id is not None
    await backend.reply_permission(asked.request_id, "reject")
    decision = (await hooked)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "instruction.md is protected" in decision["permissionDecisionReason"]

    # can_use_tool, the second line, never auto-allows it either.
    second = asyncio.create_task(client.can_use_tool("Edit", target))
    await eventually(lambda: bool(backend._permissions))
    [request] = await backend.permissions()
    await backend.reply_permission(request.id, "reject")
    assert isinstance(await second, PermissionResultDeny)


async def test_the_guard_ignores_other_files_and_tools(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient], task_dir: Path
) -> None:
    _, client = await started(backend, clients, Guard(protected=("instruction.md", "docs/**")))
    assert await client.pre_tool_use("Edit", {"file_path": str(task_dir / "main.py")}) == {}
    assert await client.pre_tool_use("Read", {"file_path": str(task_dir / "instruction.md")}) == {}
    assert await client.pre_tool_use("Bash", {"command": "cat instruction.md"}) == {}
    assert await backend.permissions() == []


async def test_the_guard_covers_new_files_and_relative_paths(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    stream = backend.events()
    _, client = await started(backend, clients, Guard(protected=("docs/**",)))
    hooked = asyncio.create_task(client.pre_tool_use("Write", {"file_path": "docs/new/a.md"}))
    asked = await until(stream, EventType.PERMISSION_ASKED)
    assert asked.patterns == ["docs/new/a.md"]
    assert asked.request_id is not None
    await backend.reply_permission(asked.request_id, "always")
    assert (await hooked)["hookSpecificOutput"]["permissionDecision"] == "allow"
    # Always covers that tool on that path, and nothing wider.
    assert await client.pre_tool_use("Write", {"file_path": "docs/new/a.md"}) == {}
    widened = asyncio.create_task(client.pre_tool_use("Write", {"file_path": "docs/b.md"}))
    await eventually(lambda: bool(backend._permissions))
    widened.cancel()


async def test_the_guard_hook_fails_closed_and_waits_long(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    _, client = await started(backend, clients)
    assert client.options.hooks is not None
    [matcher] = client.options.hooks["PreToolUse"]
    assert matcher.on_failure == "block"
    assert matcher.timeout is not None
    assert matcher.timeout >= 24 * 3600


async def test_subagents_need_no_guard_of_their_own(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    session_id, _ = await started(backend, clients)
    await backend.guard_subagent(session_id, Guard(protected=("instruction.md",)))


# ---- questions ------------------------------------------------------------------


QUESTION = {
    "questions": [
        {
            "question": "Which test runner?",
            "header": "Runner",
            "options": [{"label": "pytest", "description": ""}, {"label": "unittest"}],
            "multiSelect": False,
        }
    ]
}


async def test_a_question_is_answered_into_the_tool_input(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    stream = backend.events()
    _, client = await started(backend, clients)
    asking = asyncio.create_task(client.can_use_tool("AskUserQuestion", QUESTION))
    asked = await until(stream, EventType.QUESTION_ASKED)
    assert asked.question == "Which test runner?"
    assert asked.request_id is not None
    assert asked.request_id.startswith("que_")
    [request] = await backend.questions()
    assert [o.label for o in request.questions[0].options] == ["pytest", "unittest"]

    await backend.reply_question(asked.request_id, [["pytest"]])
    allowed = await asking
    assert isinstance(allowed, PermissionResultAllow)
    assert allowed.updated_input is not None
    assert allowed.updated_input["answers"] == {"Which test runner?": "pytest"}
    assert (await until(stream, EventType.QUESTION_REPLIED)).request_id == asked.request_id


async def test_a_rejected_question_denies_the_tool(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    stream = backend.events()
    _, client = await started(backend, clients)
    asking = asyncio.create_task(client.can_use_tool("AskUserQuestion", QUESTION))
    asked = await until(stream, EventType.QUESTION_ASKED)
    assert asked.request_id is not None
    await backend.reject_question(asked.request_id)
    assert isinstance(await asking, PermissionResultDeny)
    assert (await until(stream, EventType.QUESTION_REJECTED)).request_id == asked.request_id


# ---- the rest of the seam -------------------------------------------------------


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def test_diff_is_git_since_the_session_started(
    make_backend: Callable[..., ClaudeCodeBackend], task_dir: Path
) -> None:
    git(task_dir, "init", "-q")
    git(task_dir, "add", ".")
    git(task_dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    async with make_backend() as backend:
        session = await backend.create_session("t", Guard())
        (task_dir / "instruction.md").write_text("Make the tests pass.\nAnd more.\n")
        (task_dir / "new.py").write_text("a = 1\nb = 2\n")
        diffs = {d.file: d for d in await backend.diff(session.id)}
    assert diffs["instruction.md"].additions == 1
    assert diffs["instruction.md"].status == "modified"
    assert (diffs["new.py"].additions, diffs["new.py"].status) == (2, "added")


async def test_diff_outside_git_is_empty(backend: ClaudeCodeBackend) -> None:
    session = await backend.create_session("t", Guard())
    assert await backend.diff(session.id) == []


async def test_health_reports_the_cli_version(backend: ClaudeCodeBackend) -> None:
    health = await backend.health()
    assert health.healthy
    assert "Python" in health.version  # The stand-in CLI is the Python interpreter.


async def test_a_missing_cli_is_a_clear_error(
    make_backend: Callable[..., ClaudeCodeBackend],
) -> None:
    async with make_backend() as backend:
        backend.cli = "no-such-claude-binary"
        with pytest.raises(BackendError, match="claude auth login"):
            await backend.health()


def test_without_the_sdk_the_harness_says_what_to_install(
    monkeypatch: pytest.MonkeyPatch, task_dir: Path
) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # import now fails
    with pytest.raises(BackendError, match=r"inTandem\[claude\]"):
        ClaudeCodeBackend(task_dir)


def test_an_api_key_in_the_environment_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert api_key_warning() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    warning = api_key_warning()
    assert warning is not None
    assert "subscription" in warning


async def test_the_factory_builds_a_claude_backend_without_opencode(task_dir: Path) -> None:
    settings = Settings(task_dir=task_dir, harness="claude", claude_bin=sys.executable)
    async with contextlib.AsyncExitStack() as stack:
        backend, login = await runtime.make_backend(stack, settings)
    assert isinstance(backend, ClaudeCodeBackend)
    assert login is None


async def test_a_new_session_stops_the_last_ones_cli(
    backend: ClaudeCodeBackend, clients: list[FakeSDKClient]
) -> None:
    first, client = await started(backend, clients)
    await backend.create_session("next", Guard())
    assert not client.connected
    assert [m.info.role for m in await backend.messages(first)] == ["user"]
    # Prompted again, it resumes the CLI's session rather than starting a new one.
    await backend.prompt(first, "More.", None)
    await eventually(lambda: len(clients) == 2 and bool(clients[1].queries))
    assert clients[1].options.resume == clients[0].options.session_id
    assert clients[1].options.session_id is None
