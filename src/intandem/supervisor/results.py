"""Parsing and validation for the implementer's JSON result file.

Implementers write a structured JSON file as soon as the outcome is known rather
than relying on regexes over free-form markdown output, so formatting variations
or late generation failures do not hide the result.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError


class ImplementerResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    spec_id: str
    verdict: str
    summary: str = ""


def read_result(path: Path, spec_id: str) -> ImplementerResult | None:
    """Read and validate the result file for ``spec_id``, returning ``None`` if not ready.

    Missing, partially written, malformed, or mismatched files return ``None``
    so the monitor can retry on the next tick.
    """
    try:
        result = ImplementerResult.model_validate_json(path.read_bytes())
    except (OSError, ValidationError):
        return None
    # Ignore results left behind by an earlier spec.
    return result if result.spec_id == spec_id else None
