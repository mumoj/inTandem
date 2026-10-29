"""Everything that talks to OpenCode: models, client, event stream, shared host."""

from tandem.opencode.client import NotFoundError, OpenCodeAPI, OpenCodeClient, OpenCodeError
from tandem.opencode.events import Event, StreamRecorder
from tandem.opencode.models import ModelRef, PermissionAction, PermissionRule
from tandem.opencode.server import UnsupportedVersionError, Version

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
