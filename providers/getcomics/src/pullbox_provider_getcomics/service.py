"""Stateless GetComics provider behavior."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pullbox_provider_contract.errors import ProtocolError
from pullbox_provider_contract.models import Artifact, Candidate, SearchIntent
from pullbox_provider_contract.search_terms import (
    collection_title_fragment,
    collection_title_number,
    is_collection_intent,
)
from pullbox_provider_contract.source_http import fetch_source_html, resolve_source_redirect

from pullbox_provider_getcomics.parser import (
    extract_source_redirect_links,
    parse_release_html,
    parse_search_html,
)

if TYPE_CHECKING:
    from pullbox_provider_contract.models import ResolverProfile

_DOMAIN = "getcomics.org"
_BASE_URL = f"https://{_DOMAIN}"
_MAX_REDIRECT_CONCURRENCY = 4

PageFetcher = Callable[..., Awaitable[str]]
RedirectResolver = Callable[[str], Awaitable[str]]


class GetComicsProviderService:
    """Search and resolve GetComics pages without durable provider state."""

    def __init__(
        self,
        *,
        page_fetcher: PageFetcher = fetch_source_html,
        redirect_resolver: RedirectResolver | None = None,
    ) -> None:
        self._page_fetcher = page_fetcher
        self._redirect_resolver = redirect_resolver or _resolve_getcomics_redirect

    async def search(
        self,
        intent: SearchIntent,
        *,
        limit: int,
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Candidate]:
        candidates: list[Candidate] = []
        seen_candidate_ids: set[str] = set()
        for query in _build_queries(intent):
            await self._append_search_candidates(
                query,
                candidates=candidates,
                seen_candidate_ids=seen_candidate_ids,
                resolver_profile=resolver_profile,
            )
            if len(candidates) >= limit:
                return candidates[:limit]

        fallback = _standard_issue_fallback_query(intent)
        if (
            fallback is not None
            and len(candidates) < limit
            and not _has_requested_issue_coverage(candidates, intent.issue_number)
        ):
            await self._append_search_candidates(
                fallback,
                candidates=candidates,
                seen_candidate_ids=seen_candidate_ids,
                resolver_profile=resolver_profile,
            )
        return candidates

    async def _append_search_candidates(
        self,
        query: str,
        *,
        candidates: list[Candidate],
        seen_candidate_ids: set[str],
        resolver_profile: ResolverProfile | None,
    ) -> None:
        url = f"{_BASE_URL}/?{urlencode({'s': query})}"
        html = await self._page_fetcher(
            url,
            declared_domains=(_DOMAIN,),
            resolver_profile=resolver_profile,
        )
        for candidate in parse_search_html(html, source_domain=_DOMAIN):
            if candidate.provider_candidate_id in seen_candidate_ids:
                continue
            seen_candidate_ids.add(candidate.provider_candidate_id)
            candidates.append(candidate)

    async def resolve(
        self,
        provider_candidate_id: str,
        *,
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Artifact]:
        source_url = _candidate_url(provider_candidate_id)
        html = await self._page_fetcher(
            source_url,
            declared_domains=(_DOMAIN,),
            resolver_profile=resolver_profile,
        )
        redirect_urls = extract_source_redirect_links(html, source_domain=_DOMAIN)
        redirect_limiter = asyncio.Semaphore(_MAX_REDIRECT_CONCURRENCY)
        resolved_links = dict(
            await asyncio.gather(
                *(self._resolve_link(url, redirect_limiter) for url in redirect_urls)
            )
        )
        return parse_release_html(
            html,
            source_url=source_url,
            resolved_links=resolved_links,
            require_resolved_source_links=True,
        )

    async def source_available(self) -> bool:
        try:
            await self._page_fetcher(_BASE_URL, declared_domains=(_DOMAIN,))
        except RuntimeError:
            return False
        return True

    async def _resolve_link(
        self,
        url: str,
        limiter: asyncio.Semaphore,
    ) -> tuple[str, str | None]:
        async with limiter:
            try:
                return url, await self._redirect_resolver(url)
            except RuntimeError:
                return url, None


async def _resolve_getcomics_redirect(url: str) -> str:
    return await resolve_source_redirect(url, declared_domains=(_DOMAIN,))


def _build_query(intent: SearchIntent) -> str:
    parts = [intent.series_title]
    if intent.issue_number:
        parts.append(intent.issue_number)
    if intent.year:
        parts.append(str(intent.year))
    return " ".join(parts)[:700]


def _build_queries(intent: SearchIntent) -> list[str]:
    title_fragment = collection_title_fragment(intent.issue_title)
    explicit_title_volume = collection_title_number(intent.issue_title)
    if not is_collection_intent(intent.issue_type) or title_fragment is None:
        queries = [_build_query(intent)]
    else:
        queries = [f"{intent.series_title} {title_fragment}"[:700]]
        volume = intent.volume or intent.issue_number
        if volume:
            queries.append(f"{intent.series_title} Vol {volume}"[:700])
            if intent.issue_type == "volume":
                queries.append(f"{intent.series_title} Volume {volume}"[:700])

    if is_collection_intent(intent.issue_type) and explicit_title_volume is not None:
        volume = explicit_title_volume
        for label in ("Vol", "Volume"):
            query = f"{intent.series_title} {label} {volume}"[:700]
            if query not in queries:
                queries.append(query)

    if is_collection_intent(intent.issue_type) and intent.volume is not None:
        fallback = " ".join(
            part
            for part in (
                intent.series_title,
                (
                    str(intent.release_year or intent.year)
                    if (intent.release_year or intent.year)
                    else None
                ),
            )
            if part
        )[:700]
        if fallback not in queries:
            queries.append(fallback)
        if intent.series_year and intent.series_year != (intent.release_year or intent.year):
            series_fallback = f"{intent.series_title} {intent.series_year}"[:700]
            if series_fallback not in queries:
                queries.append(series_fallback)
    return queries[:5]


def _standard_issue_fallback_query(intent: SearchIntent) -> str | None:
    """Return one bounded range-pack fallback after an exact issue search misses."""
    if is_collection_intent(intent.issue_type) or not intent.issue_number:
        return None
    parts = [intent.series_title]
    if intent.year:
        parts.append(str(intent.year))
    return " ".join(parts)[:700]


def _has_requested_issue_coverage(
    candidates: list[Candidate],
    issue_number: str | None,
) -> bool:
    return bool(issue_number) and any(
        issue_number in candidate.parsed.issue_numbers for candidate in candidates
    )


def _candidate_url(candidate_id: str) -> str:
    prefix = "getcomics:"
    if not candidate_id.startswith(prefix):
        raise _candidate_not_found()
    encoded = candidate_id.removeprefix(prefix)
    try:
        padding = "=" * (-len(encoded) % 4)
        path = base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise _candidate_not_found() from exc
    if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
        raise _candidate_not_found()
    return f"{_BASE_URL}{path}"


def _candidate_not_found() -> ProtocolError:
    return ProtocolError(404, "candidate_not_found", "Provider candidate was not found.")
