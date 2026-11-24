"""Matches Claude Code file-editing tool calls against ``Guard`` protected patterns.

Matching errs on the side of caution by checking both ``Path.glob`` (like
``ProtectedFiles.snapshot``) and ``fnmatch`` against task-relative paths, which
also catches paths that do not exist on disk yet.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from intandem.backend.jsonpath import dig_str
from intandem.claude.translate import EDIT_TOOLS


def edit_paths(tool: str, tool_input: object) -> list[str]:
    """Return file paths targeted by a file-editing tool call."""
    if tool not in EDIT_TOOLS:
        return []
    path = dig_str(tool_input, "file_path") or dig_str(tool_input, "notebook_path")
    return [path] if path else []


def _relative(path: str, root: Path) -> str | None:
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None  # Path is outside the task directory.


def matches(relative: str, pattern: str, root: Path) -> bool:
    if fnmatch.fnmatchcase(relative, pattern):
        return True
    if fnmatch.fnmatchcase(relative, pattern.replace("**/", "")):
        return True
    try:
        return any(found.relative_to(root).as_posix() == relative for found in root.glob(pattern))
    except (ValueError, NotImplementedError):
        return False


def protected_paths(
    tool: str, tool_input: object, root: Path, patterns: tuple[str, ...]
) -> list[str]:
    """Return task-relative paths edited by this tool call that match ``patterns``."""
    if not patterns:
        return []
    hits: list[str] = []
    for path in edit_paths(tool, tool_input):
        relative = _relative(path, root)
        if relative is not None and any(matches(relative, p, root) for p in patterns):
            hits.append(relative)
    return hits
