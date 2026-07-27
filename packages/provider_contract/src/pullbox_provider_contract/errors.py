"""Protocol-safe provider error types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProtocolError(Exception):
    """An expected provider-protocol failure safe to return to Pullbox."""

    status_code: int
    code: str
    message: str
