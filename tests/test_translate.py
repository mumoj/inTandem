"""Tests for translating OpenCode 1.x bus events into ``BackendEvent``."""

from __future__ import annotations

from intandem.backend.events import EventType
from tests.fakes import event


def test_an_unknown_event_type_is_other_not_an_error() -> None:
    translated = event("lsp.client.diagnostics", sessionID="ses_1")
    assert translated.type is EventType.OTHER
    assert translated.native_type == "lsp.client.diagnostics"
    assert translated.session_id == "ses_1"


def test_a_running_tool_part_carries_its_call_and_a_bounded_detail() -> None:
    part = {
        "type": "tool",
        "callID": "call_1",
        "tool": "bash",
        "state": {"status": "running", "input": {"command": "uv   run\npytest " + "x" * 400}},
    }
    translated = event("message.part.updated", sessionID="ses_1", part=part)
    assert translated.part is not None
    assert translated.part.running
    assert translated.part.call_id == "call_1"
    assert translated.part.detail.startswith("bash (running): uv run pytest x")
    assert len(translated.part.detail) <= len("bash (running): ") + 240


def test_a_finished_tool_part_is_not_running() -> None:
    part = {"type": "tool", "id": "prt_1", "tool": "edit", "state": {"status": "completed"}}
    translated = event("message.part.updated", sessionID="ses_1", part=part)
    assert translated.part is not None
    assert not translated.part.running
    assert translated.part.call_id == "prt_1"


def test_an_abort_is_marked_as_one() -> None:
    error = {"name": "MessageAbortedError", "data": {"message": "aborted"}}
    translated = event("session.error", sessionID="ses_1", error=error)
    assert translated.aborted
    assert translated.error_message == "aborted"


def test_a_new_subagent_brings_its_parent_and_its_own_policy() -> None:
    own = [{"permission": "task", "pattern": "*", "action": "deny"}]
    info = {"id": "ses_2", "parentID": "ses_1", "permission": own}
    translated = event("session.created", sessionID="ses_2", info=info)
    assert (translated.child_id, translated.child_parent_id) == ("ses_2", "ses_1")
    assert translated.child_policy == own


def test_a_retry_status_explains_itself() -> None:
    status = {"type": "retry", "attempt": 2, "message": "rate limited"}
    translated = event("session.status", sessionID="ses_1", status=status)
    assert translated.status == "retry"
    assert translated.retry_detail == "attempt 2: rate limited"


def test_a_permission_request_carries_what_an_alert_needs() -> None:
    translated = event(
        "permission.asked", id="per_1", sessionID="ses_1", permission="edit", patterns=["a.md"]
    )
    assert translated.request_id == "per_1"
    assert (translated.permission, translated.patterns) == ("edit", ["a.md"])
    assert translated.native["properties"]["patterns"] == ["a.md"]


def test_a_question_request_and_non_alert_native_payload() -> None:
    asked = event(
        "question.asked",
        id="que_1",
        sessionID="ses_1",
        questions=[{"header": "DB", "question": ""}],
    )
    assert (asked.request_id, asked.question) == ("que_1", "DB")
    assert asked.native["properties"]["id"] == "que_1"

    delta = event("message.part.delta", sessionID="ses_1", field="text", delta="hi")
    assert delta.native == {}


def test_session_created_without_session_id_still_extracts_child() -> None:
    translated = event("session.created", info={"id": "ses_2", "parentID": "ses_1"})
    assert translated.session_id is None
    assert (translated.child_id, translated.child_parent_id) == ("ses_2", "ses_1")
