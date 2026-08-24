"""Ordinary-HTTP-first helper for a bounded FlareSolverr-compatible profile."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pullbox_provider_contract.models import ResolverProfile

_CHALLENGE_BODY_LIMIT = 64 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
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
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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


class ProviderResolverRuntime:
    """Process-local fail-fast concurrency gate and circuit breaker."""

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
        self._active = 0
        self._failures = 0
        self._state = "closed"
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        return self._state

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._lock:
            if self._state == "open":
                opened_at = self._opened_at
                if opened_at is None or self._clock() - opened_at < self._cooldown_seconds:
                    raise ProviderResolverError(
                        "resolver_circuit_open",
                        "Resolver is temporarily unavailable while its circuit recovers.",
                        retryable=True,
                    )
                self._state = "half_open"
            if self._active >= self._max_concurrency or (
                self._state == "half_open" and self._active > 0
            ):
                raise ProviderResolverError(
                    "resolver_busy",
                    "Resolver concurrency is currently full; retry later.",
                    retryable=True,
                )
            self._active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active -= 1

    async def record_success(self) -> None:
        async with self._lock:
            self._failures = 0
            self._opened_at = None
            self._state = "closed"

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            if self._failures >= self._failure_threshold:
                self._opened_at = self._clock()
                self._state = "open"


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
    try:
        try:
            async with active_runtime.slot():
                outcome = await _perform_resolution(
                    client=client,
                    endpoint=endpoint,
                    headers=headers,
                    payload=payload,
                    profile=profile,
                    challenge=challenge,
                    target_resolver=target_resolver,
                )
                await active_runtime.record_success()
                return outcome
        except ProviderResolverError as exc:
            if exc.code not in {
                "resolver_busy",
                "resolver_circuit_open",
                "resolver_redirect_rejected",
                "resolver_target_rejected",
            }:
                await active_runtime.record_failure()
            raise
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
