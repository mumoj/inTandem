"""Which ACP tool calls a ``Guard`` stops, and what the planner is shown about them.

An ACP ``toolCall`` says what it touches in several places, depending on the
tool: ``locations[*].path``, its ``rawInput`` (``TargetFile``, ``path``, ...)
and agy's own ``args``. Every one of them counts, and matching errs wide the
same way as ``claude/guard.py``: a path is protected when its task-relative
form matches a pattern under ``fnmatchcase``, or does with ``**/`` collapsed,
or when the pattern's ``root.glob`` finds it. An edit wrongly stopped costs
the planner one reply; an edit wrongly let through costs a protected file.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from intandem.backend.jsonpath import dig, dig_list, dig_str
from intandem.claude.guard import matches

EDIT_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
        "Edit",
        "Write",
        "MultiEdit",
    }
)
"""Tool names that write files, whatever ``kind`` the agent gave the call."""

SHELL_TOOLS = frozenset({"run_command", "run_shell_command", "Bash", "shell", "execute"})
"""Tool names that run a shell command."""

PATH_KEYS = ("Path", "TargetFile", "AbsolutePath", "path", "file_path", "filePath")
COMMAND_KEYS = ("CommandLine", "command", "Command", "cmd")
_PREVIEW_LINES = 120


def tool_name(call: object) -> str:
    """The tool's name, or failing that its title or kind."""
    return dig_str(call, "name") or dig_str(call, "title") or dig_str(call, "kind") or "tool"


def is_edit(call: object) -> bool:
    return dig_str(call, "kind") == "edit" or dig_str(call, "name") in EDIT_TOOLS


def is_shell(call: object) -> bool:
    return dig_str(call, "kind") == "execute" or dig_str(call, "name") in SHELL_TOOLS


def edit_paths(call: object) -> list[str]:
    """Every file an editing call says it writes, as the agent gave them, once each."""
    if not is_edit(call):
        return []
    found: list[str] = []
    for location in dig_list(call, "locations"):
        if path := dig_str(location, "path"):
            found.append(path)
    for container in ("rawInput", "args"):
        for key in PATH_KEYS:
            if path := dig_str(call, container, key):
                found.append(path)
    for entry in dig_list(call, "content"):
        if dig_str(entry, "type") == "diff" and (path := dig_str(entry, "path")):
            found.append(path)
    return list(dict.fromkeys(found))


def _relative(path: str, root: Path) -> str | None:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = root / target
    try:
        return target.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None  # Outside the task: not ours to protect.


def protected_paths(call: object, root: Path, patterns: tuple[str, ...]) -> list[str]:
    """The task-relative paths this call would edit that ``patterns`` protect."""
    if not patterns:
        return []
    hits: list[str] = []
    for path in edit_paths(call):
        relative = _relative(path, root)
        if relative is not None and any(matches(relative, p, root) for p in patterns):
            hits.append(relative)
    return list(dict.fromkeys(hits))


def command(call: object) -> str | None:
    """The shell command a call runs, if it is a shell tool."""
    if not is_shell(call):
        return None
    for container in ("rawInput", "args"):
        for key in COMMAND_KEYS:
            if value := dig_str(call, container, key):
                return value
    return None


def preview_metadata(call: object) -> dict[str, str]:
    """``command`` or ``diff`` for the dashboard's permission card."""
    if shell := command(call):
        return {"command": shell}
    diff = edit_preview(call)
    return {"diff": diff} if diff else {}


def edit_preview(call: object) -> str:
    """A unified diff of what an editing call would write, bounded."""
    if not is_edit(call):
        return ""
    chunks: list[str] = []
    for entry in dig_list(call, "content"):
        if dig_str(entry, "type") != "diff":
            continue
        path = dig_str(entry, "path") or ""
        chunks.append(
            unified(dig_str(entry, "oldText") or "", dig_str(entry, "newText") or "", path)
        )
    if not chunks:
        chunks = _input_chunks(call)
    lines = "\n".join(c for c in chunks if c).splitlines()
    if len(lines) > _PREVIEW_LINES:
        lines = [*lines[:_PREVIEW_LINES], f"... ({len(lines) - _PREVIEW_LINES} more lines)"]
    return "\n".join(lines)


def _input_chunks(call: object) -> list[str]:
    """Diffs rebuilt from the tool's input, when it sent no ``diff`` content."""
    paths = edit_paths(call)
    path = paths[0] if paths else ""
    for container in ("rawInput", "args"):
        tool_input: object = dig(call, container)
        if tool_input is None:
            continue
        chunks: list[str] = []
        for chunk in dig_list(tool_input, "ReplacementChunks") or dig_list(tool_input, "edits"):
            chunks.append(_replacement(chunk, path))
        if not chunks and (single := _replacement(tool_input, path)):
            chunks.append(single)
        if not chunks:
            for key in ("CodeContent", "content", "Content", "text"):
                if content := dig_str(tool_input, key):
                    chunks.append(unified("", content, path))
                    break
        if chunks:
            return chunks
    return []


def _replacement(chunk: object, path: str) -> str:
    pairs = (("TargetContent", "ReplacementContent"), ("old_string", "new_string"))
    for old_key, new_key in pairs:
        old = dig_str(chunk, old_key)
        new = dig_str(chunk, new_key)
        if old is not None or new is not None:
            return unified(old or "", new or "", path)
    return ""


def unified(old: str, new: str, path: str = "") -> str:
    """``old`` to ``new`` as a unified diff, headed by ``path`` when there is one."""
    name = path or "file"
    diff = difflib.unified_diff(
        old.splitlines(),
        new.splitlines(),
        fromfile=f"a/{name}" if old else "/dev/null",
        tofile=f"b/{name}",
        lineterm="",
    )
    return "\n".join(diff)
