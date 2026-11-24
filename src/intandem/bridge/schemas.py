"""Pydantic request and response schemas for the bridge HTTP API.

Input constraints (such as restricting IDs to path-safe characters) are enforced
at the schema layer before route handlers run.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator

from intandem.backend.jsonpath import dig, dig_str
from intandem.backend.models import FileDiff, Message, ModelRef, Part, PermissionReply
from intandem.supervisor.results import ImplementerResult
from intandem.supervisor.state import DirectiveAction, SpecStatus
from intandem.supervisor.supervisor import Pending, StatusReport, WaitReason


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")]
"""Identifier for specs and directives; restricted to filename-safe characters."""

Text = Annotated[str, StringConstraints(max_length=60_000), AfterValidator(_not_blank)]

# Restrict backend request IDs (e.g. OpenCode `per_`/`que_`/`frm_`, Claude, ACP)
# to path-safe characters so caller-supplied IDs cannot inject URL path segments.
PermissionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
QuestionId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]


def _model_name(value: str | None) -> str | None:
    if value is not None:
        ModelRef.parse(value)
    return value


class SpecIn(BaseModel):
    spec_id: Identifier
    text: Text
    model: Annotated[str | None, AfterValidator(_model_name)] = None
    """Model for this spec (``provider/model`` for OpenCode, or a bare model name for Claude)."""
    agent: Identifier | None = None
    """An OpenCode agent name, such as ``build`` or one from the project's config."""


class PromptIn(BaseModel):
    text: Text


class DirectiveIn(BaseModel):
    spec_id: Identifier
    directive_id: Identifier
    action: DirectiveAction
    text: Text


class RetractIn(BaseModel):
    spec_id: Identifier
    directive_id: Identifier


class PermissionIn(BaseModel):
    id: PermissionId
    reply: PermissionReply
    message: Annotated[str, StringConstraints(max_length=2_000)] | None = None


class AnswerIn(BaseModel):
    """Selected labels per question, or ``reject: true`` to decline the question."""

    id: QuestionId
    answers: list[list[str]] | None = None
    reject: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if self.reject == bool(self.answers):
            raise ValueError("send either a non-empty 'answers' or 'reject': true")
        return self


class Ok(BaseModel):
    ok: bool = True
    detail: str = ""


class SpecOut(BaseModel):
    spec_id: str
    session_id: str
    status: SpecStatus
    result_path: str


class DirectiveOut(BaseModel):
    spec_id: str
    directive_id: str
    action: DirectiveAction
    retracted: bool


class WaitOut(BaseModel):
    reason: WaitReason
    detail: str
    status: StatusReport


class OverviewOut(BaseModel):
    """Combined status, attention reason, and pending prompts returned to the dashboard."""

    status: StatusReport
    attention: WaitReason | None
    """Current reason ``wait`` would return, or ``None`` while working."""
    attention_detail: str
    pending: Pending


class JournalOut(BaseModel):
    entries: list[dict[str, Any]]
    next: int
    """Byte offset to pass as ``after`` to fetch only newly appended entries."""


class OpenCodeOut(BaseModel):
    url: str | None
    user: str = "opencode"
    password: str | None


class ResultOut(BaseModel):
    path: str | None
    result: ImplementerResult | None


_TOOL_OUTPUT_LIMIT = 16_000
_SUMMARY_KEYS = (
    "command",
    "CommandLine",
    "file_path",
    "Path",
    "TargetFile",
    "notebook_path",
    "path",
    "pattern",
    "query",
    "Query",
    "url",
    "description",
)


def _tool_summary(tool_input: object, title: str | None = None) -> str:
    summary = next((v for k in _SUMMARY_KEYS if (v := dig_str(tool_input, k))), None) or title
    if not summary:
        return ""
    one_line = " ".join(summary.split())
    return one_line if len(one_line) <= 160 else one_line[:159] + "…"


class ToolCallOut(BaseModel):
    """One tool invocation inside an implementer message, for dashboard inspection."""

    tool: str
    call_id: str = ""
    status: str = ""
    summary: str = ""
    input: Any = None
    output: str | None = None

    @classmethod
    def of(cls, part: Part) -> ToolCallOut:
        extra = part.model_extra or {}
        state = extra.get("state")
        call_id = part.call_id or dig_str(extra, "callID") or ""
        status = part.status or dig_str(state, "status") or ""
        tool_input = part.input if part.input is not None else dig(state, "input")
        raw_out = part.output
        if raw_out is None:
            raw_out = dig_str(state, "output") or dig_str(state, "error")
        if raw_out is not None and len(raw_out) > _TOOL_OUTPUT_LIMIT:
            raw_out = raw_out[:_TOOL_OUTPUT_LIMIT] + "\n… (truncated)"
        title = dig_str(state, "title")
        return cls(
            tool=part.tool or "tool",
            call_id=call_id,
            status=status,
            summary=_tool_summary(tool_input, title),
            input=tool_input,
            output=raw_out,
        )


class MessageOut(BaseModel):
    """Simplified message view for CLI and dashboard display, including tool call details."""

    role: str
    text: str
    tools: list[str] = Field(default_factory=list[str])
    tool_calls: list[ToolCallOut] = Field(default_factory=list[ToolCallOut])

    @classmethod
    def of(cls, message: Message) -> MessageOut:
        tool_parts = [part for part in message.parts if part.type == "tool" and part.tool]
        return cls(
            role=message.info.role,
            text=message.text(),
            tools=[part.tool for part in tool_parts if part.tool],
            tool_calls=[ToolCallOut.of(part) for part in tool_parts],
        )


class MessagesOut(BaseModel):
    session_id: str
    messages: list[MessageOut]


class DiffOut(BaseModel):
    files: list[FileDiff]


class FilePatchOut(BaseModel):
    file: str
    patch: str
    truncated: bool = False


class LandInfoOut(BaseModel):
    """Whether this task runs in a linked git worktree that can be merged into the main repo."""

    is_worktree: bool
    worktree_name: str = ""
    branch: str | None = None
    target_branch: str | None = None
    repo_root: str | None = None
    repo_head_moved: bool = False
    busy: bool = False
    can_merge: bool = False
    reason: str = ""
    default_message: str = ""
    files: list[FileDiff] = Field(default_factory=list[FileDiff])


class LandIn(BaseModel):
    action: Literal["merge", "discard"]
    message: Annotated[str, StringConstraints(max_length=2_000)] | None = None


class LandOut(BaseModel):
    action: Literal["merge", "discard"]
    name: str
    branch: str
    merged_into: str | None = None
    committed_leftovers: bool = False
    discarded: bool = False
    evidence: str | None = None
