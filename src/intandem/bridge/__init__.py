"""Authenticated HTTP control API wrapping the supervisor."""

from intandem.bridge.app import create_app
from intandem.bridge.manifest import Manifest

__all__ = ["Manifest", "create_app"]
