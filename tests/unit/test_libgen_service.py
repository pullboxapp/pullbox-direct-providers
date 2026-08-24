from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from pullbox_provider_contract.models import ResolverProfile, SearchIntent
from pullbox_provider_contract.source_http import BrowserChallengeRequiredError
from pullbox_provider_libgen.service import (
    LibGenProviderService,
    LibGenSourceOriginError,
    _build_queries,
    _search_url,
    validate_source_origin,
)
from pullbox_provider_libgen.transport import LibGenSourceError

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "libgen"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


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


class _SourceSession:
    def __init__(
        self,
        origin: str,
        *,
        search_html: str | None = None,
        fail: Exception | None = None,
        redirect_fail: Exception | None = None,
    ) -> None:
        self.origin = origin
        self.search_html = search_html or _fixture("search-results-v1.html")
        self.fail = fail
        self.redirect_fail = redirect_fail
        self.urls: list[str] = []
        self.max_bytes_by_url: dict[str, int] = {}
        self.closed = False

    async def fetch_text(self, url: str, *, max_bytes: int = 2 * 1024 * 1024) -> str:
        self.urls.append(url)
        self.max_bytes_by_url[url] = max_bytes
        if self.fail is not None:
            raise self.fail
        if "/index.php" in url:
            return self.search_html
        if "object=f" in url:
            return (
                _fixture("file-v1.json")
                if "0123456789abcdef0123456789abcdef" in url
                else _fixture("file-sparse-v1.json")
            )
        if "ids=910" in url:
            return _fixture("edition-v1.json")
        return "{}"

    async def resolve_redirect(self, url: str) -> str:
        self.urls.append(url)
        if self.redirect_fail is not None:
            raise self.redirect_fail
        if self.fail is not None:
            raise self.fail
        return "https://downloads.example/clockwork-harbor-003.cbz"

    async def aclose(self) -> None:
        self.closed = True


class _SessionFactory:
    def __init__(
        self,
        *,
        search_by_origin: dict[str, str] | None = None,
        failure_by_origin: dict[str, Exception] | None = None,
        redirect_failure_by_origin: dict[str, Exception] | None = None,
    ) -> None:
        self.search_by_origin = search_by_origin or {}
        self.failure_by_origin = failure_by_origin or {}
        self.redirect_failure_by_origin = redirect_failure_by_origin or {}
        self.sessions: list[_SourceSession] = []
        self.profiles: list[ResolverProfile | None] = []

    def __call__(
        self,
        origin: str,
        resolver_profile: ResolverProfile | None,
    ) -> _SourceSession:
        session = _SourceSession(
            origin,
            search_html=self.search_by_origin.get(origin),
            fail=self.failure_by_origin.get(origin),
            redirect_fail=self.redirect_failure_by_origin.get(origin),
        )
        self.sessions.append(session)
        self.profiles.append(resolver_profile)
        return session


def _search_intent() -> SearchIntent:
    return SearchIntent(
        series_title="Clockwork Harbor",
        normalized_title="clockwork harbor",
        issue_number="3",
        year=2024,
    )


async def test_service_search_discovers_enriches_deduplicates_and_caches() -> None:
    factory = _SessionFactory()
    service = LibGenProviderService(
        session_factory=factory,
        origin_resolver=_public_resolver,
    )
    profile = ResolverProfile(
        endpoint="http://resolver:8191",
        timeout_seconds=30,
        max_concurrency=1,
        declared_domains=["libgen.gl"],
    )

    first = await service.search(
        _search_intent(),
        provider_config={"source_url": "https://libgen.gl"},
        limit=10,
        resolver_profile=profile,
    )
    second = await service.search(
        _search_intent(),
        provider_config={"source_url": "https://libgen.gl"},
        limit=10,
        resolver_profile=profile,
    )

    assert first == second
    assert [candidate.provider_candidate_id for candidate in first] == [
        "libgen:0123456789abcdef0123456789abcdef",
        "libgen:fedcba9876543210fedcba9876543210",
    ]
    assert len(factory.sessions) == 1
    assert factory.profiles == [profile]
    assert factory.sessions[0].closed is True
    assert sum("/index.php" in url for url in factory.sessions[0].urls) == 2
    assert sum("object=f" in url for url in factory.sessions[0].urls) == 2
    assert all(
        factory.sessions[0].max_bytes_by_url[url] == 512 * 1024
        for url in factory.sessions[0].urls
        if "object=" in url
    )


