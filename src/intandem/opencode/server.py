"""Which ``opencode serve`` inTandem can drive, and where to find it.

OpenCode 1.x and 2.x serve entirely different APIs from the same command, and
2.x's installer replaces a 1.x binary in place. So the version is read from
the running server, never assumed: 1.x answers ``/global/health``, 2.x
``/api/info``, and each gets its own client. Any other major is refused before
a task attaches, rather than failing on every call with a 404.
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
    if version.major not in SUPPORTED_MAJORS:
        majors = " and ".join(f"{major}.x" for major in SUPPORTED_MAJORS)
        raise UnsupportedVersionError(
            f"OpenCode {version} is not supported: inTandem speaks the OpenCode {majors} "
            "server APIs. Install a supported release with `opencode upgrade 2.3.2`."
        )
    return version


def probe_version(http: httpx.Client, url: str) -> Version | None:
    """The version a running server reports, or ``None`` if it does not answer yet."""
    for path in ("/global/health", "/api/info"):
        response = http.get(f"{url}{path}")
        if response.is_success and _is_json(response):
            return Version.parse(str(response.json()["version"]))
    return None


async def aprobe_version(http: httpx.AsyncClient, url: str) -> Version:
    """``probe_version`` for a server that is known to be up. Raises if it is not."""
    for path in ("/global/health", "/api/info"):
        response = await http.get(f"{url}{path}")
        if response.is_success and _is_json(response):
            return Version.parse(str(response.json()["version"]))
    raise UnsupportedVersionError(f"{url} answers neither the OpenCode 1.x nor the 2.x API")


def _is_json(response: httpx.Response) -> bool:
    # 2.x answers unknown paths, /global/health among them, with its web app's HTML.
    return response.headers.get("content-type", "").startswith("application/json")


def resolve_binary(name: str, *, install: bool = True) -> str:
    """An absolute path to the OpenCode binary, installing it if there is none.

    Only the default name is installed: a custom ``name`` is someone's choice of
    binary, and a missing one is a mistake to report, not to paper over.
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
    """A port that was free a moment ago.

    ``opencode serve --port 0`` does not bind an ephemeral port; it falls back
    to its default 4096. So the port is chosen here, both to avoid colliding
    with another server and so it is known before the server starts.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
