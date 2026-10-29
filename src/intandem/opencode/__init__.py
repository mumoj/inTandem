"""Everything that talks to OpenCode: models, client, event stream, shared host."""

from intandem.opencode.client import NotFoundError, OpenCodeAPI, OpenCodeClient, OpenCodeError
from intandem.opencode.events import Event, StreamRecorder
from intandem.opencode.models import ModelRef, PermissionAction, PermissionRule
from intandem.opencode.server import UnsupportedVersionError, Version

__all__ = [
    "Event",
    "ModelRef",
    "NotFoundError",
    "OpenCodeAPI",
    "OpenCodeClient",
    "OpenCodeError",
    "PermissionAction",
    "PermissionRule",
    "StreamRecorder",
    "UnsupportedVersionError",
    "Version",
]
