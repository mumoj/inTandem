"""OpenCode binary discovery, port selection, and server version probing.

OpenCode 1.x exposes ``/global/health`` while 2.x exposes ``/api/info`` with a
different REST API. We probe the running server's version to select the right
client and reject unsupported major versions early.
"""

from __future__ import annotations

import re
import shutil
import socket
from dataclasses import dataclass

import httpx

from intandem.opencode import install as installer

SUPPORTED_MAJORS = (1, 2)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class UnsupportedVersionError(RuntimeError):
    """Raised when the running OpenCode server reports an unsupported major version."""


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
    if version.major not in SUPPORTED_MAJORS:
        majors = " and ".join(f"{major}.x" for major in SUPPORTED_MAJORS)
        raise UnsupportedVersionError(
            f"OpenCode {version} is not supported: inTandem speaks the OpenCode {majors} "
            "server APIs. Install a supported release with `opencode upgrade 2.3.2`."
        )
    return version


def probe_version(http: httpx.Client, url: str) -> Version | None:
    """Query the server's version endpoint, returning ``None`` if it is not responding yet."""
    for path in ("/global/health", "/api/info"):
        response = http.get(f"{url}{path}")
        if response.is_success and _is_json(response):
            return Version.parse(str(response.json()["version"]))
    return None


async def aprobe_version(http: httpx.AsyncClient, url: str) -> Version:
    """Async version of ``probe_version`` for a running server; raises if neither API responds."""
    for path in ("/global/health", "/api/info"):
        response = await http.get(f"{url}{path}")
        if response.is_success and _is_json(response):
            return Version.parse(str(response.json()["version"]))
    raise UnsupportedVersionError(f"{url} answers neither the OpenCode 1.x nor the 2.x API")


def _is_json(response: httpx.Response) -> bool:
    # OpenCode 2.x serves its web app HTML for unknown paths such as /global/health.
    return response.headers.get("content-type", "").startswith("application/json")


def resolve_binary(name: str, *, install: bool = True) -> str:
    """Return the path to the OpenCode binary, installing the default binary if missing.

    Only auto-installs when ``name`` is ``"opencode"``; an explicit custom
    binary name raises ``FileNotFoundError`` if missing.
    """
    found = shutil.which(name)
    if found:
        return found
    fallback = installer.install_dir() / "opencode"
    if name == "opencode" and fallback.is_file():
        return str(fallback)
    if name == "opencode" and install:
        try:
            return str(installer.install())
        except installer.InstallError as exc:
            raise FileNotFoundError(f"OpenCode is not installed and could not be: {exc}") from exc
    raise FileNotFoundError(f"OpenCode binary {name!r} not found on PATH")


def free_port() -> int:
    """Pick an available loopback port.

    ``opencode serve --port 0`` falls back to port 4096 instead of binding an
    ephemeral port, so we select a free port beforehand.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
