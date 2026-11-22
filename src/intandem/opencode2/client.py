"""A typed async client for the OpenCode 2.x server API.

What changed from 1.x, as far as inTandem is concerned:

* Routes live under ``/api``. Most answers wrap their payload in ``data``.
* A request names its directory as ``location[directory]``, and a session
  remembers the location it was created in.
* A prompt carries only text: the model and the agent are switched on the
  session first.
* Questions are forms. A question's options are a field's options, and an
  answer is keyed by field.
* Replying to a permission or a form needs its session as well as its id, so
  the client remembers which session each pending request belongs to.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from types import TracebackType
from typing import Any, Self, cast

import httpx
from httpx_sse import aconnect_sse

from intandem.backend.events import BackendEvent
from intandem.backend.guard import Guard
from intandem.backend.models import (
    FileDiff,
    Health,
    Message,
    MessageInfo,
    ModelRef,
    Part,
    PermissionReply,
    PermissionRequest,
    QuestionInfo,
    QuestionOption,
    QuestionRequest,
    Session,
    SessionStatus,
    TokenUsage,
)
from intandem.opencode.client import NotFoundError, OpenCodeError
from intandem.opencode.jsonpath import dig, dig_list, dig_str
from intandem.opencode2.rules import guard_to_rules, subagent_rules
from intandem.opencode2.translate import Translator

_CHAT = frozenset({"user", "assistant"})
_MAX_PAGES = 50


class OpenCode2Client:
    """HTTP access to one OpenCode 2 ``opencode serve``, scoped to one directory."""

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        directory: str,
        timeout: float = 60.0,
        max_connections: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.directory = directory
        self._location = {"location[directory]": directory}
        # 2.x always names its user "opencode"; the username is not configurable.
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=("opencode", password),
            timeout=httpx.Timeout(timeout, connect=5.0),
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
        )
        self._request_sessions: dict[str, str] = {}
        self._forms: dict[str, dict[str, Any]] = {}

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
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, path, json=json, params=params)
        except httpx.HTTPError as exc:
            raise OpenCodeError(method, path, None, f"{type(exc).__name__}: {exc}") from exc
        if response.is_error:
            error = NotFoundError if response.status_code == 404 else OpenCodeError
            raise error(method, path, response.status_code, response.text[:300])
        return response

    async def _data(self, method: str, path: str, **kwargs: Any) -> object:
        return dig((await self._request(method, path, **kwargs)).json(), "data")

    # -- the protocol -------------------------------------------------------

    async def health(self) -> Health:
        info = (await self._request("GET", "/api/info")).json()
        return Health(healthy=True, version=str(dig(info, "version")))

    async def create_session(self, title: str, guard: Guard) -> Session:
        body = {
            "location": {"directory": self.directory},
            "permissions": guard_to_rules(guard),
            "metadata": {"title": title},
        }
        return _session(await self._data("POST", "/api/session", json=body))

    async def get_session(self, session_id: str) -> Session:
        return _session(await self._data("GET", f"/api/session/{session_id}"))

    async def guard_subagent(self, session_id: str, guard: Guard, inherited: object = None) -> None:
        if inherited is None:
            inherited = dig(await self._data("GET", f"/api/session/{session_id}"), "permissions")
        body = {"permissions": subagent_rules(inherited, guard)}
        await self._request("PATCH", f"/api/session/{session_id}", json=body)

    async def session_status(self) -> dict[str, SessionStatus]:
        """Busy sessions only: 2.x lists the running ones, and the rest are idle."""
        active = await self._data("GET", "/api/session/active", params=self._location)
        running = cast(dict[str, object], active) if isinstance(active, dict) else {}
        return {session_id: SessionStatus(type="busy") for session_id in running}

    async def messages(self, session_id: str, limit: int | None = None) -> list[Message]:
        """User and assistant messages, oldest first. ``limit`` keeps the newest."""
        found: list[dict[str, Any]] = []
        params: dict[str, Any] = {"order": "desc"}
        for _ in range(_MAX_PAGES):
            response = await self._request(
                "GET", f"/api/session/{session_id}/message", params=params
            )
            body = response.json()
            page = dig_list(body, "data")
            found += [_mapping(m) for m in page if dig_str(m, "type") in _CHAT]
            cursor = dig_str(body, "cursor", "next")
            if not page or not cursor or (limit and len(found) >= limit):
                break
            params = {"cursor": cursor}
        if limit:
            found = found[:limit]
        return [_message(m) for m in reversed(found)]

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None:
        """Queue a prompt and return at once, switching model and agent first."""
        base = f"/api/session/{session_id}"
        if model is not None:
            ref = {"id": model.model_id, "providerID": model.provider_id}
            await self._request("POST", f"{base}/model", json={"model": ref})
        if agent is not None:
            await self._request("POST", f"{base}/agent", json={"agent": agent})
        await self._request("POST", f"{base}/prompt", json={"text": text})

    async def abort(self, session_id: str) -> bool:
        response = await self._request("POST", f"/api/session/{session_id}/interrupt")
        return bool(dig(response.json(), "interrupted"))

    async def permissions(self) -> list[PermissionRequest]:
        requests = await self._data("GET", "/api/permission/request", params=self._location)
        out: list[PermissionRequest] = []
        for item in _items(requests):
            request = _permission(item)
            self._request_sessions[request.id] = request.session_id
            out.append(request)
        return out

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool:
        session_id = await self._session_of(request_id, questions=False)
        body: dict[str, Any] = {"decision": reply}
        if message:
            body["message"] = message
        await self._request(
            "POST", f"/api/session/{session_id}/permission/{request_id}/reply", json=body
        )
        self._request_sessions.pop(request_id, None)
        return True

    async def questions(self) -> list[QuestionRequest]:
        forms = await self._data("GET", "/api/form", params=self._location)
        out: list[QuestionRequest] = []
        for item in _items(forms):
            form = _mapping(item)
            if not dig_str(form, "id"):
                continue
            self._forms[form["id"]] = form
            self._request_sessions[form["id"]] = str(form.get("sessionID", ""))
            out.append(_question(form))
        return out

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        """Answer a form by option labels, one list per field, as 1.x questions were."""
        session_id = await self._session_of(request_id, questions=True)
        answer = _answer(self._forms.get(request_id, {}), answers)
        await self._request(
            "POST",
            f"/api/session/{session_id}/form/{request_id}/reply",
            json={"answer": answer},
        )
        self._forget_form(request_id)
        return True

    async def reject_question(self, request_id: str) -> bool:
        session_id = await self._session_of(request_id, questions=True)
        await self._request("DELETE", f"/api/session/{session_id}/form/{request_id}")
        self._forget_form(request_id)
        return True

    async def diff(self, session_id: str) -> list[FileDiff]:
        """What the session changed, one summary per file. The patch text is dropped."""
        diffs = await self._data("GET", f"/api/session/{session_id}/diff")
        return [
            FileDiff(
                file=dig_str(d, "file"),
                additions=float(cast(float, dig(d, "additions")) or 0),
                deletions=float(cast(float, dig(d, "deletions")) or 0),
                status=dig_str(d, "status"),
            )
            for d in _items(diffs)
        ]

    async def dispose(self) -> None:
        """Nothing to free: 2.x keeps no per-directory instance to dispose of."""

    async def events(self) -> AsyncIterator[BackendEvent]:
        """One connection to the server-wide stream, filtered to this directory.

        Events without a location (execution, usage, ``server.connected``)
        are kept: the supervisor already ignores sessions that are not its own.
        """
        path = "/api/event"
        translator = Translator()
        timeout = httpx.Timeout(None, connect=5.0)
        try:
            async with aconnect_sse(self._http, "GET", path, timeout=timeout) as source:
                source.response.raise_for_status()
                async for message in source.aiter_sse():
                    event = translator.parse(message.data) if message.data else None
                    if event is None:
                        continue
                    where = dig_str(event.native, "location", "directory")
                    if where in (None, self.directory):
                        yield event
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise OpenCodeError("GET", path, status, "event stream refused") from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError("GET", path, None, f"{type(exc).__name__}: {exc}") from exc

    # -- internals ----------------------------------------------------------

    async def _session_of(self, request_id: str, *, questions: bool) -> str:
        if request_id not in self._request_sessions:
            await (self.questions() if questions else self.permissions())
        session_id = self._request_sessions.get(request_id)
        if not session_id:
            kind = "form" if questions else "permission"
            raise NotFoundError("POST", f"/api/{kind}/{request_id}", 404, "no such request")
        return session_id

    def _forget_form(self, form_id: str) -> None:
        self._forms.pop(form_id, None)
        self._request_sessions.pop(form_id, None)


def _items(data: object) -> list[object]:
    return cast(list[object], data) if isinstance(data, list) else []


def _mapping(data: object) -> dict[str, Any]:
    return cast(dict[str, Any], data) if isinstance(data, dict) else {}


def _session(data: object) -> Session:
    info = _mapping(data)
    tokens = info.get("tokens")
    return Session.model_validate(
        {
            **{k: v for k, v in info.items() if k not in ("parentID", "tokens")},
            "id": str(info.get("id", "")),
            "parent_id": info.get("parentID"),
            "title": str(dig(info, "metadata", "title") or info.get("title") or ""),
            "cost": float(info.get("cost") or 0),
            "tokens": TokenUsage.model_validate(tokens) if isinstance(tokens, dict) else None,
            # Kept under the name 1.x used, so evidence reads the same.
            "permission": info.get("permissions"),
        }
    )


def _message(data: dict[str, Any]) -> Message:
    tokens = data.get("tokens")
    parts: list[Part] = []
    if data.get("type") == "user":
        parts.append(Part(type="text", text=str(data.get("text") or "")))
    for item in dig_list(data, "content"):
        kind = dig_str(item, "type") or "unknown"
        parts.append(Part(type=kind, text=dig_str(item, "text"), tool=dig_str(item, "name")))
    return Message(
        info=MessageInfo(
            id=str(data.get("id", "")),
            role=str(data.get("type", "")),
            cost=cast(float | None, data.get("cost")),
            tokens=TokenUsage.model_validate(tokens) if isinstance(tokens, dict) else None,
            model_id=dig_str(data, "model", "id"),
            provider_id=dig_str(data, "model", "providerID"),
        ),
        parts=parts,
    )


def _permission(data: object) -> PermissionRequest:
    metadata = dig(data, "metadata")
    native = cast(dict[str, Any], metadata) if isinstance(metadata, dict) else {}
    patches = [dig_str(f, "patch") for f in dig_list(native, "files")]
    extra: dict[str, Any] = {}
    if any(patches):
        # The dashboard shows ``metadata.diff``, the name 1.x gave the patch.
        extra["diff"] = "\n".join(p for p in patches if p)
    return PermissionRequest(
        id=dig_str(data, "id") or "",
        session_id=dig_str(data, "sessionID") or "",
        permission=dig_str(data, "action") or "",
        patterns=[str(r) for r in dig_list(data, "resources")],
        metadata={**native, **extra},
    )


def _question(form: dict[str, Any]) -> QuestionRequest:
    questions: list[QuestionInfo] = []
    for field in dig_list(form, "fields"):
        options = [
            QuestionOption(
                label=dig_str(o, "label") or dig_str(o, "value") or "",
                description=dig_str(o, "description") or "",
            )
            for o in dig_list(field, "options")
        ]
        questions.append(
            QuestionInfo(
                question=dig_str(field, "description") or dig_str(field, "title") or "",
                header=dig_str(field, "title") or "",
                options=options,
                multiple=dig_str(field, "type") == "multiselect",
            )
        )
    return QuestionRequest(
        id=str(form["id"]), session_id=str(form.get("sessionID", "")), questions=questions
    )


def _answer(form: dict[str, Any], answers: list[list[str]]) -> dict[str, Any]:
    """Labels per field as the form's answer: an option's value, keyed by field."""
    answer: dict[str, Any] = {}
    for field, labels in zip(dig_list(form, "fields"), answers, strict=False):
        key = dig_str(field, "key")
        if key is None:
            continue
        values = {dig_str(o, "label"): dig_str(o, "value") for o in dig_list(field, "options")}
        chosen = [values.get(label) or label for label in labels]
        answer[key] = (
            chosen if dig_str(field, "type") == "multiselect" else (chosen[0] if chosen else "")
        )
    return answer
