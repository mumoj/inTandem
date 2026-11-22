"""The bridge over a real supervisor and a fake OpenCode, in process via ASGI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from intandem.bridge.app import OpenCodeLogin, create_app
from intandem.supervisor.supervisor import Supervisor
from tests.fakes import FakeOpenCode

TOKEN = "t0ken"


@pytest.fixture
async def http(supervisor: Supervisor) -> AsyncIterator[httpx.AsyncClient]:
    finished: list[bool] = []
    app = create_app(supervisor=supervisor, token=TOKEN, on_finish=lambda: finished.append(True))
    app.state.finished = finished
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://bridge",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


async def test_every_route_needs_the_token(http: httpx.AsyncClient) -> None:
    del http.headers["Authorization"]
    assert (await http.get("/status")).status_code == 401
    http.headers["Authorization"] = "Bearer wrong"
    assert (await http.post("/abort")).status_code == 401


async def test_spec_lifecycle(http: httpx.AsyncClient, api: FakeOpenCode) -> None:
    created = await http.post("/spec", json={"spec_id": "S-1", "text": "Add a greeting."})
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "active"
    assert body["result_path"].endswith(".json")

    status = (await http.get("/status")).json()
    assert status["spec_id"] == "S-1"
    assert status["activity"]["activity"] == "working"

    changed = await http.post("/spec", json={"spec_id": "S-1", "text": "Something else."})
    assert changed.status_code == 409
    assert "immutable" in changed.json()["detail"]

    said = await http.post("/prompt", json={"text": "also add tests"})
    assert said.status_code == 202
    assert api.prompts(body["session_id"])[-1] == "also add tests"

    messages = (await http.get("/messages", params={"limit": 1})).json()
    assert messages["messages"] == [
        {"role": "user", "text": "also add tests", "tools": [], "tool_calls": []}
    ]


async def test_ids_are_validated_before_any_handler(http: httpx.AsyncClient) -> None:
    bad_spec = await http.post("/spec", json={"spec_id": "../etc", "text": "x"})
    assert bad_spec.status_code == 422
    blank = await http.post("/spec", json={"spec_id": "S-1", "text": "   "})
    assert blank.status_code == 422
    bad_permission = await http.post("/permission", json={"id": "per_1/../x", "reply": "once"})
    assert bad_permission.status_code == 422
    bad_reply = await http.post("/permission", json={"id": "per_1", "reply": "sometimes"})
    assert bad_reply.status_code == 422
    both = await http.post("/answer", json={"id": "que_1", "answers": [["a"]], "reject": True})
    assert both.status_code == 422


async def test_directives_through_the_bridge(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "Add a greeting."})
    sent = await http.post(
        "/directive",
        json={"spec_id": "S-1", "directive_id": "D-1", "action": "CORRECT", "text": "French."},
    )
    assert sent.json() == {
        "spec_id": "S-1",
        "directive_id": "D-1",
        "action": "CORRECT",
        "retracted": False,
    }
    stale = await http.post(
        "/directive",
        json={"spec_id": "S-0", "directive_id": "D-2", "action": "CORRECT", "text": "x"},
    )
    assert stale.status_code == 409
    retracted = await http.post("/retract", json={"spec_id": "S-1", "directive_id": "D-1"})
    assert retracted.json()["retracted"] is True


async def test_releasing_a_blocked_session(http: httpx.AsyncClient, api: FakeOpenCode) -> None:
    body = (await http.post("/spec", json={"spec_id": "S-1", "text": "x"})).json()
    request_id = api.ask_permission(body["session_id"])

    pending = (await http.get("/pending")).json()
    assert [p["id"] for p in pending["permissions"]] == [request_id]

    reply = await http.post("/permission", json={"id": request_id, "reply": "once"})
    assert reply.json() == {"ok": True, "detail": f"{request_id} once"}

    gone = await http.post("/permission", json={"id": request_id, "reply": "once"})
    assert gone.status_code == 404


async def test_wait_returns_the_reason(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "x"})
    waited = (await http.get("/wait", params={"timeout": 0.05})).json()
    assert waited["reason"] == "timeout"
    assert waited["status"]["spec_id"] == "S-1"


async def test_reads_without_a_spec_are_conflicts(http: httpx.AsyncClient) -> None:
    for path in ("/messages", "/diff"):
        response = await http.get(path)
        assert response.status_code == 409
        assert "no active spec" in response.json()["detail"]
    assert (await http.get("/result")).json() == {"path": None, "result": None}


async def test_cost_diff_and_finish(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "x"})
    cost = (await http.get("/cost")).json()
    assert cost["specs"][0]["spec_id"] == "S-1"
    diff = (await http.get("/diff")).json()
    assert diff["files"][0]["file"] == "hello.txt"

    finished = await http.post("/finish")
    assert finished.json()["detail"] == "stopping"


async def test_oversized_bodies_are_refused(http: httpx.AsyncClient) -> None:
    response = await http.post("/prompt", content=b"x" * 70_000)
    assert response.status_code == 413


async def test_the_dashboard_page_is_public_but_holds_no_data(http: httpx.AsyncClient) -> None:
    del http.headers["Authorization"]
    page = await http.get("/ui")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "connect-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["referrer-policy"] == "no-referrer"
    assert TOKEN not in page.text
    for path in ("/overview", "/journal", "/opencode"):
        assert (await http.get(path)).status_code == 401


async def test_overview_says_what_wait_would(http: httpx.AsyncClient, api: FakeOpenCode) -> None:
    empty = (await http.get("/overview")).json()
    assert empty["attention"] == "no_spec"

    await http.post("/spec", json={"spec_id": "S-1", "text": "Add a greeting.", "agent": "plan"})
    overview = (await http.get("/overview")).json()
    assert overview["attention"] is None
    assert overview["status"]["agent"] == "plan"
    assert overview["pending"] == {"permissions": [], "questions": []}


async def test_journal_pages_from_an_offset(http: httpx.AsyncClient) -> None:
    await http.post("/spec", json={"spec_id": "S-1", "text": "Add a greeting."})
    first = (await http.get("/journal")).json()
    assert "session_created" in [e["action"] for e in first["entries"]]
    again = (await http.get("/journal", params={"after": first["next"]})).json()
    assert again == {"entries": [], "next": first["next"]}


async def test_a_bad_model_is_refused_before_the_supervisor(http: httpx.AsyncClient) -> None:
    response = await http.post("/spec", json={"spec_id": "S-1", "text": "x", "model": "/nomodel"})
    assert response.status_code == 422
    spaced = await http.post("/spec", json={"spec_id": "S-1", "text": "x", "model": "bad model"})
    assert spaced.status_code == 422


async def test_opencode_login_is_served_to_token_holders(supervisor: Supervisor) -> None:
    app = create_app(
        supervisor=supervisor, token=TOKEN, opencode=OpenCodeLogin("http://127.0.0.1:9", "pw")
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bridge",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        login = (await client.get("/opencode")).json()
    assert login == {"url": "http://127.0.0.1:9", "user": "opencode", "password": "pw"}


async def test_review_page_is_public_and_messages_expose_full_tool_calls(
    http: httpx.AsyncClient, api: FakeOpenCode
) -> None:
    from intandem.backend.models import Message, MessageInfo, Part

    page = await http.get("/ui/review", headers={})
    assert page.status_code == 200
    assert TOKEN not in page.text
    assert "Review &amp; Merge" in page.text

    spec = (await http.post("/spec", json={"spec_id": "S-1", "text": "Run tests."})).json()
    api.sessions[spec["session_id"]].extra_messages.append(
        Message(
            info=MessageInfo(id="m_tool", role="assistant"),
            parts=[
                Part(type="text", text="Checking."),
                Part(
                    type="tool",
                    tool="Bash",
                    call_id="call_1",
                    status="completed",
                    input={"command": "pytest -q"},
                    output="1 passed\n",
                ),
            ],
        )
    )
    msgs = (await http.get("/messages")).json()["messages"]
    last = msgs[-1]
    assert last["tools"] == ["Bash"]
    assert last["tool_calls"] == [
        {
            "tool": "Bash",
            "call_id": "call_1",
            "status": "completed",
            "summary": "pytest -q",
            "input": {"command": "pytest -q"},
            "output": "1 passed\n",
        }
    ]


def _init_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    from intandem import worktrees

    repo = tmp_path / "repo"
    repo.mkdir()
    worktrees.git(repo, "init", "-q", "-b", "main")
    # Landing commits in the worktree, so it needs an identity: CI runners have none.
    worktrees.git(repo, "config", "user.name", "t")
    worktrees.git(repo, "config", "user.email", "t@t")
    (repo / "README.md").write_text("hello\n")
    worktrees.git(repo, "add", ".")
    worktrees.git(repo, "commit", "-qm", "init")
    wt = worktrees.create_worktree(repo, tmp_path / "state", "worker-a", None)
    (wt.path / "feature.py").write_text("x = 1\n")
    return repo, wt.path


def _read_text(path: Path) -> str:
    return path.read_text()


async def test_land_endpoint_reviews_and_merges_linked_worktree(tmp_path: Path) -> None:
    from intandem.config import TaskPaths
    from intandem.notify import Notifier
    from intandem.supervisor.activity import ActivityTracker
    from intandem.supervisor.journal import Journal
    from intandem.supervisor.protected import ProtectedFiles
    from intandem.supervisor.state import StateStore

    repo, wt_path = _init_linked_worktree(tmp_path)

    paths = TaskPaths.for_task(tmp_path / "state", wt_path)
    paths.ensure()
    api = FakeOpenCode()
    sup = Supervisor(
        api=api,
        paths=paths,
        task_dir=wt_path,
        store=StateStore(paths.state, wt_path),
        journal=Journal(paths.journal),
        notifier=Notifier(log_path=paths.notifications, task="worker-a", state_dir=paths.root),
        tracker=ActivityTracker(),
        protected=ProtectedFiles(wt_path, ()),
    )
    await sup.start()
    finished = False

    def mark_finished() -> None:
        nonlocal finished
        finished = True

    app = create_app(supervisor=sup, token=TOKEN, on_finish=mark_finished)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bridge",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        info = (await client.get("/land")).json()
        assert info["is_worktree"] is True
        assert info["branch"] == "intandem/worker-a"
        assert info["target_branch"] == "main"
        assert info["can_merge"] is True
        assert [f["file"] for f in info["files"]] == ["feature.py"]

        patch = (await client.get("/diff/file", params={"path": "feature.py"})).json()
        assert "+x = 1" in patch["patch"]

        # Refuses merge while busy.
        sup.tracker.busy = True
        assert (await client.post("/land", json={"action": "merge"})).status_code == 409
        sup.tracker.busy = False

        landed = await client.post("/land", json={"action": "merge", "message": "Add feature"})
        assert landed.status_code == 200
        body = landed.json()
        assert body["merged_into"] == "main"
        assert body["committed_leftovers"] is True
    assert finished is True
    assert _read_text(repo / "feature.py") == "x = 1\n"
    assert not wt_path.exists()
