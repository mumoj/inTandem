"""Normalized data models returned by implementer backends.

Adapters parse their harness wire formats into these Pydantic models. Only the
fields used by inTandem are explicitly typed; ``extra="allow"`` preserves
additional backend-specific fields for session logs.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow")


class Health(_Model):
    healthy: bool
    version: str


class CacheUsage(_Model):
    read: float = 0
    write: float = 0


class TokenUsage(_Model):
    input: float = 0
    output: float = 0
    reasoning: float = 0
    cache: CacheUsage = Field(default_factory=CacheUsage)


class Session(_Model):
    id: str
    parent_id: str | None = None
    """Parent session ID when this session belongs to a subagent."""
    title: str = ""
    cost: float = 0
    tokens: TokenUsage | None = None


class SessionStatus(_Model):
    """Session state (typically ``idle``, ``busy``, or ``retry``)."""

    type: str
    attempt: int | None = None
    message: str | None = None


PermissionReply = Literal["once", "always", "reject"]


class PermissionRequest(_Model):
    id: str
    session_id: str
    permission: str
    patterns: list[str] = Field(default_factory=list[str])
    metadata: dict[str, Any] = Field(default_factory=dict[str, Any])


class QuestionOption(_Model):
    label: str
    description: str = ""


class QuestionInfo(_Model):
    question: str
    header: str = ""
    options: list[QuestionOption] = Field(default_factory=list[QuestionOption])
    multiple: bool = False


class QuestionRequest(_Model):
    id: str
    session_id: str
    questions: list[QuestionInfo] = Field(default_factory=list[QuestionInfo])


class FileDiff(_Model):
    file: str | None = None
    additions: float = 0
    deletions: float = 0
    status: str | None = None


class MessageInfo(_Model):
    id: str
    role: str
    cost: float | None = None
    tokens: TokenUsage | None = None
    model_id: str | None = None
    provider_id: str | None = None


class Part(_Model):
    type: str
    text: str | None = None
    tool: str | None = None
    call_id: str | None = None
    status: str | None = None
    input: Any = None
    output: str | None = None


class Message(_Model):
    info: MessageInfo
    parts: list[Part] = Field(default_factory=list[Part])

    def text(self) -> str:
        """Join all non-empty text parts of this message."""
        return "\n".join(part.text for part in self.parts if part.type == "text" and part.text)


class ModelRef(_Model):
    """Model identifier, optionally qualified as ``provider/model``."""

    provider_id: str | None = None
    model_id: str

    @property
    def qualified(self) -> str:
        return f"{self.provider_id}/{self.model_id}" if self.provider_id else self.model_id

    @classmethod
    def parse(cls, value: str) -> Self:
        if not value or any(ch.isspace() for ch in value):
            raise ValueError(f"invalid model name {value!r}")
        if "/" in value:
            provider, _, model = value.partition("/")
            if not provider or not model:
                raise ValueError(f"expected provider/model, got {value!r}")
            return cls(provider_id=provider, model_id=model)
        return cls(provider_id=None, model_id=value)
