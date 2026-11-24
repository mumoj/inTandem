"""Claude Code implementer backend built on ``claude-agent-sdk``.

The SDK is an optional extra and is imported lazily so inTandem can run without
it unless a task explicitly selects ``harness="claude"``.
"""

from intandem.claude.sdk import api_key_warning

__all__ = ["api_key_warning"]
