"""A typed async client for the OpenCode 1.x server API.

``OpenCodeClient`` is one adapter behind ``ImplementerBackend``: it speaks
OpenCode's HTTP API and wire shapes, and hands the supervisor inTandem's
neutral models.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Mapping
from types import TracebackType
from typing import Any, Self

import httpx
from httpx_sse import aconnect_sse
from pydantic import BaseModel, TypeAdapter

from intandem.backend import errors as backend_errors
from intandem.backend import models as neutral
from intandem.backend.guard import Guard
from intandem.backend.protocol import ImplementerBackend
from intandem.opencode import models as wire
from intandem.opencode.events import Event
from intandem.opencode.models import (
    FileDiff,
    Health,
    ModelRef,
    PermissionReply,
    PermissionRule,
    SessionStatus,
)
from intandem.opencode.rules import guard_to_rules, subagent_rules


class OpenCodeError(backend_errors.BackendError):
    """OpenCode answered with an error, or could not be reached at all."""

    def __init__(self, method: str, path: str, status_code: int | None, detail: str = "") -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        where = f"{method} {path}"
        super().__init__(
            f"{where} -> {status_code}: {detail}" if status_code else f"{where}: {detail}"
        )


class NotFoundError(OpenCodeError, backend_errors.NotFoundError):
    """The session, permission or question no longer exists."""


OpenCodeAPI = ImplementerBackend
"""The old name of the seam, kept for one release."""


def _neutral[T: BaseModel](kind: type[T], source: BaseModel) -> T:
    """``source`` re-read as the neutral ``kind``: field names line up, extras ride along."""
    return kind.model_validate(source.model_dump())


_STATUSES = TypeAdapter(dict[str, SessionStatus])
_MESSAGES = TypeAdapter(list[wire.Message])
_PERMISSIONS = TypeAdapter(list[wire.PermissionRequest])
_QUESTIONS = TypeAdapter(list[wire.QuestionRequest])
_DIFFS = TypeAdapter(list[FileDiff])


def _merge_diffs(turns: Iterable[list[FileDiff]]) -> list[FileDiff]:
    """One summary per file. OpenCode's patch text is dropped: it can be huge, and
    a turn's patch says nothing about the file once a later turn has changed it."""
    merged: dict[str | None, FileDiff] = {}
    for turn in turns:
        for diff in turn:
            seen = merged.setdefault(diff.file, FileDiff(file=diff.file, status=diff.status))
            seen.additions += diff.additions
            seen.deletions += diff.deletions
            if diff.status == "deleted":
                seen.status = "deleted"
    return list(merged.values())


_BOOL = TypeAdapter(bool)


