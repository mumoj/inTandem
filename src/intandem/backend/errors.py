"""What can go wrong talking to an implementer backend, whichever harness it is."""

from __future__ import annotations


class BackendError(RuntimeError):
    """The backend answered with an error, or could not be reached at all."""


class NotFoundError(BackendError):
    """The session, permission or question no longer exists."""
