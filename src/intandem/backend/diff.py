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
