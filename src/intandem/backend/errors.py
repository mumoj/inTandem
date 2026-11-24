"""Exceptions raised by implementer backends."""

from __future__ import annotations


class BackendError(RuntimeError):
    """Raised when a backend returns an error or cannot be reached."""


class NotFoundError(BackendError):
    """Raised when a session, permission request, or question no longer exists."""
