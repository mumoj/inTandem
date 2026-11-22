"""Worktree diff from git, reusable by any implementer backend."""

from __future__ import annotations

import asyncio
from pathlib import Path

from intandem.backend.errors import BackendError
from intandem.backend.models import FileDiff


async def _git(cwd: Path, *args: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
    except OSError as exc:
        raise BackendError(f"git {args[0]} failed: {exc}") from exc
    if process.returncode != 0:
        raise BackendError(f"git {args[0]} failed: {err.decode(errors='replace')[:300]}")
    return out.decode(errors="replace")


async def git_head(cwd: Path) -> str | None:
    """The current HEAD commit in ``cwd``, or ``None`` outside a git repository."""
    try:
        return (await _git(cwd, "rev-parse", "HEAD")).strip() or None
    except BackendError:
        return None


async def git_diff(cwd: Path, base: str | None = None) -> list[FileDiff]:
    """What changed in ``cwd`` since ``base`` (or HEAD), plus untracked files as added."""
    target = base or "HEAD"
    try:
        numstat = await _git(cwd, "diff", "--numstat", "--no-renames", target)
        statuses = await _git(cwd, "diff", "--name-status", "--no-renames", target)
        untracked = await _git(cwd, "ls-files", "--others", "--exclude-standard")
    except BackendError:
        return []
    status_of: dict[str, str] = {}
    for line in statuses.splitlines():
        code, _, rest = line.partition("\t")
        name = rest.split("\t")[-1]
        status_of[name] = {"A": "added", "D": "deleted"}.get(code[:1], "modified")
    diffs: list[FileDiff] = []
    for line in numstat.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3:
            continue
        added, removed, name = fields
        diffs.append(
            FileDiff(
                file=name,
                additions=float(added) if added.isdigit() else 0,
                deletions=float(removed) if removed.isdigit() else 0,
                status=status_of.get(name, "modified"),
            )
        )
    for name in untracked.splitlines():
        lines = _line_count(cwd / name)
        diffs.append(FileDiff(file=name, additions=lines, status="added"))
    return diffs


def _line_count(path: Path) -> float:
    try:
        with path.open("rb") as handle:
            return float(sum(1 for _ in handle))
    except OSError:
        return 0


def _resolve_in_repo(cwd: Path, file_path: str) -> tuple[Path, str]:
    """Resolve ``file_path`` inside ``cwd`` and return ``(absolute, posix_relative)``."""
    if not file_path or file_path.startswith("/") or "\x00" in file_path:
        raise ValueError(f"invalid file path: {file_path!r}")
    root = cwd.resolve()
    target = (root / file_path).resolve()
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes task directory: {file_path!r}") from exc
    if not relative or relative == ".":
        raise ValueError(f"invalid file path: {file_path!r}")
    return target, relative


async def git_file_patch(
    cwd: Path, file_path: str, base: str | None = None, *, max_chars: int = 200_000
) -> tuple[str, bool]:
    """Unified diff for one file in ``cwd`` (including untracked files).

    Returns ``(patch, truncated)``. Raises ``ValueError`` if ``file_path``
    escapes ``cwd``.
    """
    target, relative = _resolve_in_repo(cwd, file_path)
    rev = base or "HEAD"
    patch = ""
    try:
        patch = await _git(cwd, "diff", "--no-color", "--no-ext-diff", rev, "--", relative)
    except BackendError:
        patch = ""
    if not patch and target.is_file():
        patch = _untracked_patch(target, relative)
    if len(patch) > max_chars:
        return patch[:max_chars], True
    return patch, False


def _untracked_patch(path: Path, relative: str) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if b"\x00" in raw[:8192]:
        return f"--- /dev/null\n+++ b/{relative}\nBinary file {relative} added\n"
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if not lines:
        return f"--- /dev/null\n+++ b/{relative}\n@@ -0,0 +0,0 @@\n"
    body = "\n".join(f"+{line}" for line in lines)
    return f"--- /dev/null\n+++ b/{relative}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"
