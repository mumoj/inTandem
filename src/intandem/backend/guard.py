"""What a backend must stop the implementer from doing, in inTandem's own terms.

Each harness enforces a ``Guard`` its own way: OpenCode as an ordered
permission ruleset, another harness as a hook or an approval policy. None of
that syntax belongs here. Whatever the backend manages, the supervisor's
content hash still catches an edit that gets past it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Guard:
    protected: tuple[str, ...] = ()
    """Globs, relative to the task directory, that must not be edited unasked."""

    unattended: bool = True
    """Run without stopping for routine approvals, so only protected edits wait."""
