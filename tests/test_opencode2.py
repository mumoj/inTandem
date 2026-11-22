"""The OpenCode 2 adapter: its HTTP mapping (respx) and its event translation.

The translation tests replay streams recorded from a real 2.3.2 server driven
by the scripted model (``tests/fixtures/opencode2``), so they check the shapes
OpenCode actually sends, not ones guessed from its schema.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx

from intandem.backend.events import BackendEvent, EventType
from intandem.backend.guard import Guard
from intandem.backend.models import ModelRef
from intandem.opencode.client import NotFoundError
from intandem.opencode.server import aprobe_version
from intandem.opencode2.client import OpenCode2Client
from intandem.opencode2.translate import Translator
from intandem.supervisor.activity import Activity, ActivityTracker
from tests.fakes import FakeClock

BASE = "http://127.0.0.1:4096"
WORK = "/work"
FIXTURES = Path(__file__).parent / "fixtures" / "opencode2"


def replay(name: str) -> list[BackendEvent]:
    translator = Translator()
    lines = (FIXTURES / name).read_text().splitlines()
    events = [translator.parse(line.removeprefix("data: ")) for line in lines]
    return [event for event in events if event is not None]


@pytest.fixture
async def client() -> AsyncIterator[OpenCode2Client]:
    async with OpenCode2Client(BASE, "pw", directory=WORK) as opencode:
        yield opencode


# ---- events -----------------------------------------------------------------


def test_a_recorded_turn_reads_as_busy_tools_a_permission_and_idle() -> None:
    events = replay("session.sse")
    kinds = [e.type for e in events if e.type is not EventType.OTHER]
    assert kinds[0] is EventType.CONNECTED
    assert EventType.SESSION_CREATED in kinds
    assert kinds.index(EventType.STATUS) < kinds.index(EventType.PERMISSION_ASKED)
    assert kinds.index(EventType.PERMISSION_ASKED) < kinds.index(EventType.PERMISSION_REPLIED)
    assert kinds[-1] is not EventType.STATUS
    assert EventType.IDLE in kinds

    [asked] = [e for e in events if e.type is EventType.PERMISSION_ASKED]
    assert (asked.permission, asked.patterns) == ("edit", ["instruction.md"])
    assert asked.request_id is not None
    assert asked.request_id.startswith("per_")
    [replied] = [e for e in events if e.type is EventType.PERMISSION_REPLIED]
    assert replied.request_id == asked.request_id


def test_tool_calls_keep_their_name_across_events() -> None:
    tools = [e.part for e in replay("session.sse") if e.part is not None and e.part.kind == "tool"]
    shell = [p for p in tools if p.call_id == "call_4"]
    # Only the first event names the tool; the rest are matched by call id.
    assert {p.tool for p in shell} == {"shell"}
    assert shell[0].running
    assert not shell[-1].running
    assert "echo ran" in shell[-1].detail


def test_a_replayed_turn_drives_the_activity_tracker_to_idle(clock: FakeClock) -> None:
    events = replay("session.sse")
    [created] = [e for e in events if e.type is EventType.SESSION_CREATED]
    tracker = ActivityTracker(clock=clock)
    tracker.track(created.child_id)
    tracker.turn_started()
    for event in events:
        tracker.observe(event)
    assert tracker.classify() is Activity.IDLE
    assert not tracker.pending
    assert not tracker.running_tools


def test_a_question_is_a_form_asked_then_replied() -> None:
    events = replay("question.sse")
    [asked] = [e for e in events if e.type is EventType.QUESTION_ASKED]
    assert asked.question == "Formal or informal greeting?"
    assert asked.request_id is not None
    assert asked.request_id.startswith("frm_")
    [replied] = [e for e in events if e.type is EventType.QUESTION_REPLIED]
    assert replied.request_id == asked.request_id


def test_execution_failures_and_interrupts() -> None:
    translator = Translator()
    failed = translator.translate(
        {
            "type": "session.execution.failed",
            "data": {"sessionID": "ses_1", "error": {"type": "APIError", "message": "quota"}},
        }
    )
    assert failed.type is EventType.ERROR
    assert (failed.error_name, failed.error_message, failed.aborted) == ("APIError", "quota", False)
    stopped = translator.translate(
        {"type": "session.execution.interrupted", "data": {"sessionID": "ses_1", "reason": "user"}}
    )
    assert stopped.type is EventType.ERROR
    assert stopped.aborted
    retry = translator.translate(
        {
            "type": "session.retry.scheduled",
            "data": {"sessionID": "ses_1", "attempt": 2, "error": {"message": "rate limited"}},
        }
    )
    assert (retry.type, retry.status, retry.retry_detail) == (
        EventType.STATUS,
        "retry",
        "attempt 2: rate limited",
    )


def test_garbage_is_not_an_event() -> None:
    translator = Translator()
    assert translator.parse("not json") is None
    assert translator.parse('{"no": "type"}') is None
    assert translator.parse("[]") is None


@respx.mock
async def test_the_stream_keeps_this_directory_and_unlocated_events(
    client: OpenCode2Client,
) -> None:
    mine = {"type": "permission.asked", "location": {"directory": WORK}, "data": {"id": "per_1"}}
    theirs = {"type": "permission.asked", "location": {"directory": "/elsewhere"}, "data": {}}
    unlocated = {"type": "session.execution.started", "data": {"sessionID": "ses_1"}}
    payload = "".join(f"data: {json.dumps(e)}\n\n" for e in (mine, theirs, unlocated))
    respx.get(f"{BASE}/api/event").respond(
        headers={"content-type": "text/event-stream"}, content=payload.encode()
    )
    events = [event async for event in client.events()]
    assert [e.type for e in events] == [EventType.PERMISSION_ASKED, EventType.STATUS]


# ---- HTTP ---------------------------------------------------------------------


@respx.mock
async def test_create_session_sends_location_and_ordered_rules(client: OpenCode2Client) -> None:
    route = respx.post(f"{BASE}/api/session").respond(
        json={"data": {"id": "ses_1", "cost": 0, "permissions": []}}
    )
    session = await client.create_session("inTandem t [S-1]", Guard(protected=("a.md",)))
    assert session.id == "ses_1"
    body = json.loads(route.calls.last.request.content)
    assert body["location"] == {"directory": WORK}
    assert body["metadata"] == {"title": "inTandem t [S-1]"}
    assert body["permissions"][0] == {"action": "edit", "resource": "*", "effect": "allow"}
    assert body["permissions"][-1] == {"action": "edit", "resource": "a.md", "effect": "ask"}


@respx.mock
async def test_a_session_reads_in_neutral_terms(client: OpenCode2Client) -> None:
    tokens = {"input": 5, "output": 2, "reasoning": 0, "cache": {"read": 1, "write": 0}}
    respx.get(f"{BASE}/api/session/ses_2").respond(
        json={"data": {"id": "ses_2", "parentID": "ses_1", "cost": 0.25, "tokens": tokens}}
    )
    session = await client.get_session("ses_2")
    assert (session.parent_id, session.cost) == ("ses_1", 0.25)
    assert session.tokens is not None
    assert session.tokens.cache.read == 1


@respx.mock
async def test_a_missing_session_is_not_found(client: OpenCode2Client) -> None:
    respx.get(f"{BASE}/api/session/ses_x").respond(404, json={"_tag": "SessionNotFoundError"})
    with pytest.raises(NotFoundError):
        await client.get_session("ses_x")


@respx.mock
async def test_prompt_switches_model_and_agent_before_the_text(client: OpenCode2Client) -> None:
    model = respx.post(f"{BASE}/api/session/ses_1/model").respond(204)
    agent = respx.post(f"{BASE}/api/session/ses_1/agent").respond(204)
    prompt = respx.post(f"{BASE}/api/session/ses_1/prompt").respond(json={"data": {}})
    await client.prompt("ses_1", "hello", ModelRef.parse("openai/gpt-5.6-luna"), "build")
    assert json.loads(model.calls.last.request.content) == {
        "model": {"id": "gpt-5.6-luna", "providerID": "openai"}
    }
    assert json.loads(agent.calls.last.request.content) == {"agent": "build"}
    assert json.loads(prompt.calls.last.request.content) == {"text": "hello"}
    order = [call.request.url.path.rsplit("/", 1)[-1] for call in respx.calls]
    assert order == ["model", "agent", "prompt"]


@respx.mock
async def test_a_permission_is_listed_and_answered_through_its_session(
    client: OpenCode2Client,
) -> None:
    files = [{"file": "a.md", "patch": "-x\n+y\n"}]
    request = {
        "id": "per_1",
        "sessionID": "ses_1",
        "action": "edit",
        "resources": ["a.md"],
        "metadata": {"files": files},
    }
    listing = respx.get(f"{BASE}/api/permission/request").respond(
        json={"location": {"directory": WORK}, "data": [request]}
    )
    reply = respx.post(f"{BASE}/api/session/ses_1/permission/per_1/reply").respond(204)

    [pending] = await client.permissions()
    assert listing.calls.last.request.url.params["location[directory]"] == WORK
    assert (pending.session_id, pending.permission, pending.patterns) == ("ses_1", "edit", ["a.md"])
    # Shown by the dashboard as the edit it would make.
    assert pending.metadata["diff"] == "-x\n+y\n"

    assert await client.reply_permission("per_1", "once", "go ahead")
    assert json.loads(reply.calls.last.request.content) == {
        "decision": "once",
        "message": "go ahead",
    }


@respx.mock
async def test_replying_to_an_unlisted_permission_looks_it_up_first(
    client: OpenCode2Client,
) -> None:
    respx.get(f"{BASE}/api/permission/request").respond(json={"data": []})
    with pytest.raises(NotFoundError):
        await client.reply_permission("per_9", "reject")


@respx.mock
async def test_a_form_reads_as_questions_and_takes_answers_by_label(
    client: OpenCode2Client,
) -> None:
    form = json.loads((FIXTURES / "form.json").read_text())
    respx.get(f"{BASE}/api/form").respond(json={"location": {"directory": WORK}, "data": [form]})
    reply = respx.post(f"{BASE}/api/session/{form['sessionID']}/form/{form['id']}/reply").respond(
        204
    )

    [question] = await client.questions()
    assert [q.question for q in question.questions] == [
        "Formal or informal greeting?",
        "Which extras?",
    ]
    assert [q.multiple for q in question.questions] == [False, True]
    assert [o.label for o in question.questions[0].options] == ["Formal", "Informal"]

    await client.reply_question(question.id, [["Informal"], ["Emoji", "Name"]])
    assert json.loads(reply.calls.last.request.content) == {
        "answer": {"q0": "Informal", "q1": ["Emoji", "Name"]}
    }


@respx.mock
async def test_rejecting_a_question_cancels_its_form(client: OpenCode2Client) -> None:
    form = json.loads((FIXTURES / "form.json").read_text())
    respx.get(f"{BASE}/api/form").respond(json={"data": [form]})
    cancel = respx.delete(f"{BASE}/api/session/{form['sessionID']}/form/{form['id']}").respond(204)
    assert await client.reject_question(form["id"])
    assert cancel.called


@respx.mock
async def test_messages_page_back_and_come_out_oldest_first(client: OpenCode2Client) -> None:
    first = {
        "data": [
            {"id": "msg_4", "type": "idle"},
            {"id": "msg_3", "type": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ],
        "cursor": {"next": "c1"},
    }
    second = {
        "data": [
            {
                "id": "msg_2",
                "type": "assistant",
                "content": [{"type": "tool", "name": "edit", "id": "call_1"}],
            },
            {"id": "msg_1", "type": "user", "text": "INTANDEM_SPEC_ID: S-1"},
        ],
        "cursor": {"next": "c2"},
    }
    route = respx.get(f"{BASE}/api/session/ses_1/message").mock(
        side_effect=[
            httpx.Response(200, json=first),
            httpx.Response(200, json=second),
            httpx.Response(200, json={"data": [], "cursor": {}}),
        ]
    )
    messages = await client.messages("ses_1")
    assert [m.info.id for m in messages] == ["msg_1", "msg_2", "msg_3"]
    assert messages[0].text() == "INTANDEM_SPEC_ID: S-1"
    assert messages[1].parts[0].tool == "edit"
    assert route.calls[1].request.url.params["cursor"] == "c1"
    assert route.calls[1].request.url.params["order"] == "desc"


async def test_opencode2_prompt_requires_a_provider_prefix(client: OpenCode2Client) -> None:
    from intandem.opencode.client import OpenCodeError

    with pytest.raises(OpenCodeError, match="provider/model"):
        await client.prompt("ses_1", "hello", ModelRef.parse("gpt-5"))


@respx.mock
async def test_status_lists_the_running_sessions_as_busy(client: OpenCode2Client) -> None:
    respx.get(f"{BASE}/api/session/active").respond(json={"data": {"ses_1": {"type": "running"}}})
    statuses = await client.session_status()
    assert {k: v.type for k, v in statuses.items()} == {"ses_1": "busy"}


@respx.mock
async def test_a_subagent_guard_keeps_its_rules_and_adds_denies(client: OpenCode2Client) -> None:
    own = {"action": "subagent", "resource": "*", "effect": "deny"}
    respx.get(f"{BASE}/api/session/ses_2").respond(
        json={"data": {"id": "ses_2", "permissions": [own]}}
    )
    patch = respx.patch(f"{BASE}/api/session/ses_2").respond(204)
    await client.guard_subagent("ses_2", Guard(protected=("a.md",)))
    assert json.loads(patch.calls.last.request.content) == {
        "permissions": [own, {"action": "edit", "resource": "a.md", "effect": "deny"}]
    }


@respx.mock
async def test_diff_drops_the_patch_text(client: OpenCode2Client) -> None:
    respx.get(f"{BASE}/api/session/ses_1/diff").respond(
        json={
            "data": [
                {"file": "a", "patch": "x", "additions": 2, "deletions": 1, "status": "modified"}
            ]
        }
    )
    [diff] = await client.diff("ses_1")
    assert (diff.file, diff.additions, diff.deletions, diff.status) == ("a", 2, 1, "modified")
    assert "patch" not in diff.model_dump()


@respx.mock
async def test_interrupt_reports_whether_a_turn_stopped(client: OpenCode2Client) -> None:
    respx.post(f"{BASE}/api/session/ses_1/interrupt").respond(json={"interrupted": True})
    assert await client.abort("ses_1")


# ---- which API a server speaks ---------------------------------------------


@respx.mock
async def test_a_1x_server_is_told_by_its_health_route() -> None:
    respx.get(f"{BASE}/global/health").respond(json={"healthy": True, "version": "1.19.6"})
    async with httpx.AsyncClient() as http:
        version = await aprobe_version(http, BASE)
    assert version.major == 1


@respx.mock
async def test_a_2x_server_answers_health_with_html_and_info_with_json() -> None:
    respx.get(f"{BASE}/global/health").respond(
        200, text="<!doctype html>", headers={"content-type": "text/html"}
    )
    respx.get(f"{BASE}/api/info").respond(json={"version": "2.3.2", "pid": 1, "urls": []})
    async with httpx.AsyncClient() as http:
        version = await aprobe_version(http, BASE)
    assert (version.major, version.minor) == (2, 3)
