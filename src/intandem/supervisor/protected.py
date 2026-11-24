"""Protection for files the implementer is not allowed to modify.

Enforced in two layers:
* a ``Guard`` passed to the backend so edit attempts pause for approval, and
* SHA-256 content hashes checked periodically to detect edits that bypass
  backend tool permissions (e.g. via shell redirection or scripts).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from intandem.backend.guard import Guard

MISSING = "<missing>"


@dataclass(frozen=True, slots=True)
class ProtectedFiles:
    root: Path
    patterns: tuple[str, ...] = ()

    def guard(self) -> Guard:
        """Build a backend-independent ``Guard`` from these glob patterns."""
        return Guard(protected=self.patterns)

    def snapshot(self) -> dict[str, str]:
        """Hash every matching file, recording ``MISSING`` for patterns with no matches.

        Storing unmatched patterns ensures that creating a new file matching a
        protected glob is detected as a change.
        """
        state: dict[str, str] = {}
        for pattern in self.patterns:
            matches = sorted(path for path in self.root.glob(pattern) if path.is_file())
            if not matches:
                state[pattern] = MISSING
            for path in matches:
                state[str(path.relative_to(self.root))] = _digest(path)
        return state

    def changes(self, baseline: dict[str, str]) -> list[str]:
        """Return sorted paths or patterns that differ from ``baseline``.

        ``baseline`` must have been taken with the same pattern set; comparing
        snapshots from different patterns would report every newly added or
        removed pattern as a change.
        """
        current = self.snapshot()
        return sorted(
            name
            for name in current.keys() | baseline.keys()
            if current.get(name) != baseline.get(name)
        )

    def with_patterns(self, patterns: tuple[str, ...]) -> ProtectedFiles:
        return ProtectedFiles(self.root, patterns)

    def entry_changes(self, baseline: dict[str, str]) -> list[str]:
        """Check recorded ``baseline`` entries directly when the original patterns are unavailable.

        Verifies existing file hashes and confirms previously unmatched patterns
        still match nothing. Used as a fallback for older state files that did
        not store ``protected_patterns``.
        """
        changed: list[str] = []
        for name, recorded in baseline.items():
            if recorded == MISSING:
                if any(path.is_file() for path in self.root.glob(name)):
                    changed.append(name)
            elif _digest(self.root / name) != recorded:
                changed.append(name)
        return sorted(changed)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return MISSING
