"""Stateless LibGen discovery and resolution behavior."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import structlog
from pullbox_provider_contract.models import (
    Artifact,
    ArtifactCoverage,
    ArtifactRoute,
    Candidate,
    Mirror,
    ProviderStatus,
    ResolverProfile,
    SearchIntent,
)
from pullbox_provider_contract.resolver import ProviderResolverError
from pullbox_provider_contract.search_terms import collection_title_fragment, is_collection_intent
from pullbox_provider_contract.source_http import BrowserChallengeRequiredError

from pullbox_provider_libgen.cache import BoundedTTLCache
from pullbox_provider_libgen.metadata import (
    EditionMetadata,
    FileMetadata,
    LibGenMetadataEnricher,
    LibGenMetadataError,
    _edition_metadata_url,
    _file_metadata_url,
    parse_edition_metadata,
    parse_file_metadata_by_md5,
)
from pullbox_provider_libgen.parser import LibGenLayoutError, parse_search_html
from pullbox_provider_libgen.transport import LibGenSourceError, LibGenSourceSession

KNOWN_SOURCE_DOMAINS = (
    "libgen.gl",
    "libgen.li",
    "libgen.vg",
    "libgen.la",
    "libgen.bz",
)
KNOWN_SOURCE_URLS = tuple(f"https://{domain}" for domain in KNOWN_SOURCE_DOMAINS)
DEFAULT_SOURCE_URL = "https://libgen.gl"
_SPECIAL_USE_SOURCE_SUFFIXES = (
    "localhost",
    "local",
    "onion",
    "internal",
    "home.arpa",
)
_MAX_METADATA_BYTES = 512 * 1024
_LOGGER = structlog.get_logger(__name__)

SourceResolver = Callable[[str, int], Awaitable[Sequence[str]]]
_CANDIDATE_ID = re.compile(r"\Alibgen:(?P<md5>[0-9a-f]{32})\Z")


class SourceSession(Protocol):
    async def fetch_text(self, url: str, *, max_bytes: int = ...) -> str: ...

    async def resolve_redirect(self, url: str) -> str: ...

    async def aclose(self) -> None: ...


SessionFactory = Callable[[str, ResolverProfile | None], SourceSession]


class LibGenSourceOriginError(ValueError):
    """The configured LibGen source origin is unsafe or unavailable."""


async def validate_source_origin(
    raw_url: str,
    *,
    resolver: SourceResolver | None = None,
) -> str:
    """Return one normalized public HTTPS origin after DNS validation."""
    if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 2_000:
        raise LibGenSourceOriginError("LibGen source URL must be a bounded HTTPS origin.")
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise LibGenSourceOriginError("LibGen source URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LibGenSourceOriginError(
            "LibGen source URL must be one HTTPS origin without credentials or a path."
        )

    host = parsed.hostname.casefold().rstrip(".")
    if not _is_public_source_hostname_syntax(host):
        raise LibGenSourceOriginError("LibGen source URL must use a public hostname.")
    resolve = resolver or _resolve_addresses
    try:
        raw_addresses = await resolve(host, 443)
        addresses = tuple(ipaddress.ip_address(value) for value in raw_addresses)
    except (OSError, TimeoutError, ValueError) as exc:
        raise LibGenSourceOriginError("LibGen source URL could not be resolved safely.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise LibGenSourceOriginError("LibGen source URL must resolve only to public addresses.")
    return urlunsplit(("https", host, "", "", ""))


def _is_public_source_hostname_syntax(hostname: str) -> bool:
    if any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _SPECIAL_USE_SOURCE_SUFFIXES
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return False
    return not _looks_like_legacy_ipv4_literal(hostname)


def _looks_like_legacy_ipv4_literal(hostname: str) -> bool:
    labels = hostname.split(".")
    if not 1 <= len(labels) <= 4:
        return False
    for label in labels:
        if not label:
            return False
        if label.startswith("0x"):
            digits = label[2:]
            if not digits or any(character not in "0123456789abcdef" for character in digits):
                return False
        elif not label.isascii() or not label.isdigit():
            return False
    return True


async def _resolve_addresses(host: str, port: int) -> Sequence[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(record[4][0]) for record in records}))


def _build_queries(intent: SearchIntent) -> list[str]:
    year = intent.release_year or intent.year
    issue_or_volume = intent.volume or intent.issue_number
    variants: list[tuple[str | int | None, ...]] = []
    if is_collection_intent(intent.issue_type):
        title = collection_title_fragment(intent.issue_title)
        if title:
            variants.append((intent.series_title, title, year))
        if issue_or_volume:
            variants.append((intent.series_title, "Vol", issue_or_volume, year))
        if title:
            variants.append((intent.series_title, title))
        elif intent.alternate_titles:
            variants.append((intent.alternate_titles[0], "Vol", issue_or_volume, year))
    else:
        variants.extend(
            (
                (intent.series_title, intent.issue_number, year),
                (intent.series_title, intent.issue_number),
            )
        )
        if intent.alternate_titles:
            variants.append((intent.alternate_titles[0], intent.issue_number, year))

    queries: list[str] = []
    for parts in variants:
        query = " ".join(" ".join(str(part).split()) for part in parts if part is not None).strip()
        query = query[:500].rstrip()
        if query and query not in queries:
            queries.append(query)
        if len(queries) == 3:
            break
    return queries


def _search_url(origin: str, query: str) -> str:
    params = urlencode(
        [
            ("req", query[:500]),
            ("columns[]", "t"),
            ("columns[]", "s"),
            ("objects[]", "f"),
            ("topics[]", "c"),
            ("res", "25"),
            ("filesuns", "all"),
        ]
    )
    return f"{origin}/index.php?{params}"


class LibGenProviderService:
    """Bounded LibGen search, enrichment, failover, and resolution."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory | None = None,
        origin_resolver: SourceResolver | None = None,
    ) -> None:
        self._origin_resolver = origin_resolver
        self._session_factory = session_factory or self._new_session
        self._search_cache: BoundedTTLCache[str, tuple[Candidate, ...]] = BoundedTTLCache(
            max_entries=512,
            ttl_seconds=10 * 60,
            negative_ttl_seconds=2 * 60,
        )
        self._metadata_cache: BoundedTTLCache[
            str,
            FileMetadata | EditionMetadata,
        ] = BoundedTTLCache(
            max_entries=2_048,
            ttl_seconds=60 * 60,
            negative_ttl_seconds=2 * 60,
        )

    async def source_health(self) -> dict[str, ProviderStatus]:
        health: dict[str, ProviderStatus] = {}
        for origin in KNOWN_SOURCE_URLS:
            session = self._session_factory(origin, None)
            try:
                await session.fetch_text(f"{origin}/index.php")
            except BrowserChallengeRequiredError:
                status = ProviderStatus.CHALLENGE_REQUIRED
            except LibGenSourceError as exc:
                status = (
                    ProviderStatus.RATE_LIMITED
                    if exc.code == "source_rate_limited"
                    else ProviderStatus.UNAVAILABLE
                )
            except ProviderResolverError:
                status = ProviderStatus.UNAVAILABLE
            else:
                status = ProviderStatus.HEALTHY
            finally:
                await session.aclose()
            health[urlsplit(origin).hostname or origin] = status
        return health

    async def search(
        self,
        intent: SearchIntent,
        *,
        provider_config: Mapping[str, object],
        limit: int,
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Candidate]:
        started = time.perf_counter()
        try:
            candidates = await self._search(
                intent,
                provider_config=provider_config,
                limit=limit,
                resolver_profile=resolver_profile,
            )
        except asyncio.CancelledError:
            _observe_operation("search", started=started, failure_class="cancelled")
            raise
        except Exception as exc:
            _observe_operation(
                "search",
                started=started,
                failure_class=_failure_class(exc),
            )
            raise
        _observe_operation("search", started=started, result_count=len(candidates))
        return candidates

    async def _search(
        self,
        intent: SearchIntent,
        *,
        provider_config: Mapping[str, object],
        limit: int,
        resolver_profile: ResolverProfile | None,
    ) -> list[Candidate]:
        origins = await self._operation_origins(provider_config)
        last_error: Exception | None = None
        for origin in origins:
            try:
                return await self._search_origin(
                    origin,
                    intent=intent,
                    limit=limit,
                    resolver_profile=resolver_profile,
                )
            except (
                BrowserChallengeRequiredError,
                LibGenLayoutError,
                ProviderResolverError,
            ) as exc:
                last_error = exc
            except LibGenSourceError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        return []

    async def resolve(
        self,
        provider_candidate_id: str,
        *,
        provider_config: Mapping[str, object],
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Artifact]:
        started = time.perf_counter()
        try:
            artifacts = await self._resolve(
                provider_candidate_id,
                provider_config=provider_config,
                resolver_profile=resolver_profile,
            )
        except asyncio.CancelledError:
            _observe_operation("resolve", started=started, failure_class="cancelled")
            raise
        except Exception as exc:
            _observe_operation(
                "resolve",
                started=started,
                failure_class=_failure_class(exc),
            )
            raise
        _observe_operation("resolve", started=started, result_count=len(artifacts))
        return artifacts

    async def _resolve(
        self,
        provider_candidate_id: str,
        *,
        provider_config: Mapping[str, object],
        resolver_profile: ResolverProfile | None,
    ) -> list[Artifact]:
        md5 = _candidate_md5(provider_candidate_id)
        origins = await self._operation_origins(provider_config)
        last_error: Exception | None = None
        for origin in origins:
            session = self._session_factory(origin, resolver_profile)
            try:
                return [await self._resolve_origin(session, origin=origin, md5=md5)]
            except (
                BrowserChallengeRequiredError,
                ProviderResolverError,
            ) as exc:
                last_error = exc
            except LibGenSourceError as exc:
                if not exc.retryable:
                    raise
                last_error = exc
            finally:
                await session.aclose()
        if last_error is not None:
            raise last_error
        return []

    def _new_session(
        self,
        origin: str,
        resolver_profile: ResolverProfile | None,
    ) -> SourceSession:
        return LibGenSourceSession(
            origin,
            resolver_profile=resolver_profile,
            target_resolver=self._origin_resolver,
        )

    async def _operation_origins(self, provider_config: Mapping[str, object]) -> list[str]:
        preferred = await validate_source_origin(
            str(provider_config.get("source_url", DEFAULT_SOURCE_URL)),
            resolver=self._origin_resolver,
        )
        origins = [preferred]
        for candidate in KNOWN_SOURCE_URLS:
            if candidate == preferred:
                continue
            try:
                alternate = await validate_source_origin(
                    candidate,
                    resolver=self._origin_resolver,
                )
            except LibGenSourceOriginError:
                continue
            origins.append(alternate)
            break
        return origins

    async def _search_origin(
        self,
        origin: str,
        *,
        intent: SearchIntent,
        limit: int,
        resolver_profile: ResolverProfile | None,
    ) -> list[Candidate]:
        cache_key = f"{origin}:{limit}:{intent.model_dump_json()}"
        cached = self._search_cache.get(cache_key)
        if cached.hit:
            return list(cached.value or ())

        session = self._session_factory(origin, resolver_profile)
        try:

            async def fetch_metadata(url: str) -> str:
                return await session.fetch_text(url, max_bytes=_MAX_METADATA_BYTES)

            enricher = LibGenMetadataEnricher(
                fetcher=fetch_metadata,
                cache=self._metadata_cache,
            )
            candidates: list[Candidate] = []
            seen: set[str] = set()
            for query in _build_queries(intent):
                html = await session.fetch_text(_search_url(origin, query))
                for discovered in parse_search_html(html, source_origin=origin):
                    if discovered.md5 in seen:
                        continue
                    seen.add(discovered.md5)
                    candidate = await enricher.enrich(discovered)
                    if candidate is not None:
                        candidates.append(candidate)
                    if len(candidates) >= limit:
                        result = tuple(candidates[:limit])
                        self._search_cache.set(cache_key, result)
                        return list(result)
            result = tuple(candidates[:limit])
            self._search_cache.set(cache_key, result or None)
            return list(result)
        finally:
            await session.aclose()

    async def _resolve_origin(
        self,
        session: SourceSession,
        *,
        origin: str,
        md5: str,
    ) -> Artifact:
        file_payload = await session.fetch_text(
            _file_metadata_url(origin, md5),
            max_bytes=_MAX_METADATA_BYTES,
        )
        file_metadata = parse_file_metadata_by_md5(file_payload, expected_md5=md5)
        edition = None
        if file_metadata.edition_id is not None:
            edition_payload = await session.fetch_text(
                _edition_metadata_url(origin, file_metadata.edition_id),
                max_bytes=_MAX_METADATA_BYTES,
            )
            edition = parse_edition_metadata(
                edition_payload,
                expected_edition_id=file_metadata.edition_id,
                expected_file_id=file_metadata.file_id,
            )
        destination = await session.resolve_redirect(f"{origin}/get.php?md5={md5}")
        issue_numbers = [edition.issue_number] if edition and edition.issue_number else []
        return Artifact(
            artifact_id=f"libgen-direct:{md5}",
            coverage=ArtifactCoverage(
                issue_numbers=issue_numbers,
                volume=edition.issue_volume if edition else None,
                description=(edition.series_name or edition.title) if edition else None,
            ),
            route=ArtifactRoute.DIRECT_ARTIFACT,
            format=file_metadata.extension,
            edition=edition.edition_type if edition else None,
            size_bytes=file_metadata.size_bytes,
            mirrors=[
                Mirror(
                    mirror_id=f"libgen:{md5}:direct",
                    host_kind="generic_https",
                    final_url=destination,
                    size_bytes=file_metadata.size_bytes,
                    checksum=f"md5:{md5}",
                )
            ],
        )


