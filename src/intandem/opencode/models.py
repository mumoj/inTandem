"""The slice of OpenCode's server API this package reads, in OpenCode's own shape.

Shapes come from the OpenAPI document ``opencode serve`` publishes at ``/doc``
(checked against 1.19.3). Every model allows extra fields: OpenCode adds
properties between minor versions, and a new field should never be what stops
a supervisor mid-task. Only the fields something here acts on are declared.

Models whose shape matches inTandem's neutral ones are those very classes. The
rest carry OpenCode's field names; the client converts them at its edge.
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
    """Set on a subagent's session: the session whose ``task`` tool started it."""
    title: str = ""
    cost: float = 0
    tokens: TokenUsage | None = None


class PermissionAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class PermissionRule(_Model):
    """One entry in a session's permission ruleset.

    OpenCode evaluates the rules in order and the **last** match wins, so a broad
    rule has to come before the narrow one that overrides it. Verified against
    1.19.3: ``[ask hello.txt, allow *]`` lets the edit through unasked.
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
