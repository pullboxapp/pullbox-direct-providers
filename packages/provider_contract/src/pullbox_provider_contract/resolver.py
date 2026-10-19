"""Ordinary-HTTP-first helper for a bounded FlareSolverr-compatible profile."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pullbox_provider_contract.api import remaining_operation_seconds
from pullbox_provider_contract.models import ResolverProfile

_CHALLENGE_BODY_LIMIT = 64 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SOURCE_FAILURE_THRESHOLD = 2
_SOURCE_COOLDOWN_SECONDS = 300.0
_MAX_SOURCE_BACKOFFS = 128
_FORBIDDEN_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "host",
        "proxy-authenticate",
        "proxy-authorization",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

ProviderTargetResolver = Callable[[str, int], Awaitable[Sequence[str]]]


class BrowserChallengeKind(StrEnum):
    CLOUDFLARE = "cloudflare"
    DDOS_GUARD = "ddos_guard"
    BROWSER_CHALLENGE = "browser_challenge"


class ProviderResolverError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        reason: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds

    @property
    def public_message(self) -> str | None:
        """Only curated categories can cross the provider API; never upstream prose."""
        challenge = (
            self.reason
            if self.reason
            in {"DDoS-Guard", "Cloudflare", "Imperva", "Akamai", "Anubis", "DataDome", "AWS WAF"}
            else "the source's browser challenge"
        )
        if self.code == "resolver_source_blocked":
            return (
                f"TRAWL reached the source but could not clear {challenge}. "
                "Other resolvers may still work."
            )
        if self.code == "resolver_source_backoff":
            seconds = min(300, max(1, self.retry_after_seconds or 300))
            failure = (
                "exhausting its page-resolution deadline"
                if self.reason == "browser_deadline"
                else f"failing {challenge}"
            )
            return (
                f"This resolver is temporarily paused for this source after {failure}. "
                f"Retry in {seconds} seconds or use another resolver."
            )
        if self.code == "resolver_timed_out":
            return (
                "The resolver exhausted its page-resolution deadline. "
                "Other resolvers may still work."
            )
        return None


@dataclass(frozen=True, slots=True)
class OrdinaryHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: str | bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderResolverCookie:
    name: str
    value: str = field(repr=False)
    domain: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResolverSolution:
    final_url: str = field(repr=False)
    status_code: int
    html: str = field(repr=False)
    cookies: tuple[ProviderResolverCookie, ...] = field(repr=False)
    user_agent: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderResolverOutcome:
    challenge: BrowserChallengeKind
    solution: ProviderResolverSolution


@dataclass(slots=True)
class _SourceBackoff:
    failures: int = 0
    last_failure_at: float = 0
    retry_at: float | None = None
    probing: bool = False
    message: str = "The browser challenge was not solved."
    reason: str | None = None


class ProviderResolverRuntime:
    """Shared concurrency, endpoint circuit, and bounded source-specific backoff."""

    def __init__(
        self,
        *,
        max_concurrency: int,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 4:
            raise ValueError("Resolver concurrency must be between 1 and 4.")
        if failure_threshold < 1 or cooldown_seconds <= 0:
            raise ValueError("Resolver circuit settings must be positive.")
        self._max_concurrency = max_concurrency
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._available = asyncio.Condition(self._lock)
        self._waiting = 0
        self._active = 0
        self._failures = 0
        self._state = "closed"
        self._opened_at: float | None = None
        self._sources: OrderedDict[str, _SourceBackoff] = OrderedDict()

    @property
    def state(self) -> str:
        return self._state

    @asynccontextmanager
    async def slot(
        self, *, wait_seconds: float = 0, source_origin: str | None = None
    ) -> AsyncIterator[None]:
        queued = False
        source_probe: _SourceBackoff | None = None
        async with self._available:
            try:
                async with asyncio.timeout(wait_seconds if wait_seconds > 0 else None):
                    while True:
                        source = self._sources.get(source_origin) if source_origin else None
                        if source is not None and source.retry_at is not None:
                            if source.probing or self._clock() < source.retry_at:
                                seconds = max(1, math.ceil(source.retry_at - self._clock()))
                                raise ProviderResolverError(
                                    "resolver_source_backoff",
                                    f"{source.message} This resolver is temporarily paused for "
                                    f"this source; retry in {seconds} seconds "
                                    "or use another resolver.",
                                    retryable=True,
                                    reason=source.reason,
                                    retry_after_seconds=seconds,
                                )
                        if self._state == "open":
                            opened_at = self._opened_at
                            if (
                                opened_at is None
                                or self._clock() - opened_at < self._cooldown_seconds
                            ):
                                raise ProviderResolverError(
                                    "resolver_circuit_open",
                                    "Resolver is temporarily unavailable "
                                    "while its circuit recovers.",
                                    retryable=True,
                                )
                            self._state = "half_open"
                        if self._active < self._max_concurrency and not (
                            self._state == "half_open" and self._active > 0
                        ):
                            if source is not None and source.retry_at is not None:
                                source.probing = True
                                source_probe = source
                            break
                        if wait_seconds <= 0 or (
                            not queued and self._waiting >= self._max_concurrency * 4
                        ):
                            raise TimeoutError
                        if not queued:
                            self._waiting += 1
                            queued = True
                        await self._available.wait()
            except TimeoutError as exc:
                raise ProviderResolverError(
                    "resolver_busy",
                    "Resolver concurrency is currently full; retry later.",
                    retryable=True,
                ) from exc
            finally:
                if queued:
                    self._waiting -= 1
            self._active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1
                if source_probe is not None:
                    source_probe.probing = False
                self._available.notify_all()

    def _reset_endpoint_failure(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._state = "closed"

    async def record_success(self, source_origin: str | None = None) -> None:
        async with self._lock:
            self._reset_endpoint_failure()
            if source_origin:
                self._sources.pop(source_origin, None)
            self._available.notify_all()

    async def record_source_failure(self, source_origin: str, error: ProviderResolverError) -> None:
        async with self._lock:
            # A resolver that reports an unsolved challenge is reachable, not down.
            self._reset_endpoint_failure()
            source = self._sources.get(source_origin)
            if source is None:
                if len(self._sources) >= _MAX_SOURCE_BACKOFFS:
                    oldest = next(key for key, value in self._sources.items() if not value.probing)
                    del self._sources[oldest]
                source = self._sources[source_origin] = _SourceBackoff()
            now = self._clock()
            if source.retry_at is None and now - source.last_failure_at >= _SOURCE_COOLDOWN_SECONDS:
                source.failures = 0
            source.failures += 1
            source.last_failure_at = now
            source.message = error.public_message or "The browser challenge was not solved."
            source.reason = error.reason
            if source.failures >= _SOURCE_FAILURE_THRESHOLD:
                source.retry_at = now + _SOURCE_COOLDOWN_SECONDS
            self._sources.move_to_end(source_origin)
            self._available.notify_all()

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = self._clock()
                self._state = "open"
                self._available.notify_all()


_RUNTIMES: dict[tuple[str, int, str], ProviderResolverRuntime] = {}


class _CookieModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=16_384)
    domain: str | None = Field(default=None, max_length=500)
    path: str | None = Field(default=None, max_length=2_000)


class _SolutionModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1, max_length=4_000)
    status: int = Field(ge=100, le=599)
    response: str = Field(max_length=_MAX_RESPONSE_BYTES)
    cookies: list[_CookieModel] = Field(default_factory=list, max_length=200)
    user_agent: str | None = Field(default=None, alias="userAgent", max_length=2_000)


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str
    solution: _SolutionModel


class _TrawlResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str = Field(min_length=1, max_length=4_000)
    status_code: int = Field(alias="statusCode", ge=100, le=599)
    html: str = Field(max_length=_MAX_RESPONSE_BYTES)
    cookies: list[_CookieModel] = Field(default_factory=list, max_length=200)
    user_agent: str | None = Field(default=None, alias="userAgent", max_length=2_000)


class _TrawlFailureTiming(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tier: int = Field(ge=1, le=4)
    status: Literal["success", "blocked", "needs-js", "timeout", "error", "skipped"]
    reason: str = Field(default="", max_length=2_000)


class _TrawlFailureModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timings: list[_TrawlFailureTiming] = Field(min_length=1, max_length=4)


def _trawl_failure(content: bytes) -> ProviderResolverError | None:
    try:
        failure = _TrawlFailureModel.model_validate_json(content).timings[-1]
    except ValidationError:
        return None
    if failure.status == "timeout":
        return ProviderResolverError(
            "resolver_timed_out",
            "TRAWL exhausted its page-resolution deadline.",
            retryable=True,
            reason="browser_deadline",
        )
    if failure.status != "blocked":
        return None
    # Translate known markers, never echo arbitrary upstream text or claim an IP block.
    challenge = "the source's browser challenge"
    for marker, label in (
        ("ddos-guard", "DDoS-Guard"),
        ("cloudflare", "Cloudflare"),
        ("imperva", "Imperva"),
        ("akamai", "Akamai"),
        ("anubis", "Anubis"),
        ("datadome", "DataDome"),
        ("aws-waf", "AWS WAF"),
    ):
        if failure.reason.casefold().startswith(marker) or (
            marker == "ddos-guard"
            and failure.reason.startswith(
                "datacenter-ip-blocked (DDoS-Guard clearance cookie obtained"
            )
        ):
            challenge = label
            break
    return ProviderResolverError(
        "resolver_source_blocked",
        f"TRAWL reached the source but could not clear {challenge}. "
        "Other resolvers may still work.",
        retryable=True,
        reason=challenge,
    )


def detect_browser_challenge(
    status_code: int,
    headers: Mapping[str, str],
    body: str | bytes,
) -> BrowserChallengeKind | None:
    """Recognize a narrow challenge signature without parsing arbitrary HTML."""
    if status_code not in {403, 429, 503}:
        return None
    normalized_headers = {
        str(key).casefold(): str(value).casefold() for key, value in headers.items()
    }
    if isinstance(body, bytes):
        sample = body[:_CHALLENGE_BODY_LIMIT].decode("utf-8", errors="ignore").casefold()
    else:
        sample = body[:_CHALLENGE_BODY_LIMIT].casefold()

    cloudflare_marker = any(
        marker in sample
        for marker in (
            "just a moment",
            "cf-chl-",
            "/cdn-cgi/challenge-platform/",
            "cloudflare ray id",
        )
    )
    if cloudflare_marker and (
        "cf-ray" in normalized_headers or "cloudflare" in normalized_headers.get("server", "")
    ):
        return BrowserChallengeKind.CLOUDFLARE

    if "ddos-guard" in normalized_headers.get("server", "") and any(
        marker in sample for marker in ("checking your browser", "ddos-guard")
    ):
        return BrowserChallengeKind.DDOS_GUARD

    if "challenge-form" in sample and any(
        marker in sample for marker in ("enable javascript", "enable cookies", "checking")
    ):
        return BrowserChallengeKind.BROWSER_CHALLENGE
    return None


class _ChallengePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.title: list[str] = []
        self.challenge_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self.in_title = True
        if tag == "form" and (dict(attrs).get("id") or "").casefold() == "challenge-form":
            self.challenge_form = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)


def _solution_still_has_challenge(html: str) -> bool:
    # Solvers can return HTTP 200 for an interstitial. Require paired page markers,
    # not incidental mentions of a challenge vendor in ordinary source content.
    sample = html[:_CHALLENGE_BODY_LIMIT].casefold()
    page = _ChallengePageParser()
    page.feed(sample)
    title = " ".join("".join(page.title).split())
    return (
        (title == "ddos-guard" and "checking your browser" in sample)
        or (
            title.startswith("just a moment")
            and any(marker in sample for marker in ("cf-chl-", "/cdn-cgi/challenge-platform/"))
        )
        or (
            page.challenge_form
            and any(marker in sample for marker in ("enable javascript", "enable cookies"))
        )
    )


async def resolve_after_challenge(
    response: OrdinaryHttpResponse,
    *,
    source_url: str,
    profile: ResolverProfile,
    recognized_challenge: BrowserChallengeKind | None = None,
    http_client: httpx.AsyncClient | None = None,
    runtime: ProviderResolverRuntime | None = None,
    target_resolver: ProviderTargetResolver | None = None,
) -> ProviderResolverOutcome | None:
    """Call standard ``/v1`` only when the ordinary response is a known challenge."""
    challenge = recognized_challenge or detect_browser_challenge(
        response.status_code,
        response.headers,
        response.body,
    )
    if challenge is None:
        return None
    target_url = await _validate_source_url(
        source_url,
        profile.declared_domains,
        resolver=target_resolver,
    )
    endpoint = _resolver_operation_url(profile)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **_validated_auth_headers(profile.authentication_headers),
    }
    payload: dict[str, str | int | bool] = (
        {
            "url": target_url,
            "maxTimeout": round(profile.timeout_seconds * 1000),
            "skipHttp": True,
            "maxTier": 3,
        }
        if profile.mode == "trawl_scrape"
        else {
            "cmd": "request.get",
            "url": target_url,
            "maxTimeout": round(profile.timeout_seconds * 1000),
        }
    )
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=5.0,
            read=profile.timeout_seconds,
            write=10.0,
            pool=5.0,
        ),
        follow_redirects=False,
        trust_env=False,
    )
    active_runtime = runtime or _runtime_for_profile(profile)
    expires = time.monotonic() + remaining_operation_seconds(profile.timeout_seconds)
    parsed = urlsplit(target_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    source_origin = f"{parsed.scheme}://{parsed.hostname}:{port}"
    try:
        async with active_runtime.slot(
            wait_seconds=min(10.0, max(0.0, expires - time.monotonic()) / 3),
            source_origin=source_origin,
        ):
            seconds = remaining_operation_seconds(expires - time.monotonic())
            if seconds <= 0:
                raise ProviderResolverError(
                    "resolver_timed_out", "Resolver request deadline expired.", retryable=True
                )
            # Browser startup and response serialization must fit inside the HTTP budget.
            browser_seconds = seconds - min(2.0, seconds / 5)
            # Byparr interprets values below 1000 as seconds, not milliseconds.
            if browser_seconds < 1:
                raise ProviderResolverError(
                    "resolver_timed_out",
                    "Too little time remains to start a browser.",
                    retryable=True,
                )
            payload["maxTimeout"] = int(browser_seconds * 1000)
            bounded_profile = profile.model_copy(update={"timeout_seconds": seconds})
            try:
                outcome = await _perform_resolution(
                    client=client,
                    endpoint=endpoint,
                    headers=headers,
                    payload=payload,
                    profile=bounded_profile,
                    challenge=challenge,
                    target_resolver=target_resolver,
                )
            except ProviderResolverError as exc:
                if exc.code in {"resolver_source_blocked", "resolver_challenge_failed"} or (
                    exc.code == "resolver_timed_out" and exc.reason == "browser_deadline"
                ):
                    await active_runtime.record_source_failure(source_origin, exc)
                elif exc.code not in {"resolver_redirect_rejected", "resolver_target_rejected"}:
                    await active_runtime.record_failure()
                raise
            await active_runtime.record_success(source_origin)
            return outcome
    finally:
        if owns_client:
            await client.aclose()


async def _perform_resolution(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, str | int | bool],
    profile: ResolverProfile,
    challenge: BrowserChallengeKind,
    target_resolver: ProviderTargetResolver | None,
) -> ProviderResolverOutcome:
    try:
        async with asyncio.timeout(profile.timeout_seconds):
            async with client.stream("POST", endpoint, headers=headers, json=payload) as result:
                if 300 <= result.status_code < 400:
                    raise ProviderResolverError(
                        "resolver_endpoint_redirect_rejected",
                        "Resolver endpoint redirects are not permitted.",
                    )
                content = await _read_bounded_response(result)
    except asyncio.CancelledError:
        raise
    except TimeoutError as exc:
        raise ProviderResolverError(
            "resolver_timed_out", "Resolver request timed out.", retryable=True
        ) from exc
    except httpx.TimeoutException as exc:
        raise ProviderResolverError(
            "resolver_timed_out", "Resolver request timed out.", retryable=True
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderResolverError(
            "resolver_unavailable", "Resolver request failed.", retryable=True
        ) from exc

    if result.status_code in {401, 403}:
        raise ProviderResolverError(
            "resolver_authentication_failed", "Resolver rejected its authentication."
        )
    if result.status_code == 429:
        raise ProviderResolverError(
            "resolver_rate_limited", "Resolver is rate limited.", retryable=True
        )
    if result.status_code == 408:
        raise ProviderResolverError(
            "resolver_timed_out",
            "Resolver exhausted its page-resolution deadline.",
            retryable=True,
            reason="browser_deadline",
        )
    if result.status_code == 500 and profile.mode == "trawl_scrape":
        failure = _trawl_failure(content)
        if failure is not None:
            raise failure
    if result.status_code >= 400:
        raise ProviderResolverError(
            "resolver_unavailable",
            f"Resolver returned HTTP {result.status_code}.",
            retryable=result.status_code >= 500,
        )
    try:
        raw_decoded = json.loads(content)
        if profile.mode == "trawl_scrape":
            trawl = _TrawlResponseModel.model_validate(raw_decoded)
            solution_url = trawl.url
            solution_status = trawl.status_code
            solution_html = trawl.html
            solution_cookies = trawl.cookies
            solution_user_agent = trawl.user_agent
        else:
            decoded = _ResponseModel.model_validate(raw_decoded)
            if decoded.status != "ok":
                raise ProviderResolverError(
                    "resolver_challenge_failed",
                    "Resolver did not solve the browser challenge.",
                )
            solution_url = decoded.solution.url
            solution_status = decoded.solution.status
            solution_html = decoded.solution.response
            solution_cookies = decoded.solution.cookies
            solution_user_agent = decoded.solution.user_agent
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        raise ProviderResolverError(
            "resolver_malformed_response", "Resolver returned an invalid response."
        ) from exc
    try:
        final_url = await _validate_source_url(
            solution_url,
            profile.declared_domains,
            resolver=target_resolver,
        )
    except ProviderResolverError as exc:
        raise ProviderResolverError(
            "resolver_redirect_rejected",
            "Resolver returned a URL outside the declared source domains.",
        ) from exc
    if _solution_still_has_challenge(solution_html):
        raise ProviderResolverError(
            "resolver_challenge_failed",
            "Resolver returned an unsolved browser challenge.",
        )
    return ProviderResolverOutcome(
        challenge=challenge,
        solution=ProviderResolverSolution(
            final_url=final_url,
            status_code=solution_status,
            html=solution_html,
            cookies=tuple(
                ProviderResolverCookie(
                    name=cookie.name,
                    value=cookie.value,
                    domain=cookie.domain,
                    path=cookie.path,
                )
                for cookie in solution_cookies
            ),
            user_agent=solution_user_agent,
        ),
    )


def _runtime_for_profile(profile: ResolverProfile) -> ProviderResolverRuntime:
    key = (profile.endpoint, profile.max_concurrency, profile.mode)
    runtime = _RUNTIMES.get(key)
    if runtime is None:
        runtime = ProviderResolverRuntime(max_concurrency=profile.max_concurrency)
        _RUNTIMES[key] = runtime
    return runtime


def _resolver_operation_url(profile: ResolverProfile) -> str:
    raw_endpoint = profile.endpoint
    try:
        parsed = urlsplit(raw_endpoint.strip())
    except ValueError as exc:
        raise ProviderResolverError(
            "resolver_endpoint_rejected", "Resolver endpoint is malformed."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ProviderResolverError(
            "resolver_endpoint_rejected", "Resolver endpoint is not a safe service root."
        )
    path = "/scrape" if profile.mode == "trawl_scrape" else "/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


async def _validate_source_url(
    raw_url: str,
    declared_domains: Sequence[str],
    *,
    resolver: ProviderTargetResolver | None = None,
) -> str:
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise ProviderResolverError(
            "resolver_target_rejected", "Resolver target is malformed."
        ) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ProviderResolverError(
            "resolver_target_rejected", "Resolver target contains unsafe URL components."
        )
    host = parsed.hostname.casefold().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ProviderResolverError(
            "resolver_target_rejected",
            "Resolver targets must use a declared public domain.",
        )
    domains = tuple(_normalize_domain(value) for value in declared_domains)
    if not domains or not any(host == domain or host.endswith(f".{domain}") for domain in domains):
        raise ProviderResolverError(
            "resolver_target_rejected", "Resolver target is outside the declared domains."
        )
    effective_port = port or (443 if parsed.scheme == "https" else 80)
    resolve = resolver or _resolve_addresses
    try:
        addresses = await resolve(host, effective_port)
    except (OSError, TimeoutError) as exc:
        raise ProviderResolverError(
            "resolver_target_rejected", "Resolver target could not be resolved."
        ) from exc
    if not addresses:
        raise ProviderResolverError("resolver_target_rejected", "Resolver target did not resolve.")
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError as exc:
        raise ProviderResolverError(
            "resolver_target_rejected", "Resolver target resolved unsafely."
        ) from exc
    if any(not address.is_global for address in parsed_addresses):
        raise ProviderResolverError(
            "resolver_target_rejected", "Resolver target resolves to an unsafe network."
        )

    default_port = 443 if parsed.scheme == "https" else 80
    netloc = host if effective_port == default_port else f"{host}:{effective_port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def _normalize_domain(raw_domain: str) -> str:
    value = raw_domain.strip().casefold().lstrip(".").rstrip(".")
    if not value or len(value) > 253 or any(item in value for item in ("/", ":", "@")):
        raise ProviderResolverError(
            "resolver_target_rejected", "Declared resolver domain is invalid."
        )
    return value


async def _resolve_addresses(host: str, port: int) -> Sequence[str]:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(result[4][0]) for result in results))


def _validated_auth_headers(headers: Mapping[str, str]) -> dict[str, str]:
    if len(headers) > 4:
        raise ProviderResolverError(
            "resolver_auth_rejected", "Resolver authentication headers are unbounded."
        )
    result: dict[str, str] = {}
    for name, value in headers.items():
        folded = name.casefold()
        if (
            not name
            or any(character.isspace() for character in name)
            or folded in _FORBIDDEN_HEADERS
            or folded.startswith("proxy-")
            or "\r" in value
            or "\n" in value
        ):
            raise ProviderResolverError(
                "resolver_auth_rejected", "Resolver authentication header is unsafe."
            )
        result[name] = value
    return result


async def _read_bounded_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > _MAX_RESPONSE_BYTES:
            raise ProviderResolverError(
                "resolver_response_too_large", "Resolver response exceeded the 8 MiB limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)
