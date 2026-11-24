"""Downloads and verifies a pinned OpenCode GitHub release when none is installed.

Downloads the platform archive, checks its SHA-256 against the release's
``SHA256SUMS``, and extracts the binary to ``~/.opencode/bin``. Set
``INTANDEM_NO_INSTALL=1`` to disable automatic installation.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import platform
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

import httpx

INSTALL_VERSION = "2.3.2"
"""Pinned OpenCode release tested against the OpenCode 2 adapter."""

RELEASES = "https://github.com/anomalyco/opencode/releases/download"
NO_INSTALL = "INTANDEM_NO_INSTALL"


class InstallError(RuntimeError):
    """Raised when downloading, verifying, or extracting OpenCode fails."""


def install_dir() -> Path:
    return Path.home() / ".opencode" / "bin"


def asset_name(system: str | None = None, machine: str | None = None) -> str:
    """Return the GitHub release asset filename for the given platform and architecture."""
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise InstallError(f"no OpenCode build for the {machine} processor")
    if system.startswith("linux"):
        variant = "-baseline" if arch == "x64" and not _has_avx2() else ""
        libc = "-musl" if _is_musl() else ""
        return f"opencode-linux-{arch}{variant}{libc}.tar.gz"
    if system == "darwin":
        return f"opencode-darwin-{arch}.zip"
    raise InstallError(f"inTandem installs OpenCode on Linux and macOS, not {system}")


def install(version: str = INSTALL_VERSION, *, http: httpx.Client | None = None) -> Path:
    """Download, verify and unpack OpenCode ``version``. Returns the binary's path."""
    if os.environ.get(NO_INSTALL):
        raise InstallError(f"OpenCode is not installed, and {NO_INSTALL} is set")
    name = asset_name()
    base = f"{RELEASES}/v{version}"
    print(f"inTandem: OpenCode not found; installing {version} ({name})", file=sys.stderr)
    client = http or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(300, connect=15))
    try:
        sums = _get(client, f"{base}/SHA256SUMS").decode()
        archive = _get(client, f"{base}/{name}")
    finally:
        if http is None:
            client.close()
    expected = _expected_digest(sums, name)
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected:
        raise InstallError(f"{name} failed its checksum: expected {expected}, got {actual}")
    target = install_dir() / "opencode"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"opencode.{os.getpid()}.partial")
    try:
        partial.write_bytes(_extract(name, archive))
        partial.chmod(partial.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        partial.replace(target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            partial.unlink()
    print(f"inTandem: installed OpenCode {version} to {target}", file=sys.stderr)
    return target


def _get(client: httpx.Client, url: str) -> bytes:
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise InstallError(f"could not download {url}: {exc}") from exc
    return response.content


def _expected_digest(sums: str, name: str) -> str:
    for line in sums.splitlines():
        digest, _, file = line.strip().partition(" ")
        if file.strip().lstrip("*") == name:
            return digest.lower()
    raise InstallError(f"{name} is not listed in the release's SHA256SUMS")


def _extract(name: str, archive: bytes) -> bytes:
    """Extract the ``opencode`` binary from an in-memory ``.zip`` or ``.tar.gz`` archive."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            member = next((n for n in bundle.namelist() if Path(n).name == "opencode"), None)
            if member is None:
                raise InstallError(f"{name} holds no opencode binary")
            return bundle.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for member in bundle.getmembers():
            if member.isfile() and Path(member.name).name == "opencode":
                extracted = bundle.extractfile(member)
                if extracted is not None:
                    return extracted.read()
    raise InstallError(f"{name} holds no opencode binary")


def _is_musl() -> bool:
    return any(Path("/lib").glob("ld-musl-*.so.1"))


def _has_avx2() -> bool:
    try:
        return "avx2" in Path("/proc/cpuinfo").read_text()
    except OSError:
        return True
