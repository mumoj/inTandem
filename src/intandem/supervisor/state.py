"""Persistent supervisor state and per-task file locking.

Saving state to disk lets a restarted supervisor reconnect to its existing
session instead of creating a duplicate, and verify whether pending prompts
were already delivered before resending them.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal, Self

from pydantic import BaseModel, Field


class SpecStatus(StrEnum):
    PROMPT_PENDING = "prompt_pending"
    """Session created, but delivery of the spec prompt has not been confirmed yet."""
    ACTIVE = "active"
    RESULT_WRITTEN = "result_written"
    PROTECTED_VIOLATION = "protected_violation"
    SUPERSEDED = "superseded"


class DirectiveAction(StrEnum):
    CORRECT = "CORRECT"
    """Apply a correction and continue working."""
    STOP_AND_WAIT = "STOP_AND_WAIT"
    """Abort the active turn, write a partial result, and wait."""


class DirectiveRecord(BaseModel):
    directive_id: str
    action: DirectiveAction
    text: str
    digest: str
    delivered: bool = False
    """Set to ``True`` once the prompt is accepted; restarts re-check undelivered directives."""
    retracted: bool = False
    created_at: float = Field(default_factory=time.time)


class SpecRecord(BaseModel):
    spec_id: str
    digest: str
    text: str
    session_id: str
    model: str | None = None
    """Model in ``provider/model`` format for this spec, or ``None`` for the supervisor default."""
    agent: str | None = None
    """Backend agent/mode name for this spec, or ``None`` for the backend default."""
    status: SpecStatus = SpecStatus.PROMPT_PENDING
    created_at: float = Field(default_factory=time.time)
    last_prompt_at: float = 0.0
    """Timestamp of the most recent prompt; earlier result files are ignored."""
    protected: dict[str, str] = Field(default_factory=dict[str, str])
    protected_patterns: list[str] | None = None
    """Glob patterns used to build ``protected``, or ``None`` for legacy state files."""
    directives: dict[str, DirectiveRecord] = Field(default_factory=dict[str, DirectiveRecord])
    lost_sessions: list[str] = Field(default_factory=list[str])

    @property
    def active_directives(self) -> list[str]:
        return [d.directive_id for d in self.directives.values() if not d.retracted]


class SupervisorState(BaseModel):
    version: Literal[1] = 1
    task_dir: str
    current_spec_id: str | None = None
    specs: dict[str, SpecRecord] = Field(default_factory=dict[str, SpecRecord])
    updated_at: float = Field(default_factory=time.time)

    @property
    def current(self) -> SpecRecord | None:
        return self.specs.get(self.current_spec_id) if self.current_spec_id else None


class StateError(RuntimeError):
    """Raised when the state file is unreadable or belongs to another task directory."""


def write_private(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` with 0600 permissions.

    Writes to a sibling temp file created with mode 0600 (important because
    manifests contain bearer tokens), fsyncs it, and renames it into place so a
    crash cannot leave a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


class StateStore:
    def __init__(self, path: Path, task_dir: Path) -> None:
        self.path = path
        self.task_dir = str(task_dir)

    def load(self) -> SupervisorState:
        if not self.path.exists():
            return SupervisorState(task_dir=self.task_dir)
        try:
            state = SupervisorState.model_validate_json(self.path.read_bytes())
        except (OSError, ValueError) as exc:
            raise StateError(f"cannot read supervisor state {self.path}: {exc}") from exc
        if state.task_dir != self.task_dir:
            raise StateError(f"{self.path} belongs to {state.task_dir}, not {self.task_dir}")
        return state

    def save(self, state: SupervisorState) -> None:
        state.updated_at = time.time()
        write_private(self.path, state.model_dump_json(indent=2) + "\n")


class SupervisorLock:
    """Non-blocking ``flock`` lock ensuring only one supervisor runs per task directory.

    The kernel automatically releases the lock if the process crashes, so stale
    lock files cannot block future runs.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: IO[str] | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise RuntimeError(f"another supervisor already owns {self.path.parent}") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid(), "acquired": time.time()}) + "\n")
        stream.flush()
        self._stream = stream
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None


def dump_jsonl(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"), default=str) + "\n"
