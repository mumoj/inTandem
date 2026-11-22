"""The data inTandem reads from an implementer backend, in its own field names.

A backend adapter parses its harness's wire format and hands back these
models. Only the fields something in inTandem acts on are declared. Every model
allows extra fields, so an adapter can keep what its harness sent beside them
for the evidence it records.
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
    """Set on a subagent's session: the session that started it."""
    title: str = ""
    cost: float = 0
    tokens: TokenUsage | None = None


class SessionStatus(_Model):
    """``idle``, ``busy`` or ``retry``. Kept open in case a backend adds one."""

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


class Message(_Model):
    info: MessageInfo
    parts: list[Part] = Field(default_factory=list[Part])

    def text(self) -> str:
        """The message's visible text, joined across its text parts."""
        return "\n".join(part.text for part in self.parts if part.type == "text" and part.text)


class ModelRef(_Model):
    """A model choice, optionally qualified as ``provider/model``."""

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
