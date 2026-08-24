from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import parse_qs, urlsplit

import pytest
from pullbox_provider_contract.models import SearchIntent
from pullbox_provider_libgen.service import (
    LibGenSourceOriginError,
    _build_queries,
    _search_url,
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


def test_build_queries_is_bounded_deterministic_and_uses_one_alternate() -> None:
    intent = SearchIntent(
        series_title="Clockwork Harbor",
        normalized_title="clockwork harbor",
        alternate_titles=["The Clockwork Harbor", "Clockwork Harbour"],
        issue_number="3",
        year=2024,
    )

    assert _build_queries(intent) == [
        "Clockwork Harbor 3 2024",
        "Clockwork Harbor 3",
        "The Clockwork Harbor 3 2024",
    ]


def test_build_queries_prefers_collection_title_then_volume() -> None:
    intent = SearchIntent(
        series_title="Clockwork Chronicles",
        normalized_title="clockwork chronicles",
        issue_type="TPB",
        issue_title="The End of Tides",
        issue_number="2",
        volume="2",
        year=2025,
    )

    assert _build_queries(intent) == [
        "Clockwork Chronicles The End of Tides 2025",
        "Clockwork Chronicles Vol 2 2025",
        "Clockwork Chronicles The End of Tides",
    ]


def test_build_queries_limits_query_length_and_count() -> None:
    title = "A" * 500
    intent = SearchIntent(
        series_title=title,
        normalized_title=title.casefold(),
        alternate_titles=["B" * 500, "C" * 500],
        issue_number="999",
        year=2024,
    )

    queries = _build_queries(intent)

    assert 1 <= len(queries) <= 3
    assert all(len(query) <= 500 for query in queries)
    assert len(set(queries)) == len(queries)


def test_search_url_uses_closed_comics_file_query_parameters() -> None:
    url = _search_url("https://libgen.gl", "Clockwork Harbor 3")
    parsed = urlsplit(url)

    assert parsed.scheme == "https"
    assert parsed.netloc == "libgen.gl"
    assert parsed.path == "/index.php"
    assert parse_qs(parsed.query) == {
        "req": ["Clockwork Harbor 3"],
        "columns[]": ["t", "s"],
        "objects[]": ["f"],
        "topics[]": ["c"],
        "res": ["25"],
        "filesuns": ["all"],
    }
