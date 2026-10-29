"""The authenticated control API over the supervisor."""

from intandem.bridge.app import create_app
from intandem.bridge.manifest import Manifest

__all__ = ["Manifest", "create_app"]
