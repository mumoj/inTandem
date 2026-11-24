"""Helpers for safely extracting typed values from nested JSON dictionaries."""

from __future__ import annotations

from typing import cast


def dig(data: object, *keys: str) -> object:
    """Traverse nested dicts along ``keys``, returning ``None`` if any key is missing."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = cast(dict[str, object], data).get(key)
    return data


def dig_str(data: object, *keys: str) -> str | None:
    value = dig(data, *keys)
    return value if isinstance(value, str) else None


def dig_list(data: object, *keys: str) -> list[object]:
    value = dig(data, *keys)
    return cast(list[object], value) if isinstance(value, list) else []
