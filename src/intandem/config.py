"""Configuration settings and on-disk layout for a supervised task.

All configuration lives in ``Settings`` so the CLI, environment variables, and
tests share the same schema. Environment variables use the ``INTANDEM_`` prefix
(for example, ``INTANDEM_MODEL=openrouter/qwen/qwen3-coder``).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_state_root() -> Path:
    """Return ``$XDG_STATE_HOME/intandem`` for storing logs and resumable state."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "intandem"


class Settings(BaseSettings):
    """Configuration for a single task and for starting the hub via ``inTandem serve``."""

    model_config = SettingsConfigDict(env_prefix="INTANDEM_", extra="ignore")

    task_dir: Path
    """Working directory edited by the implementer."""

    model: str | None = None
    """Model in ``provider/model`` format. When ``None``, uses the harness default."""

    harness: Literal["opencode", "claude", "antigravity"] = "opencode"
    """Implementer harness: OpenCode, Claude Code (via the Agent SDK), or the
    Antigravity CLI (``agy``) over ACP.

    ``claude`` requires the optional ``claude`` extra and a logged-in ``claude``
    CLI, and uses ``anthropic/<model>`` names. ``antigravity`` requires a
    signed-in ``agy`` CLI and accepts bare model IDs (from ``agy models``) or
    ``antigravity/<model>``."""

    claude_bin: str = "claude"
    """Path or name of the ``claude`` CLI executable."""

    antigravity_bin: str = "agy"
    """Path or name of the Antigravity CLI executable (run as ``agy acp``)."""

    protected: list[str] = Field(default_factory=list)
    """Glob patterns relative to ``task_dir`` that the implementer may not edit."""

    opencode_bin: str = "opencode"
    """Executable used by ``inTandem serve`` to start the shared OpenCode server."""
    opencode_port: int = 0
    """Port for the shared OpenCode server (0 chooses an available port)."""
    opencode_url: str | None = None
    """URL of the shared ``opencode serve`` instance, populated by the hub."""
    opencode_password: SecretStr | None = None
    """Password for the shared OpenCode server, populated by the hub."""
    bridge_port: int = 0
    """Port for ``inTandem serve`` to listen on (0 chooses an available port)."""
    state_root: Path = Field(default_factory=default_state_root)

    notify_cmd: str | None = None
    """Shell command run on escalated alerts, with the summary passed in ``$INTANDEM_NOTIFY``."""

    ntfy_topic: str | None = None
    """Optional ntfy topic for push alerts (disabled by default to avoid external publishing)."""

    ntfy_server: str = "https://ntfy.sh"

    stall_after: float = 720.0
    """Seconds of text output without a tool call before flagging a reasoning stall."""

    quiet_after: float = 300.0
    """Seconds without text or tool activity before flagging a busy turn as hung."""

    text_recent: float = 120.0
    """Maximum age (in seconds) of recent text output for a turn to be considered active."""

    poll_interval: float = 2.0
    """Interval in seconds between monitor checks (result file, protected files, stalls)."""

    max_stream_line: int = 128 * 1024
    notify_timeout: float = 30.0

    @field_validator("task_dir")
    @classmethod
    def _task_dir_exists(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"no such task directory: {resolved}")
        return resolved

    @model_validator(mode="after")
    def _shared_server_needs_its_password(self) -> Self:
        if (self.opencode_url is None) != (self.opencode_password is None):
            raise ValueError("opencode_url and opencode_password go together")
        return self

    @model_validator(mode="after")
    def _validate_model(self) -> Self:
        if self.model is None:
            return self
        from intandem.backend.models import ModelRef

        ref = ModelRef.parse(self.model)
        if self.harness == "opencode" and ref.provider_id is None:
            raise ValueError("model must be provider/model, e.g. openrouter/qwen/qwen3-coder")
        return self


@dataclass(frozen=True, slots=True)
class TaskPaths:
    """Paths to state, log, and control files for a single task.

    Keeping each task in its own directory makes it straightforward to inspect
    a run or reset it by deleting the directory.
    """

    root: Path

    @classmethod
    def for_task(cls, state_root: Path, task_dir: Path) -> TaskPaths:
        # Include the directory name for readability when passing --task, and a
        # short path hash so two checkouts with the same folder name don't collide.
        digest = hashlib.sha256(str(task_dir).encode()).hexdigest()[:6]
        return cls(state_root.expanduser() / f"{task_dir.name}-{digest}")

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def journal(self) -> Path:
        return self.root / "journal.jsonl"

    @property
    def events(self) -> Path:
        return self.root / "events.sse"

    @property
    def notifications(self) -> Path:
        return self.root / "notifications.log"

    @property
    def lock(self) -> Path:
        return self.root / "supervisor.lock"

    @property
    def results(self) -> Path:
        return self.root / "results"

    @property
    def evidence(self) -> Path:
        return self.root / "sessions"

    def ensure(self) -> None:
        for directory in (self.root, self.results, self.evidence):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
