"""Request-scoped LibGen HTTP and browser-resolver session continuity."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pullbox_provider_contract.models import ResolverProfile
from pullbox_provider_contract.resolver import (
    BrowserChallengeKind,
    OrdinaryHttpResponse,
    ProviderResolverOutcome,
    ProviderTargetResolver,
    _validate_source_url,
    detect_browser_challenge,
    resolve_after_challenge,
)
from pullbox_provider_contract.source_http import BrowserChallengeRequiredError

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_COOKIE_NAME = re.compile(r"\A[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}\Z")
_MAX_TEXT_BYTES = 2 * 1024 * 1024
_MAX_REDIRECT_LENGTH = 4_000
_DEFAULT_USER_AGENT = "PullboxDirectProvider/0.1"

BrowserResolver = Callable[..., Awaitable[ProviderResolverOutcome | None]]


class LibGenSourceError(RuntimeError):
    """A bounded source operation failed without exposing response details."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class _SessionCookie:
    name: str
    value: str = field(repr=False)
    domain: str
    path: str


class LibGenSourceSession:
    """Keep resolver state in memory for exactly one provider operation."""

    def __init__(
        self,
        source_origin: str,
        *,
        resolver_profile: ResolverProfile | None = None,
        http_client: httpx.AsyncClient | None = None,
        target_resolver: ProviderTargetResolver | None = None,
        browser_resolver: BrowserResolver = resolve_after_challenge,
    ) -> None:
        parsed = urlsplit(source_origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise LibGenSourceError(
                "source_origin_invalid",
                "LibGen source origin is invalid.",
            ) from exc
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
            raise LibGenSourceError("source_origin_invalid", "LibGen source origin is invalid.")
        self._origin = f"https://{parsed.hostname.casefold()}"
        self._source_host = parsed.hostname.casefold()
        self._resolver_profile = resolver_profile
        self._target_resolver = target_resolver
        self._browser_resolver = browser_resolver
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
        )
        self._user_agent = _DEFAULT_USER_AGENT
        self._cookies: tuple[_SessionCookie, ...] = ()

    async def fetch_text(self, raw_url: str, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
        safe_url = await self._safe_source_url(raw_url)
        response, body = await self._send(safe_url, accept="text/html, application/json")
        try:
            challenge = _recognized_challenge(response.status_code, response.headers, body)
            if challenge is not None:
                return await self._resolve_page(
                    safe_url,
                    response=response,
                    body=body,
                    challenge=challenge,
                    max_bytes=max_bytes,
                )
            if response.status_code in _REDIRECT_STATUSES:
                raise LibGenSourceError("source_redirect_rejected", "LibGen source redirected.")
            _raise_for_source_status(response.status_code)
            if len(body) > max_bytes:
                raise LibGenSourceError(
                    "source_response_too_large",
                    "LibGen source response exceeds the supported size.",
                )
            return body.decode(response.encoding or "utf-8", errors="replace")
        finally:
            await response.aclose()

    async def resolve_redirect(self, raw_url: str) -> str:
        safe_url = await self._safe_source_url(raw_url)
        response, body = await self._send(safe_url, accept="*/*")
        try:
            challenge = _recognized_challenge(response.status_code, response.headers, body)
            if challenge is not None:
                await self._resolve_page(
                    safe_url,
                    response=response,
                    body=body,
                    challenge=challenge,
                    max_bytes=_MAX_TEXT_BYTES,
                )
            else:
                return await self._redirect_destination(response)
        finally:
            await response.aclose()

        retry, _ = await self._send(safe_url, accept="*/*", read_body=False)
        try:
            return await self._redirect_destination(retry)
        finally:
            await retry.aclose()

    async def aclose(self) -> None:
        self._cookies = ()
        self._user_agent = _DEFAULT_USER_AGENT
        if self._owns_client:
            await self._client.aclose()

    async def _resolve_page(
        self,
        safe_url: str,
        *,
        response: httpx.Response,
        body: bytes,
        challenge: BrowserChallengeKind,
        max_bytes: int,
    ) -> str:
        if self._resolver_profile is None:
            raise BrowserChallengeRequiredError
        ordinary = OrdinaryHttpResponse(
            status_code=response.status_code,
            headers=_safe_headers(response.headers),
            body=body,
        )
        outcome = await self._browser_resolver(
            ordinary,
            source_url=safe_url,
            profile=self._resolver_profile,
            recognized_challenge=challenge,
            target_resolver=self._target_resolver,
        )
        if outcome is None or outcome.solution.status_code >= 400:
            raise LibGenSourceError(
                "browser_resolution_failed",
                "LibGen browser resolution did not return a usable source page.",
            )
        encoded = outcome.solution.html.encode("utf-8")
        if len(encoded) > max_bytes:
            raise LibGenSourceError(
                "source_response_too_large",
                "LibGen resolved response exceeds the supported size.",
            )
        self._apply_solution_state(outcome)
        return outcome.solution.html

    async def _send(
        self,
        safe_url: str,
        *,
        accept: str,
        read_body: bool = True,
    ) -> tuple[httpx.Response, bytes]:
        try:
            response = await self._client.send(
                self._client.build_request(
                    "GET",
                    safe_url,
                    headers=self._request_headers(safe_url, accept=accept),
                ),
                stream=True,
                follow_redirects=False,
            )
            body = await _read_bounded(response) if read_body else b""
            return response, body
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            raise LibGenSourceError(
                "source_unavailable",
                "LibGen source request is temporarily unavailable.",
            ) from exc

    async def _safe_source_url(self, raw_url: str) -> str:
        safe_url = await _validate_source_url(
            raw_url,
            (self._source_host,),
            resolver=self._target_resolver,
        )
        parsed = urlsplit(safe_url)
        if parsed.scheme != "https" or parsed.hostname != self._source_host:
            raise LibGenSourceError(
                "source_url_rejected",
                "LibGen source URL is outside the configured origin.",
                retryable=False,
            )
        return safe_url

    def _request_headers(self, raw_url: str, *, accept: str) -> dict[str, str]:
        parsed = urlsplit(raw_url)
        host = parsed.hostname or ""
        path = parsed.path or "/"
        values = [
            f"{cookie.name}={cookie.value}"
            for cookie in self._cookies
            if (host == cookie.domain or host.endswith(f".{cookie.domain}"))
            and path.startswith(cookie.path)
        ]
        headers = {"Accept": accept, "User-Agent": self._user_agent}
        if values:
            headers["Cookie"] = "; ".join(values)
        return headers

    def _apply_solution_state(self, outcome: ProviderResolverOutcome) -> None:
        user_agent = outcome.solution.user_agent
        if user_agent is not None:
            if (
                not user_agent.strip()
                or len(user_agent) > 500
                or any(marker in user_agent for marker in ("\r", "\n"))
            ):
                raise LibGenSourceError(
                    "browser_resolution_failed",
                    "LibGen resolver user-agent state is invalid.",
                )
            self._user_agent = user_agent

        cookies: list[_SessionCookie] = []
        for cookie in outcome.solution.cookies:
            domain = (cookie.domain or self._source_host).casefold().lstrip(".").rstrip(".")
            path = cookie.path or "/"
            if (
                _COOKIE_NAME.fullmatch(cookie.name) is None
                or len(cookie.value) > 16_384
                or any(marker in cookie.value for marker in ("\r", "\n"))
                or not (self._source_host == domain or self._source_host.endswith(f".{domain}"))
                or not path.startswith("/")
                or len(path) > 2_000
            ):
                continue
            cookies.append(
                _SessionCookie(
                    name=cookie.name,
                    value=cookie.value,
                    domain=domain,
                    path=path,
                )
            )
        self._cookies = tuple(cookies)

    async def _redirect_destination(self, response: httpx.Response) -> str:
        if response.status_code not in _REDIRECT_STATUSES:
            _raise_for_source_status(response.status_code)
            raise LibGenSourceError(
                "artifact_unavailable",
                "LibGen source link did not redirect to an artifact destination.",
            )
        return await _validate_public_destination(
            response.headers.get("location"),
            resolver=self._target_resolver,
        )


def _recognized_challenge(
    status_code: int,
    headers: Mapping[str, str],
    body: bytes,
) -> BrowserChallengeKind | None:
    sample = body[: 64 * 1024].decode("utf-8", errors="ignore").casefold()
    if (
        status_code == 200
        and "<title>welcome to nginx!</title>" in sample
        and "<h1>welcome to nginx!</h1>" in sample
        and "further configuration is required" in sample
    ):
        return BrowserChallengeKind.BROWSER_CHALLENGE
    return detect_browser_challenge(status_code, headers, body)


async def _read_bounded(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_TEXT_BYTES:
            raise LibGenSourceError(
                "source_response_too_large",
                "LibGen source response exceeds the supported size.",
            )
    return bytes(body)


def _safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.casefold() in {"cf-ray", "content-type", "retry-after", "server"}
    }


def _raise_for_source_status(status_code: int) -> None:
    if status_code == 429:
        raise LibGenSourceError("source_rate_limited", "LibGen source is rate limited.")
    if status_code >= 400:
        raise LibGenSourceError(
            "source_unavailable",
            f"LibGen source returned HTTP {status_code}.",
        )


async def _validate_public_destination(
    raw_url: str | None,
    *,
    resolver: ProviderTargetResolver | None,
) -> str:
    if not raw_url or len(raw_url) > _MAX_REDIRECT_LENGTH:
        raise LibGenSourceError(
            "artifact_unavailable",
            "LibGen artifact destination is invalid.",
            retryable=False,
        )
    try:
        parsed = urlsplit(raw_url.strip())
        port = parsed.port
    except ValueError as exc:
        raise LibGenSourceError(
            "artifact_unavailable",
            "LibGen artifact destination is invalid.",
            retryable=False,
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise LibGenSourceError(
            "artifact_unavailable",
            "LibGen artifact destination is unsafe.",
            retryable=False,
        )
    host = parsed.hostname.casefold()
    effective_resolver = resolver or _resolve_addresses
    try:
        addresses = tuple(
            ipaddress.ip_address(value) for value in await effective_resolver(host, 443)
        )
    except (OSError, TimeoutError, ValueError) as exc:
        raise LibGenSourceError(
            "artifact_unavailable",
            "LibGen artifact destination could not be validated.",
        ) from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise LibGenSourceError(
            "artifact_unavailable",
            "LibGen artifact destination is not public.",
            retryable=False,
        )
    return raw_url.strip()


async def _resolve_addresses(host: str, port: int) -> Sequence[str]:
    records = await asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({str(record[4][0]) for record in records}))