def _candidate_md5(provider_candidate_id: str) -> str:
    match = _CANDIDATE_ID.fullmatch(provider_candidate_id)
    if match is None:
        raise ValueError("LibGen candidate identity is malformed.")
    return match.group("md5")


def _observe_operation(
    operation: str,
    *,
    started: float,
    result_count: int | None = None,
    failure_class: str | None = None,
) -> None:
    fields: dict[str, str | int] = {
        "provider": "libgen",
        "operation": operation,
        "outcome": "failure" if failure_class is not None else "success",
        "duration_ms": max(0, round((time.perf_counter() - started) * 1_000)),
    }
    if result_count is not None:
        fields["result_count"] = result_count
    if failure_class is not None:
        fields["failure_class"] = failure_class
    _LOGGER.info("provider_operation", **fields)


def _failure_class(exc: Exception) -> str:
    if isinstance(exc, LibGenSourceOriginError):
        return "provider_configuration_invalid"
    if isinstance(exc, LibGenLayoutError):
        return "source_contract_changed"
    if isinstance(exc, LibGenMetadataError):
        return "candidate_invalid"
    if isinstance(exc, LibGenSourceError):
        return exc.code
    if isinstance(exc, (BrowserChallengeRequiredError, ProviderResolverError)):
        return exc.code
    if isinstance(exc, ValueError):
        return "candidate_invalid"
    return "internal_error"
