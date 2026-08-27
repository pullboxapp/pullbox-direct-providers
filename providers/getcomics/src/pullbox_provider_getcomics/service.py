"""Stateless GetComics provider behavior."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pullbox_provider_contract.comic_parser import normalize_issue
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
            if not is_collection_intent(intent.issue_type) and _has_requested_issue_coverage(
                candidates, intent
            ):
                return _prioritize_requested_issue_coverage(candidates, intent)[:limit]
            if len(candidates) >= limit and is_collection_intent(intent.issue_type):
                return candidates[:limit]

        fallbacks = _standard_issue_fallback_queries(intent)
        if fallbacks and not _has_requested_issue_coverage(candidates, intent):
            fallback_candidates: list[Candidate] = []
            for fallback in fallbacks:
                await self._append_search_candidates(
                    fallback,
                    candidates=fallback_candidates,
                    seen_candidate_ids=seen_candidate_ids,
                    resolver_profile=resolver_profile,
                )
                if _has_requested_issue_coverage(fallback_candidates, intent):
                    break
            # Exact queries can return unrelated releases first. Prioritize only
            # fallback packs that explicitly cover the requested issue; broad
            # fallback noise must not displace a targeted exact-search result.
            covering_fallbacks = [
                candidate
                for candidate in fallback_candidates
                if _candidate_covers_intent(candidate, intent)
            ]
            noncovering_fallbacks = [
                candidate
                for candidate in fallback_candidates
                if not _candidate_covers_intent(candidate, intent)
            ]
            return [*covering_fallbacks, *candidates, *noncovering_fallbacks][:limit]
        return candidates[:limit]

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
    if not is_collection_intent(intent.issue_type):
        return _standard_issue_exact_queries(intent)

    title_fragment = collection_title_fragment(intent.issue_title)
    explicit_title_volume = collection_title_number(intent.issue_title)
    if title_fragment is None:
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


def _standard_issue_exact_queries(intent: SearchIntent) -> list[str]:
    """Return release-aware exact queries without losing legacy year behavior."""
    if not intent.issue_number:
        return [_build_query(intent)]

    base = f"{intent.series_title} {intent.issue_number}"
    queries: list[str] = []
    preferred_year = intent.release_year
    if preferred_year is None and intent.series_year is None:
        preferred_year = intent.year
    if preferred_year is not None:
        queries.append(f"{base} {preferred_year}"[:700])
    queries.append(base[:700])

    compatibility_year = intent.series_year or intent.year
    if compatibility_year is not None:
        queries.append(f"{base} {compatibility_year}"[:700])
    return list(dict.fromkeys(queries))[:3]


def _standard_issue_fallback_queries(intent: SearchIntent) -> list[str]:
    """Return bounded release-year and series-year range-pack fallbacks."""
    if is_collection_intent(intent.issue_type) or not intent.issue_number:
        return []

    years: list[int] = []
    preferred_year = intent.release_year
    if preferred_year is None and intent.series_year is None:
        preferred_year = intent.year
    if preferred_year is not None:
        years.append(preferred_year)
    compatibility_year = intent.series_year or intent.year
    if compatibility_year is not None:
        years.append(compatibility_year)
    unique_years = list(dict.fromkeys(years))
    if not unique_years:
        return [intent.series_title[:700]]
    return [f"{intent.series_title} {year}"[:700] for year in unique_years[:2]]


def _has_requested_issue_coverage(
    candidates: list[Candidate],
    intent: SearchIntent,
) -> bool:
    issue_number = intent.issue_number
    return bool(issue_number) and any(
        _candidate_covers_intent(candidate, intent) for candidate in candidates
    )


def _prioritize_requested_issue_coverage(
    candidates: list[Candidate],
    intent: SearchIntent,
) -> list[Candidate]:
    """Keep a later exact query from being displaced by earlier search noise."""
    covering = [
        candidate for candidate in candidates if _candidate_covers_intent(candidate, intent)
    ]
    noncovering = [
        candidate for candidate in candidates if not _candidate_covers_intent(candidate, intent)
    ]
    return [*covering, *noncovering]


def _candidate_covers_intent(candidate: Candidate, intent: SearchIntent) -> bool:
    """Return whether a candidate covers this issue for the requested series."""
    if intent.issue_number is None:
        return False
    if normalize_issue(intent.issue_number) not in candidate.parsed.issue_numbers:
        return False
    if _normalized_series_title(candidate.parsed.series_title) not in _intent_series_titles(intent):
        return False
    expected_years = {
        year for year in (intent.release_year, intent.series_year, intent.year) if year is not None
    }
    return not (
        expected_years
        and candidate.parsed.year is not None
        and candidate.parsed.year not in expected_years
    )


def _intent_series_titles(intent: SearchIntent) -> set[str]:
    return {
        normalized
        for title in (intent.series_title, intent.normalized_title, *intent.alternate_titles)
        if (normalized := _normalized_series_title(title))
    }


def _normalized_series_title(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


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
