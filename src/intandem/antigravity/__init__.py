"""The Antigravity CLI (``agy``) implementer backend, over ACP on stdio.

No extra dependencies: the ACP client is the stdlib's ``asyncio`` subprocess
and newline-delimited JSON. It needs ``agy`` on PATH, signed in.
"""

from intandem.antigravity.backend import AntigravityBackend

__all__ = ["AntigravityBackend"]
