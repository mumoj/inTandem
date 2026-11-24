"""Pydantic models for the OpenCode 1.x HTTP API (verified against 1.19.3).

All models allow extra fields so additions in minor OpenCode releases do not
cause validation failures. Models whose fields already match ``intandem.backend``
are re-exported directly; the rest use OpenCode's field aliases and are
converted by ``OpenCodeClient``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from intandem.backend.models import (
    CacheUsage,
    FileDiff,
    Health,
    ModelRef,
    Part,
    PermissionReply,
    QuestionInfo,
    QuestionOption,
    SessionStatus,
    TokenUsage,
)

__all__ = [
    "CacheUsage",
    "FileDiff",
    "Health",
    "Message",
    "MessageInfo",
    "ModelRef",
    "Part",
    "PermissionAction",
    "PermissionReply",
    "PermissionRequest",
    "PermissionRule",
    "QuestionInfo",
    "QuestionOption",
    "QuestionRequest",
    "Session",
    "SessionStatus",
    "TokenUsage",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Session(_Model):
    id: str
    parent_id: str | None = Field(default=None, alias="parentID")
    """Parent session ID when this session was spawned as a subagent."""
    title: str = ""
    cost: float = 0
    tokens: TokenUsage | None = None


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionRule(_Model):
    """Entry in an OpenCode session permission ruleset.

    OpenCode evaluates rules in order and the last matching rule wins, so broad
    defaults must precede narrower overrides (e.g. ``allow *`` before
    ``ask hello.txt``).
    """

    permission: str
    pattern: str
    action: PermissionAction


class PermissionRequest(_Model):
    id: str
    session_id: str = Field(alias="sessionID")
    permission: str
    patterns: list[str] = Field(default_factory=list[str])
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


class QuestionRequest(_Model):
    id: str
    session_id: str = Field(alias="sessionID")
    questions: list[QuestionInfo] = Field(default_factory=list[QuestionInfo])


class MessageInfo(_Model):
    id: str
    role: str
    cost: float | None = None
    tokens: TokenUsage | None = None
    model_id: str | None = Field(default=None, alias="modelID")
    provider_id: str | None = Field(default=None, alias="providerID")


class Message(_Model):
    info: MessageInfo
    parts: list[Part] = Field(default_factory=list[Part])
