"""Which ``opencode serve`` inTandem can drive, and where to find it.

OpenCode 2 replaced the server API this package targets, and its installer
replaces the 1.x binary in place. One upgrade on the host would otherwise turn
every call into a 404 with no hint why, so the shared host checks the version
in its own health report before any task attaches to it.
"""

from __future__ import annotations

import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_MAJOR = 1
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class UnsupportedVersionError(RuntimeError):
    """The installed OpenCode speaks a server API this package does not."""


@dataclass(frozen=True, order=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _VERSION.search(text)
        if match is None:
            raise UnsupportedVersionError(f"cannot read an OpenCode version from {text.strip()!r}")
        major, minor, patch = (int(group) for group in match.groups())
        return cls(major, minor, patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def require_supported(version: Version) -> Version:
    if version.major != SUPPORTED_MAJOR:
        raise UnsupportedVersionError(
            f"OpenCode {version} is not supported: inTandem speaks the OpenCode "
            f"{SUPPORTED_MAJOR}.x server API, which OpenCode 2 replaced. "
            "Install the 1.x line with `npm i -g opencode-ai@1`."
        )
    return version


def resolve_binary(name: str) -> str:
    """An absolute path to the OpenCode binary, or a clear error."""
    found = shutil.which(name)
    if found:
        return found
    fallback = Path.home() / ".opencode" / "bin" / "opencode"
    if name == "opencode" and fallback.is_file():
        return str(fallback)
    raise FileNotFoundError(f"OpenCode binary {name!r} not found on PATH")


def free_port() -> int:
    """A port that was free a moment ago.

    ``opencode serve --port 0`` does not bind an ephemeral port; it falls back
    to its default 4096. So the port is chosen here, both to avoid colliding
    with another server and so it is known before the server starts.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
