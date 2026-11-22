"""Which file edits a ``Guard`` stops, for the Claude Code tools.

Matching errs wide: a path counts as protected when the glob, applied the way
``ProtectedFiles.snapshot`` applies it, finds it, *or* when ``fnmatch`` matches
its path relative to the task. The second catches files that do not exist yet,
and a ``*`` there also crosses ``/``. An edit wrongly stopped costs the planner
one reply; an edit wrongly let through costs a protected file.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from intandem.backend.jsonpath import dig_str
from intandem.claude.translate import EDIT_TOOLS


def edit_paths(tool: str, tool_input: object) -> list[str]:
    """The file(s) an editing tool call writes, as the model gave them."""
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
        return None  # Outside the task: not ours to protect.


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
    """The task-relative paths this call would edit that ``patterns`` protect."""
    if not patterns:
        return []
    hits: list[str] = []
    for path in edit_paths(tool, tool_input):
        relative = _relative(path, root)
        if relative is not None and any(matches(relative, p, root) for p in patterns):
            hits.append(relative)
    return hits
