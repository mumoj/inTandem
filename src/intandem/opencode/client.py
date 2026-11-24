"""Async HTTP/SSE client implementing ``ImplementerBackend`` for OpenCode 1.x."""

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
from intandem.backend.events import BackendEvent
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
from intandem.opencode.translate import from_opencode


class OpenCodeError(backend_errors.BackendError):
    """Raised when the OpenCode server returns an error or cannot be reached."""

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
    """Raised when a session, permission request, or question does not exist."""


OpenCodeAPI = ImplementerBackend
"""Deprecated alias for ``ImplementerBackend``, kept for backwards compatibility."""


def _neutral[T: BaseModel](kind: type[T], source: BaseModel) -> T:
    """Convert an OpenCode wire model into the corresponding backend model."""
    return kind.model_validate(source.model_dump())


_STATUSES = TypeAdapter(dict[str, SessionStatus])
_MESSAGES = TypeAdapter(list[wire.Message])
_PERMISSIONS = TypeAdapter(list[wire.PermissionRequest])
_QUESTIONS = TypeAdapter(list[wire.QuestionRequest])
_DIFFS = TypeAdapter(list[FileDiff])


def _merge_diffs(turns: Iterable[list[FileDiff]]) -> list[FileDiff]:
    """Sum per-turn diff stats per file, omitting raw patch text."""
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
    """Async HTTP client for an ``opencode serve`` 1.x instance.

    Concurrency is bounded by the httpx connection pool rather than a mutex so
    large reads (like session captures) do not block permission replies.

    When ``directory`` is set, requests include it as a query parameter and
    ``events()`` subscribes to ``/global/event`` filtered to that directory.
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
        """Replace a session's OpenCode permission ruleset."""
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
        """Queue a prompt asynchronously; the turn runs on the OpenCode server."""
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if model is not None:
            if model.provider_id is None:
                raise OpenCodeError(
                    "POST",
                    f"/session/{session_id}/prompt_async",
                    None,
                    f"OpenCode requires provider/model, got {model.model_id!r}",
                )
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
        """Aggregate file diffs across all user turns in ``session_id``.

        OpenCode returns per-message diffs and yields an empty list when
        ``messageID`` is omitted, so each user turn is queried individually.
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

    async def events(self) -> AsyncIterator[BackendEvent]:
        """Stream events for a single SSE connection and return when the server closes it.

        Reconnection is left to the caller so the supervisor knows to resync
        any events missed while disconnected.
        """
        path = "/global/event" if self.directory else "/event"
        timeout = httpx.Timeout(None, connect=5.0)
        try:
            async with aconnect_sse(self._http, "GET", path, timeout=timeout) as source:
                source.response.raise_for_status()
                async for message in source.aiter_sse():
                    if message.data and (event := self._parse(message.data)) is not None:
                        yield from_opencode(event)
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