async def test_zero_results_are_not_misclassified_as_failover() -> None:
    factory = _SessionFactory(
        search_by_origin={"https://libgen.gl": _fixture("search-zero-v1.html")}
    )
    service = LibGenProviderService(session_factory=factory, origin_resolver=_public_resolver)

    assert (
        await service.search(
            _search_intent(),
            provider_config={"source_url": "https://libgen.gl"},
            limit=10,
        )
        == []
    )
    assert [session.origin for session in factory.sessions] == ["https://libgen.gl"]


async def test_search_fails_over_once_for_temporary_source_failure() -> None:
    factory = _SessionFactory(
        search_by_origin={"https://libgen.li": _fixture("search-zero-v1.html")},
        failure_by_origin={
            "https://libgen.gl": LibGenSourceError("source_unavailable", "temporary")
        },
    )
    service = LibGenProviderService(session_factory=factory, origin_resolver=_public_resolver)

    assert (
        await service.search(
            _search_intent(),
            provider_config={"source_url": "https://libgen.gl"},
            limit=10,
        )
        == []
    )
    assert [session.origin for session in factory.sessions] == [
        "https://libgen.gl",
        "https://libgen.li",
    ]
    assert all(session.closed for session in factory.sessions)


async def test_resolve_revalidates_by_md5_and_returns_generic_https_artifact() -> None:
    factory = _SessionFactory()
    service = LibGenProviderService(session_factory=factory, origin_resolver=_public_resolver)

    artifacts = await service.resolve(
        "libgen:0123456789abcdef0123456789abcdef",
        provider_config={"source_url": "https://libgen.gl"},
    )

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.artifact_id == "libgen-direct:0123456789abcdef0123456789abcdef"
    assert artifact.coverage.issue_numbers == ["3"]
    assert artifact.coverage.description == "Clockwork Harbor"
    assert artifact.route == "direct_artifact"
    assert artifact.format == "cbz"
    assert artifact.size_bytes == 18 * 1024 * 1024
    assert len(artifact.mirrors) == 1
    assert artifact.mirrors[0].host_kind == "generic_https"
    assert artifact.mirrors[0].final_url == "https://downloads.example/clockwork-harbor-003.cbz"
    assert artifact.mirrors[0].checksum == "md5:0123456789abcdef0123456789abcdef"
    assert factory.sessions[0].closed is True


@pytest.mark.parametrize(
    "candidate_id",
    [
        "anna:0123456789abcdef0123456789abcdef",
        "libgen:not-an-md5",
        "libgen:0123456789ABCDEF0123456789ABCDEF",
    ],
)
async def test_resolve_rejects_invalid_candidate_identity_without_source_io(
    candidate_id: str,
) -> None:
    factory = _SessionFactory()
    service = LibGenProviderService(session_factory=factory, origin_resolver=_public_resolver)

    with pytest.raises(ValueError, match="candidate"):
        await service.resolve(
            candidate_id,
            provider_config={"source_url": "https://libgen.gl"},
        )

    assert factory.sessions == []


async def test_missing_resolver_error_is_returned_after_bounded_failover() -> None:
    factory = _SessionFactory(
        failure_by_origin={
            "https://libgen.gl": BrowserChallengeRequiredError(),
            "https://libgen.li": BrowserChallengeRequiredError(),
        }
    )
    service = LibGenProviderService(session_factory=factory, origin_resolver=_public_resolver)

    with pytest.raises(BrowserChallengeRequiredError):
        await service.search(
            _search_intent(),
            provider_config={"source_url": "https://libgen.gl"},
            limit=10,
        )

    assert len(factory.sessions) == 2


async def test_resolve_does_not_fail_over_to_work_around_unsafe_destination() -> None:
    factory = _SessionFactory(
        redirect_failure_by_origin={
            "https://libgen.gl": LibGenSourceError(
                "artifact_unavailable",
                "unsafe destination",
                retryable=False,
            )
        }
    )
    service = LibGenProviderService(session_factory=factory, origin_resolver=_public_resolver)

    with pytest.raises(LibGenSourceError, match="unsafe"):
        await service.resolve(
            "libgen:0123456789abcdef0123456789abcdef",
            provider_config={"source_url": "https://libgen.gl"},
        )

    assert [session.origin for session in factory.sessions] == ["https://libgen.gl"]
