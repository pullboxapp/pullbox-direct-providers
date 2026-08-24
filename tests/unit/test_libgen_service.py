from __future__ import annotations

from collections.abc import Sequence

import pytest
from pullbox_provider_libgen.service import (
    LibGenSourceOriginError,
    validate_source_origin,
)


async def _public_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34",)


async def _private_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("127.0.0.1",)


async def _mixed_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34", "10.0.0.1")


async def _unavailable_resolver(_host: str, _port: int) -> Sequence[str]:
    raise OSError("source DNS unavailable")


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("https://libgen.gl", "https://libgen.gl"),
        ("https://custom-libgen.example/", "https://custom-libgen.example"),
        ("https://CUSTOM-LIBGEN.EXAMPLE:443", "https://custom-libgen.example"),
    ],
)
async def test_validate_source_origin_accepts_public_https_roots(
    raw_url: str,
    expected: str,
) -> None:
    assert await validate_source_origin(raw_url, resolver=_public_resolver) == expected


@pytest.mark.parametrize(
    "raw_url",
    [
        "http://libgen.gl",
        "https://user:secret@libgen.gl",
        "https://libgen.gl/comics",
        "https://libgen.gl?topic=c",
        "https://libgen.gl#results",
        "https://libgen.gl:444",
        "https://127.0.0.1",
        "https://2130706433",
        "https://0x7f000001",
        "https://017700000001",
        "https://source.localhost",
        "https://source.local",
        "https://source.onion",
        "https://source.internal",
        "https://source.home.arpa",
    ],
)
async def test_validate_source_origin_rejects_unsafe_url_shapes(raw_url: str) -> None:
    with pytest.raises(LibGenSourceOriginError):
        await validate_source_origin(raw_url, resolver=_public_resolver)


@pytest.mark.parametrize("resolver", [_private_resolver, _mixed_resolver, _unavailable_resolver])
async def test_validate_source_origin_rejects_unresolved_or_non_public_dns(resolver) -> None:
    with pytest.raises(LibGenSourceOriginError):
        await validate_source_origin("https://libgen.example", resolver=resolver)
