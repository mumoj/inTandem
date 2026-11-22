"""The Claude Code implementer backend, over the Claude Agent SDK.

Optional: it needs the ``claude`` extra (``claude-agent-sdk``) and a logged-in
``claude`` CLI. Nothing here imports the SDK at module level, so inTandem
without the extra still imports this package, and fails with a clear error only
when a task picks the claude harness.
"""

from intandem.claude.sdk import api_key_warning

__all__ = ["api_key_warning"]
