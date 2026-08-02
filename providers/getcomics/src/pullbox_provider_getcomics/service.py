"""Stateless GetComics provider behavior."""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pullbox_provider_contract.models import Artifact, Candidate, SearchIntent
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
        for query in _build_queries(intent):
            url = f"{_BASE_URL}/?{urlencode({'s': query})}"
            html = await self._page_fetcher(
                url,
                declared_domains=(_DOMAIN,),
                resolver_profile=resolver_profile,
            )
            candidates = parse_search_html(html, source_domain=_DOMAIN)
            if candidates:
                return candidates[:limit]
        return []

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
    queries = [_build_query(intent)]
    if intent.volume is not None and intent.issue_number:
        fallback = " ".join(
            part
            for part in (intent.series_title, str(intent.year) if intent.year else None)
            if part
        )[:700]
        if fallback not in queries:
            queries.append(fallback)
    return queries


def _candidate_url(candidate_id: str) -> str:
    prefix = "getcomics:"
    if not candidate_id.startswith(prefix):
        raise RuntimeError("GetComics candidate identifier is invalid.")
    encoded = candidate_id.removeprefix(prefix)
    try:
        padding = "=" * (-len(encoded) % 4)
        path = base64.urlsafe_b64decode(f"{encoded}{padding}").decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise RuntimeError("GetComics candidate identifier is invalid.") from exc
    if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
        raise RuntimeError("GetComics candidate identifier is invalid.")
    return f"{_BASE_URL}{path}"
