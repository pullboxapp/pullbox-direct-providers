"""Protocol compatibility negotiation for direct-download providers."""

from __future__ import annotations

import re
from collections.abc import Sequence

_PROTOCOL_VERSION = re.compile(r"direct-download-provider/v[1-9][0-9]*\Z")


class IncompatibleProtocolError(ValueError):
    """Raised when Pullbox and a provider have no exact protocol intersection."""


def negotiate_protocol_version(
    *,
    pullbox_versions: Sequence[str],
    provider_versions: Sequence[str],
) -> str:
    """Return the highest exact protocol version shared by both participants."""
    pullbox = _validated_versions(pullbox_versions)
    provider = _validated_versions(provider_versions)
    intersection = pullbox.intersection(provider)
    if not intersection:
        raise IncompatibleProtocolError("No compatible direct-download protocol version.")
    return max(intersection, key=_major_version)


def _validated_versions(versions: Sequence[str]) -> set[str]:
    validated = {version for version in versions if _PROTOCOL_VERSION.fullmatch(version)}
    if len(validated) != len(set(versions)) and not validated:
        raise IncompatibleProtocolError("A protocol version claim is malformed.")
    return validated


def _major_version(version: str) -> int:
    return int(version.rpartition("v")[2])
