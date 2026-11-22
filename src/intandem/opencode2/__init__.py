"""The OpenCode 2.x server API: a second adapter behind ``ImplementerBackend``.

OpenCode 2 rebuilt its server API from scratch (``/api/...`` routes, location
scoping, forms instead of questions, a new event vocabulary) and serves it
from the same ``opencode serve`` command. Shapes here were recorded from a
real 2.3.2 server driven by the scripted model; see ``tests/fixtures/opencode2``.
"""

from intandem.opencode2.client import OpenCode2Client

__all__ = ["OpenCode2Client"]
