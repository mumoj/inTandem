"""Harness-independent file protection configuration.

Each backend enforces a ``Guard`` using its own mechanism (permission rulesets,
hooks, or approval callbacks). As a fallback, the supervisor also hashes
protected files to detect edits that bypass backend controls.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Guard:
    protected: tuple[str, ...] = ()
    """Glob patterns relative to the task directory that require approval before editing."""

    unattended: bool = True
    """Auto-approve routine operations so only protected file edits pause for review."""
