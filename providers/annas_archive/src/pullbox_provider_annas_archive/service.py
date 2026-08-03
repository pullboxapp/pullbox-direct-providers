"""Stateless Anna's Archive discovery and member fast-download behavior."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

import httpx
from pullbox_provider_contract.errors import ProtocolError
from pullbox_provider_contract.models import (
    Artifact,
    ArtifactCoverage,
    ArtifactRoute,
    Candidate,
    Mirror,
    QuotaStatus,
    SearchIntent,
)
from pullbox_provider_contract.search_terms import collection_title_fragment, is_collection_intent
from pullbox_provider_contract.source_http import fetch_source_html

from pullbox_provider_annas_archive.parser import parse_search_html

if TYPE_CHECKING:
    from pullbox_provider_contract.models import ResolverProfile

SUPPORTED_OFFICIAL_DOMAINS = (
    "annas-archive.gl",
    "annas-archive.pk",
    "annas-archive.gd",
)
SUPPORTED_OFFICIAL_URLS = tuple(f"https://{domain}" for domain in SUPPORTED_OFFICIAL_DOMAINS)
DEFAULT_OFFICIAL_URL = "https://annas-archive.gd"
_MD5 = re.compile(r"\A[a-f0-9]{32}\Z")
_MAX_JSON_BYTES = 256 * 1024
_FAST_DOWNLOAD_WINDOW_SECONDS = 64_800
_FAST_DOWNLOAD_DOMAIN_INDICES = (0, 2, 4, 6)

PageFetcher = Callable[..., Awaitable[str]]
FastDownloadFetcher = Callable[..., Awaitable[tuple[int, dict[str, object]]]]


@dataclass(frozen=True, slots=True)
class AnnasArchiveResolveResult:
    """Resolved artifacts plus safe source-account capacity telemetry."""

    artifacts: list[Artifact]
    quota: QuotaStatus | None


class AnnasArchiveProviderService:
    """Conservative official-domain search with member-only resolution."""

    def __init__(
        self,
        *,
        page_fetcher: PageFetcher = fetch_source_html,
        fast_download_fetcher: FastDownloadFetcher | None = None,
    ) -> None:
        self._page_fetcher = page_fetcher
        self._fast_download_fetcher = fast_download_fetcher or _fetch_fast_download

    async def search(
        self,
        intent: SearchIntent,
        *,
        provider_config: Mapping[str, object],
        limit: int,
        resolver_profile: ResolverProfile | None = None,
    ) -> list[Candidate]:
        domain = validate_official_domain(str(provider_config.get("domain", DEFAULT_OFFICIAL_URL)))
        query = _build_query(intent)
        params = urlencode([("q", query), ("ext", "cbz"), ("ext", "cbr"), ("ext", "pdf")])
        html = await self._page_fetcher(
            f"{domain}/search?{params}",
            declared_domains=((urlsplit(domain).hostname or ""),),
            resolver_profile=resolver_profile,
        )
        return parse_search_html(
            html,
            source_domain=urlsplit(domain).hostname or "annas-archive.gd",
        )[:limit]

    async def resolve(
        self,
        provider_candidate_id: str,
        *,
        provider_config: Mapping[str, object],
        source_credentials: Mapping[str, str],
    ) -> AnnasArchiveResolveResult:
        domain = validate_official_domain(str(provider_config.get("domain", DEFAULT_OFFICIAL_URL)))
        md5 = _candidate_md5(provider_candidate_id)
        member_key = source_credentials.get("member_secret_key", "")
        if not member_key:
            raise ProtocolError(
                401,
                "source_authentication_required",
                "Anna's Archive member fast-download access is required.",
            )
        status, payload = await self._fast_download_fetcher(
            domain=domain,
            md5=md5,
            member_secret_key=member_key,
            path_index=0,
            domain_index=0,
        )
        _raise_primary_fast_download_error(status, payload)
        indexed_payloads = [(0, payload)]
        for domain_index in _FAST_DOWNLOAD_DOMAIN_INDICES[1:]:
            try:
                alternate_status, alternate_payload = await self._fast_download_fetcher(
                    domain=domain,
                    md5=md5,
                    member_secret_key=member_key,
                    path_index=0,
                    domain_index=domain_index,
                )
            except ProtocolError:
                continue
            if alternate_status == 429 or _is_quota_error(alternate_payload):
                break
            if alternate_status in {200, 204}:
                indexed_payloads.append((domain_index, alternate_payload))

        mirrors = _secure_fast_download_mirrors(md5, indexed_payloads)
        if not mirrors:
            raise ProtocolError(
                503,
                "source_malformed_response",
                "Anna's Archive returned no safe fast-download URLs.",
            )
        return AnnasArchiveResolveResult(
            artifacts=[
                Artifact(
                    artifact_id=f"anna-fast:{md5}",
                    coverage=ArtifactCoverage(issue_ids=[md5]),
                    route=ArtifactRoute.DIRECT_ARTIFACT,
                    mirrors=mirrors,
                    limitations=["member_fast_download"],
                )
            ],
            quota=_quota_status(payload),
        )

    async def source_available(self) -> bool:
        for domain, url in zip(SUPPORTED_OFFICIAL_DOMAINS, SUPPORTED_OFFICIAL_URLS, strict=True):
            try:
                await self._page_fetcher(
                    url,
                    declared_domains=(domain,),
                )
            except RuntimeError:
                continue
            return True
        return False

    async def source_reachability(self) -> dict[str, bool]:
        """Report each selectable source independently for configuration health."""
        reachability: dict[str, bool] = {}
        for domain, url in zip(SUPPORTED_OFFICIAL_DOMAINS, SUPPORTED_OFFICIAL_URLS, strict=True):
            try:
                await self._page_fetcher(
                    url,
                    declared_domains=(domain,),
                )
            except RuntimeError:
                reachability[domain] = False
            else:
                reachability[domain] = True
        return reachability


def validate_official_domain(raw_domain: str) -> str:
    value = raw_domain.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _domain_error() from exc
    if (
        value not in SUPPORTED_OFFICIAL_URLS
        or parsed.scheme != "https"
        or parsed.hostname not in SUPPORTED_OFFICIAL_DOMAINS
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise _domain_error()
    return value


async def _fetch_fast_download(
    *,
    domain: str,
    md5: str,
    member_secret_key: str,
    path_index: int = 0,
    domain_index: int = 0,
) -> tuple[int, dict[str, object]]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
        follow_redirects=False,
        trust_env=False,
        headers={"Accept": "application/json", "User-Agent": "PullboxDirectProvider/0.1"},
    ) as client:
        response: httpx.Response | None = None
        try:
            response = await client.send(
                client.build_request(
                    "GET",
                    f"{domain}/dyn/api/fast_download.json",
                    params={
                        "md5": md5,
                        "key": member_secret_key,
                        "path_index": path_index,
                        "domain_index": domain_index,
                    },
                ),
                stream=True,
                follow_redirects=False,
            )
            content = await _read_bounded_json(response)
            try:
                decoded = httpx.Response(200, content=content).json()
            except ValueError as exc:
                raise ProtocolError(
                    503,
                    "source_malformed_response",
                    "Anna's Archive returned invalid JSON.",
                ) from exc
            if not isinstance(decoded, dict):
                raise ProtocolError(
                    503,
                    "source_malformed_response",
                    "Anna's Archive returned invalid JSON.",
                )
            return response.status_code, {str(key): value for key, value in decoded.items()}
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            raise ProtocolError(
                503,
                "source_unavailable",
                "Anna's Archive fast-download request failed.",
            ) from exc
        finally:
            if response is not None:
                await response.aclose()


async def _read_bounded_json(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_JSON_BYTES:
            raise ProtocolError(
                503,
                "source_malformed_response",
                "Anna's Archive response exceeded the supported size limit.",
            )
    return bytes(body)


def _candidate_md5(candidate_id: str) -> str:
    value = candidate_id.removeprefix("anna:") if candidate_id.startswith("anna:") else ""
    if not _MD5.fullmatch(value):
        raise ProtocolError(404, "candidate_not_found", "Provider candidate was not found.")
    return value


def _build_query(intent: SearchIntent) -> str:
    if is_collection_intent(intent.issue_type):
        title_fragment = collection_title_fragment(intent.issue_title)
        if title_fragment:
            return f"{intent.series_title} {title_fragment}"[:700]
        volume = intent.volume or intent.issue_number
        if volume:
            parts = [intent.series_title, "Vol", volume]
            if intent.release_year or intent.year:
                parts.append(str(intent.release_year or intent.year))
            return " ".join(parts)[:700]
    parts = [intent.series_title]
    if intent.issue_number:
        parts.append(intent.issue_number)
    if intent.year:
        parts.append(str(intent.year))
    return " ".join(parts)[:700]


def _safe_download_url(raw_url: str) -> bool:
    return _safe_download_origin(raw_url) is not None


def _safe_download_origin(raw_url: str) -> tuple[str, int] | None:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return parsed.hostname.casefold().rstrip("."), port or 443


def _raise_primary_fast_download_error(status: int, payload: Mapping[str, object]) -> None:
    if status in {401, 403}:
        raise ProtocolError(
            401,
            "source_authentication_required",
            "Anna's Archive rejected the member fast-download credential.",
        )
    if status == 429 or _is_quota_error(payload):
        raise ProtocolError(
            429,
            "source_quota_limited",
            "Anna's Archive member fast-download quota is unavailable.",
            retry_after_seconds=_FAST_DOWNLOAD_WINDOW_SECONDS,
        )
    if _is_candidate_unavailable(payload):
        raise ProtocolError(
            404,
            "candidate_not_found",
            "Anna's Archive result has no member fast-download route.",
        )
    if status not in {200, 204}:
        raise ProtocolError(503, "source_unavailable", "Anna's Archive resolve failed.")


def _secure_fast_download_mirrors(
    md5: str,
    indexed_payloads: Sequence[tuple[int, Mapping[str, object]]],
) -> list[Mirror]:
    mirrors: list[Mirror] = []
    seen_origins: set[tuple[str, int]] = set()
    for domain_index, payload in indexed_payloads:
        raw_url = payload.get("download_url")
        if not isinstance(raw_url, str):
            continue
        origin = _safe_download_origin(raw_url)
        if origin is None or origin in seen_origins:
            continue
        seen_origins.add(origin)
        suffix = "0" if domain_index == 0 else f"domain-{domain_index}"
        mirrors.append(
            Mirror(
                mirror_id=f"anna-fast:{md5}:{suffix}",
                host_kind="generic_https",
                final_url=raw_url,
                checksum=f"md5:{md5}",
            )
        )
    return mirrors


def _is_quota_error(payload: Mapping[str, object]) -> bool:
    error = payload.get("error")
    return isinstance(error, str) and any(
        marker in error.casefold() for marker in ("quota", "downloads remaining", "limit")
    )


def _is_candidate_unavailable(payload: Mapping[str, object]) -> bool:
    error = payload.get("error")
    return isinstance(error, str) and "invalid domain_index or path_index" in error.casefold()


def _quota_status(payload: Mapping[str, object]) -> QuotaStatus | None:
    raw = payload.get("account_fast_download_info")
    if not isinstance(raw, Mapping):
        return None
    remaining = _bounded_nonnegative_int(raw.get("downloads_left"))
    limit = next(
        (
            parsed
            for key in ("downloads_per_day", "download_limit", "daily_limit")
            if (parsed := _bounded_nonnegative_int(raw.get(key))) is not None
        ),
        None,
    )
    if remaining is None and limit is None:
        return None
    return QuotaStatus(
        remaining=remaining,
        limit=limit,
        # Anna documents a rolling window rather than a midnight reset.
        window_seconds=_FAST_DOWNLOAD_WINDOW_SECONDS,
    )


def _bounded_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 1_000_000 else None


def _domain_error() -> ProtocolError:
    return ProtocolError(
        422,
        "invalid_source_domain",
        "Anna's Archive must use the exact supported official domain.",
    )
