"""Antigravity CLI (``agy``) implementer backend communicating over ACP stdio.

Uses only the standard library (``asyncio`` subprocesses and newline-delimited
JSON) and requires a signed-in ``agy`` executable on ``PATH``.
"""

from intandem.antigravity.backend import AntigravityBackend

__all__ = ["AntigravityBackend"]