class OpenCodeClient:
    """HTTP access to one ``opencode serve`` process.

    Concurrency is capped by the connection pool rather than a lock. An
    exclusive lock in the first version let one slow call, a multi-megabyte
    evidence capture, stall the supervisor, the bridge and permission replies
    behind it.

    With ``directory``, the client is one tenant of a server shared by several
    tasks: every request names its directory, and the event stream is the
    server-wide one filtered to it.
    """

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        username: str = "opencode",
        timeout: float = 60.0,
        max_connections: int = 8,
        directory: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.directory = directory
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=(username, password),
            params={"directory": directory} if directory else None,
            timeout=httpx.Timeout(timeout, connect=5.0),
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
        )

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

    async def health(self) -> Health:
        response = await self._request("GET", "/global/health")
        return Health.model_validate_json(response.content)

    async def create_session(self, title: str, guard: Guard) -> neutral.Session:
        body = {"title": title, "permission": _dump_rules(guard_to_rules(guard))}
        response = await self._request("POST", "/session", json=body)
        return _neutral(neutral.Session, wire.Session.model_validate_json(response.content))

    async def get_session(self, session_id: str) -> neutral.Session:
        response = await self._request("GET", f"/session/{session_id}")
        return _neutral(neutral.Session, wire.Session.model_validate_json(response.content))

    async def guard_subagent(self, session_id: str, guard: Guard, inherited: object = None) -> None:
        if inherited is None:
            session = await self.get_session(session_id)
            inherited = (session.model_extra or {}).get("permission")
        await self.set_session_permission(session_id, subagent_rules(inherited, guard))

    async def set_session_permission(
        self, session_id: str, permission: list[PermissionRule]
    ) -> None:
        """Replace a session's whole ruleset. OpenCode-only; the seam uses guards."""
        await self._request(
            "PATCH", f"/session/{session_id}", json={"permission": _dump_rules(permission)}
        )

    async def session_status(self) -> dict[str, SessionStatus]:
        response = await self._request("GET", "/session/status")
        return _STATUSES.validate_json(response.content)

    async def messages(self, session_id: str, limit: int | None = None) -> list[neutral.Message]:
        params = {"limit": limit} if limit else None
        response = await self._request("GET", f"/session/{session_id}/message", params=params)
        return [_neutral(neutral.Message, m) for m in _MESSAGES.validate_json(response.content)]

    async def prompt(
        self, session_id: str, text: str, model: ModelRef | None, agent: str | None = None
    ) -> None:
        """Queue a prompt and return at once. The turn runs on the server."""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model is not None:
            body["model"] = {"providerID": model.provider_id, "modelID": model.model_id}
        if agent is not None:
            body["agent"] = agent
        await self._request("POST", f"/session/{session_id}/prompt_async", json=body)

    async def abort(self, session_id: str) -> bool:
        response = await self._request("POST", f"/session/{session_id}/abort")
        return _BOOL.validate_json(response.content)

    async def permissions(self) -> list[neutral.PermissionRequest]:
        response = await self._request("GET", "/permission")
        requests = _PERMISSIONS.validate_json(response.content)
        return [_neutral(neutral.PermissionRequest, r) for r in requests]

    async def reply_permission(
        self, request_id: str, reply: PermissionReply, message: str | None = None
    ) -> bool:
        body: dict[str, Any] = {"reply": reply}
        if message:
            body["message"] = message
        response = await self._request("POST", f"/permission/{request_id}/reply", json=body)
        return _BOOL.validate_json(response.content)

    async def questions(self) -> list[neutral.QuestionRequest]:
        response = await self._request("GET", "/question")
        requests = _QUESTIONS.validate_json(response.content)
        return [_neutral(neutral.QuestionRequest, r) for r in requests]

    async def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        response = await self._request(
            "POST", f"/question/{request_id}/reply", json={"answers": answers}
        )
        return _BOOL.validate_json(response.content)

    async def reject_question(self, request_id: str) -> bool:
        response = await self._request("POST", f"/question/{request_id}/reject")
        return _BOOL.validate_json(response.content)

    async def diff(self, session_id: str) -> list[FileDiff]:
        """What the session changed, summed across its turns.

        OpenCode keeps a diff per user message and answers an empty list without
        a ``messageID``, so a session has to be asked one turn at a time.
        """
        messages = await self.messages(session_id)
        turns = [message.info.id for message in messages if message.info.role == "user"]
        return _merge_diffs(await asyncio.gather(*(self._turn_diff(session_id, t) for t in turns)))

    async def _turn_diff(self, session_id: str, message_id: str) -> list[FileDiff]:
        response = await self._request(
            "GET", f"/session/{session_id}/diff", params={"messageID": message_id}
        )
        return _DIFFS.validate_json(response.content)

    async def dispose(self) -> None:
        await self._request("POST", "/instance/dispose")

    async def events(self) -> AsyncIterator[Event]:
        """One connection to the event stream. Returns when the server closes it.

        Reconnecting is the caller's decision: the supervisor needs to resync
        what it missed while disconnected, which a transparent retry here would
        hide from it.
        """
        path = "/global/event" if self.directory else "/event"
        timeout = httpx.Timeout(None, connect=5.0)
        try:
            async with aconnect_sse(self._http, "GET", path, timeout=timeout) as source:
                source.response.raise_for_status()
                async for message in source.aiter_sse():
                    if message.data and (event := self._parse(message.data)) is not None:
                        yield event
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise OpenCodeError("GET", path, status, "event stream refused") from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError("GET", path, None, f"{type(exc).__name__}: {exc}") from exc

    def _parse(self, data: str) -> Event | None:
        if self.directory:
            return Event.parse_global(data, self.directory)
        return Event.parse(data)


def _dump_rules(rules: list[PermissionRule]) -> list[dict[str, Any]]:
    return [rule.model_dump(mode="json") for rule in rules]
