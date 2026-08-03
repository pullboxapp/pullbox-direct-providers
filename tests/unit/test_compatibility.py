from __future__ import annotations

import pytest
from pullbox_provider_contract.compatibility import (
    IncompatibleProtocolError,
    negotiate_protocol_version,
)


def test_negotiation_selects_the_supported_protocol_intersection() -> None:
    assert (
        negotiate_protocol_version(
            pullbox_versions=["direct-download-provider/v1"],
            provider_versions=[
                "direct-download-provider/v2",
                "direct-download-provider/v1",
            ],
        )
        == "direct-download-provider/v1"
    )


@pytest.mark.parametrize(
    "provider_versions",
    [
        [],
        ["direct-download-provider/v2"],
        ["v1"],
        ["direct-download-provider/v1.1"],
    ],
)
def test_negotiation_fails_closed_without_an_exact_supported_version(
    provider_versions: list[str],
) -> None:
    with pytest.raises(IncompatibleProtocolError):
        negotiate_protocol_version(
            pullbox_versions=["direct-download-provider/v1"],
            provider_versions=provider_versions,
        )


def test_negotiation_is_deterministic_for_duplicate_version_claims() -> None:
    assert (
        negotiate_protocol_version(
            pullbox_versions=["direct-download-provider/v1"],
            provider_versions=[
                "direct-download-provider/v1",
                "direct-download-provider/v1",
            ],
        )
        == "direct-download-provider/v1"
    )
