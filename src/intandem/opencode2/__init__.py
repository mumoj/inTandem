"""``ImplementerBackend`` adapter for the OpenCode 2.x HTTP/SSE server API.

OpenCode 2 uses ``/api/...`` routes, location-scoped requests, form-based
questions, and a revised event format. Wire shapes were verified against
OpenCode 2.3.2 (see ``tests/fixtures/opencode2``).
"""

from intandem.opencode2.client import OpenCode2Client

__all__ = ["OpenCode2Client"]
