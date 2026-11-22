"""Installing OpenCode when it is missing: platform choice, checksum, unpacking."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from intandem.opencode import install as installer
from intandem.opencode.server import resolve_binary

BASE = f"{installer.RELEASES}/v{installer.INSTALL_VERSION}"


def _tarball(content: bytes) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        info = tarfile.TarInfo("opencode")
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(installer.NO_INSTALL, raising=False)
    monkeypatch.setattr(installer, "asset_name", lambda: "opencode-linux-x64.tar.gz")
    return tmp_path


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("darwin", "arm64", "opencode-darwin-arm64.zip"),
        ("darwin", "x86_64", "opencode-darwin-x64.zip"),
    ],
)
def test_the_archive_matches_the_platform(system: str, machine: str, expected: str) -> None:
    assert installer.asset_name(system, machine) == expected


def test_linux_picks_the_variant_this_cpu_and_libc_need(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "_has_avx2", lambda: False)
    monkeypatch.setattr(installer, "_is_musl", lambda: True)
    assert installer.asset_name("linux", "x86_64") == "opencode-linux-x64-baseline-musl.tar.gz"
    monkeypatch.setattr(installer, "_has_avx2", lambda: True)
    monkeypatch.setattr(installer, "_is_musl", lambda: False)
    assert installer.asset_name("linux", "aarch64") == "opencode-linux-arm64.tar.gz"


def test_windows_and_unknown_processors_are_refused() -> None:
    with pytest.raises(installer.InstallError, match="Linux and macOS"):
        installer.asset_name("win32", "x86_64")
    with pytest.raises(installer.InstallError, match="riscv64"):
        installer.asset_name("linux", "riscv64")


@respx.mock
def test_a_verified_download_lands_executable_in_the_usual_place(home: Path) -> None:
    archive = _tarball(b"#!/bin/sh\necho opencode\n")
    digest = hashlib.sha256(archive).hexdigest()
    respx.get(f"{BASE}/SHA256SUMS").respond(
        text=f"{'0' * 64}  other.zip\n{digest}  opencode-linux-x64.tar.gz\n"
    )
    respx.get(f"{BASE}/opencode-linux-x64.tar.gz").respond(content=archive)

    path = installer.install(http=httpx.Client())

    assert path == home / ".opencode" / "bin" / "opencode"
    assert path.read_bytes() == b"#!/bin/sh\necho opencode\n"
    assert os.access(path, os.X_OK)


@respx.mock
def test_a_download_that_fails_its_checksum_is_not_installed(home: Path) -> None:
    respx.get(f"{BASE}/SHA256SUMS").respond(text=f"{'0' * 64}  opencode-linux-x64.tar.gz\n")
    respx.get(f"{BASE}/opencode-linux-x64.tar.gz").respond(content=_tarball(b"tampered"))
    with pytest.raises(installer.InstallError, match="checksum"):
        installer.install(http=httpx.Client())
    assert not (home / ".opencode" / "bin" / "opencode").exists()


def test_install_can_be_switched_off(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(installer.NO_INSTALL, "1")
    monkeypatch.setenv("PATH", str(home / "empty"))
    with pytest.raises(FileNotFoundError, match=installer.NO_INSTALL):
        resolve_binary("opencode")


def test_a_missing_default_binary_is_installed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(home / "empty"))
    target = home / "installed-opencode"
    monkeypatch.setattr(installer, "install", lambda: target)
    assert resolve_binary("opencode") == str(target)


def test_a_missing_custom_binary_is_reported_not_installed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(home / "empty"))

    def refuse() -> Path:
        raise AssertionError("a custom binary must not be installed")

    monkeypatch.setattr(installer, "install", refuse)
    with pytest.raises(FileNotFoundError, match="my-opencode"):
        resolve_binary("my-opencode")
