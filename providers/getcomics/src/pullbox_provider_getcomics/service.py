"""Stateless GetComics provider behavior."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from pullbox_provider_contract.models import Artifact, Candidate, SearchIntent
from pullbox_provider_contract.source_http import fetch_source_html

from pullbox_provider_getcomics.parser import parse_release_html, parse_search_html

if TYPE_CHECKING:
    from pullbox_provider_contract.models import ResolverProfile

_DOMAIN = "getcomics.org"
_BASE_URL = f"https://{_DOMAIN}"

PageFetcher = Callable[..., Awaitable[str]]


class GetComicsProviderService:
    """Search and resolve GetComics pages without durable provider state."""

    def __init__(self, *, page_fetcher: PageFetcher = fetch_source_html) -> None:
        self._page_fetcher = page_fetcher

    async def search(
        self,
        intent: SearchIntent,
        *,
        limit: int,
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Candidate]:
        query = _build_query(intent)
        url = f"{_BASE_URL}/?{urlencode({'s': query})}"
        html = await self._page_fetcher(
            url,
            declared_domains=(_DOMAIN,),
            resolver_profile=resolver_profile,
        )
        return parse_search_html(html, source_domain=_DOMAIN)[:limit]

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
        return parse_release_html(html, source_url=source_url)

    async def source_available(self) -> bool:
        try:
            await self._page_fetcher(_BASE_URL, declared_domains=(_DOMAIN,))
        except RuntimeError:
            return False
        return True


def _build_query(intent: SearchIntent) -> str:
    parts = [intent.series_title]
    if intent.issue_number:
        parts.append(intent.issue_number)
    if intent.year:
        parts.append(str(intent.year))
    return " ".join(parts)[:700]


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
