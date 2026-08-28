"""Bounded source-page retrieval with optional browser challenge resolution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx

from pullbox_provider_contract.resolver import (
    OrdinaryHttpResponse,
    ProviderResolverOutcome,
    _validate_source_url,
    detect_browser_challenge,
    resolve_after_challenge,
)

if TYPE_CHECKING:
    from pullbox_provider_contract.models import ResolverProfile
    from pullbox_provider_contract.resolver import ProviderTargetResolver

_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_REDIRECT_URL_LENGTH = 4_000
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_MAX_SOURCE_REDIRECTS = 1

BrowserResolver = Callable[..., Awaitable[ProviderResolverOutcome | None]]


class BrowserChallengeRequiredError(RuntimeError):
    """Signal that ordinary source HTTP reached a recognized browser challenge."""

    code = "browser_challenge_required"

    def __init__(self) -> None:
        super().__init__("Browser challenge handling is required.")


async def fetch_source_html(
    raw_url: str,
    *,
    declared_domains: Sequence[str],
    resolver_profile: ResolverProfile | None = None,
    http_client: httpx.AsyncClient | None = None,
    target_resolver: ProviderTargetResolver | None = None,
    browser_resolver: BrowserResolver = resolve_after_challenge,
) -> str:
    """Fetch one declared source page and invoke a resolver only for a challenge."""
    safe_url = await _validate_source_url(
        raw_url,
        declared_domains,
        resolver=target_resolver,
    )
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "PullboxDirectProvider/0.1"},
    )
    response: httpx.Response | None = None
    try:
        try:
            for redirect_count in range(_MAX_SOURCE_REDIRECTS + 1):
                response = await client.send(
                    client.build_request("GET", safe_url, headers={"Accept": "text/html"}),
                    stream=True,
                    follow_redirects=False,
                )
                if response.status_code not in _REDIRECT_STATUS_CODES:
                    body = await _read_bounded(response)
                    break
                if redirect_count >= _MAX_SOURCE_REDIRECTS:
                    raise RuntimeError("Source redirect limit was exceeded.")
                redirected_url = _same_origin_source_redirect(
                    safe_url,
                    response.headers.get("location"),
                )
                await response.aclose()
                response = None
                safe_url = await _validate_source_url(
                    redirected_url,
                    declared_domains,
                    resolver=target_resolver,
                )
            else:  # pragma: no cover - bounded loop always returns or raises
                raise RuntimeError("Source redirect limit was exceeded.")
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RuntimeError("Source request is temporarily unavailable.") from exc

        if response is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Source request did not return a response.")
        ordinary = OrdinaryHttpResponse(
            status_code=response.status_code,
            headers=_safe_headers(response.headers),
            body=body,
        )
        challenge = detect_browser_challenge(
            ordinary.status_code,
            ordinary.headers,
            ordinary.body,
        )
        if challenge is not None:
            if resolver_profile is None:
                raise BrowserChallengeRequiredError
            outcome = await browser_resolver(
                ordinary,
                source_url=safe_url,
                profile=resolver_profile,
                target_resolver=target_resolver,
            )
            if outcome is None:
                raise RuntimeError("The browser resolver did not return a source page.")
            return outcome.solution.html
        if response.status_code == 429:
            raise RuntimeError("Source request is rate limited.")
        if response.status_code >= 400:
            raise RuntimeError(f"Source request returned HTTP {response.status_code}.")
        return body.decode(response.encoding or "utf-8", errors="replace")
    finally:
        if response is not None:
            await response.aclose()
        if owns_client:
            await client.aclose()


def _same_origin_source_redirect(source_url: str, raw_location: str | None) -> str:
    if not raw_location or len(raw_location) > _MAX_REDIRECT_URL_LENGTH:
        raise RuntimeError("Source redirect destination is invalid.")
    try:
        source = urlsplit(source_url)
        destination_url = urljoin(source_url, raw_location.strip())
        destination = urlsplit(destination_url)
        source_port = source.port or 443
        destination_port = destination.port or 443
    except ValueError as exc:
        raise RuntimeError("Source redirect destination is invalid.") from exc
    if (
        source.scheme != "https"
        or destination.scheme != "https"
        or not source.hostname
        or not destination.hostname
        or source.hostname.casefold().rstrip(".") != destination.hostname.casefold().rstrip(".")
        or source_port != destination_port
        or destination.username
        or destination.password
        or destination.fragment
    ):
        raise RuntimeError("Source redirect destination was rejected.")
    return destination_url


async def resolve_source_redirect(
    raw_url: str,
    *,
    declared_domains: Sequence[str],
    http_client: httpx.AsyncClient | None = None,
    target_resolver: ProviderTargetResolver | None = None,
) -> str:
    """Resolve exactly one declared source redirect without fetching its destination."""
    safe_url = await _validate_source_url(
        raw_url,
        declared_domains,
        resolver=target_resolver,
    )
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "PullboxDirectProvider/0.1"},
    )
    response: httpx.Response | None = None
    try:
        try:
            response = await client.send(
                client.build_request("GET", safe_url, headers={"Accept": "*/*"}),
                stream=True,
                follow_redirects=False,
            )
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise RuntimeError("Source redirect is temporarily unavailable.") from exc
        if response.status_code not in _REDIRECT_STATUS_CODES:
            raise RuntimeError("Source link did not redirect to an artifact host.")
        return _validate_redirect_destination(response.headers.get("location"))
    finally:
        if response is not None:
            await response.aclose()
        if owns_client:
            await client.aclose()


async def _read_bounded(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_SOURCE_BYTES:
            raise RuntimeError("Source response exceeded the supported size limit.")
    return bytes(body)


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.casefold() in {"cf-ray", "content-type", "retry-after", "server"}
    }


def _validate_redirect_destination(raw_url: str | None) -> str:
    if not raw_url or len(raw_url) > _MAX_REDIRECT_URL_LENGTH:
        raise RuntimeError("Source redirect destination is invalid.")
    destination = raw_url.strip()
    try:
        parsed = urlsplit(destination)
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError("Source redirect destination is invalid.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("Source redirect destination is invalid.")
    return destination
