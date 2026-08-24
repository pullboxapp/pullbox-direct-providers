"""Stateless LibGen discovery and resolution behavior."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit, urlunsplit

from pullbox_provider_contract.models import Artifact, Candidate, SearchIntent
from pullbox_provider_contract.search_terms import collection_title_fragment, is_collection_intent

if TYPE_CHECKING:
    from pullbox_provider_contract.models import ResolverProfile

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

SourceResolver = Callable[[str, int], Awaitable[Sequence[str]]]


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
    """Bounded LibGen provider; source behavior is added in later LG-2 slices."""

    async def source_available(self) -> bool:
        return False

    async def search(
        self,
        _intent: SearchIntent,
        *,
        provider_config: Mapping[str, object],
        limit: int,
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Candidate]:
        del provider_config, limit, resolver_profile
        raise RuntimeError("LibGen discovery is not implemented.")

    async def resolve(
        self,
        _provider_candidate_id: str,
        *,
        provider_config: Mapping[str, object],
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Artifact]:
        del provider_config, resolver_profile
        raise RuntimeError("LibGen resolution is not implemented.")
