"""Manifest file written by a running bridge so clients can locate and authenticate to it.

Because it stores the bearer token and OpenCode password, it is created with
0600 permissions and removed on clean shutdown.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_serializer

from intandem.supervisor.state import write_private


class Manifest(BaseModel):
    version: Literal[1] = 1
    host: str
    port: int
    path: str = ""
    """URL prefix where this task's bridge is mounted on the hub."""
    token: SecretStr
    task_dir: str
    state_dir: str
    pid: int = Field(default_factory=os.getpid)
    opencode_port: int | None = None
    opencode_version: str | None = None
    # Stored so `inTandem attach` can connect OpenCode's TUI or web UI to the live session.
    opencode_password: SecretStr | None = None
    started_at: float = Field(default_factory=time.time)

    @field_serializer("token", "opencode_password", when_used="json")
    def _reveal_secret(self, secret: SecretStr | None) -> str | None:
        # Unwrap secrets when serializing to the 0600 manifest file on disk.
        return secret.get_secret_value() if secret is not None else None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    @property
    def dashboard_url(self) -> str:
        # Pass the token in the URL fragment so browsers never send it in HTTP requests.
        return f"{self.url}/ui#token={self.token.get_secret_value()}"

    @property
    def opencode_url(self) -> str | None:
        return f"http://127.0.0.1:{self.opencode_port}" if self.opencode_port else None

    @property
    def alive(self) -> bool:
        """Return True if the PID that created this manifest is still running."""
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def write(self, path: Path) -> None:
        write_private(path, self.model_dump_json(indent=2) + "\n")

    @classmethod
    def read(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_bytes())

    @staticmethod
    def remove(path: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
