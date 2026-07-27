"""Ordinary-HTTP-first browser resolver helper contracts."""

from __future__ import annotations

import json
from collections.abc import Sequence

import httpx
import pytest
from pullbox_provider_contract.models import ResolverProfile
from pullbox_provider_contract.resolver import (
    BrowserChallengeKind,
    OrdinaryHttpResponse,
    ProviderResolverError,
    ProviderResolverRuntime,
    detect_browser_challenge,
    resolve_after_challenge,
)

AUTH_SECRET = "provider-resolver-auth-secret"
COOKIE_SECRET = "provider-source-cookie-secret"


async def _resolve_public(_host: str, _port: int) -> Sequence[str]:
    return ["8.8.8.8"]


async def _resolve_private(_host: str, _port: int) -> Sequence[str]:
    return ["127.0.0.1"]


def _profile() -> ResolverProfile:
    return ResolverProfile(
        endpoint="http://resolver:8191",
        timeout_seconds=60,
        max_concurrency=1,
        declared_domains=["source.example"],
        authentication_headers={"Authorization": f"Bearer {AUTH_SECRET}"},
    )


def _solution(*, url: str = "https://source.example/comics") -> dict[str, object]:
    return {
        "status": "ok",
        "message": "Challenge solved!",
        "solution": {
            "url": url,
            "status": 200,
            "headers": {"x-private": "not-retained"},
            "response": "<html>Comics</html>",
            "cookies": [
                {
                    "name": "cf_clearance",
                    "value": COOKIE_SECRET,
                    "domain": ".source.example",
                    "path": "/",
                }
            ],
            "userAgent": "Resolver Browser",
        },
    }


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (
            503,
            {"server": "cloudflare", "cf-ray": "abc"},
            "Just a moment...",
            BrowserChallengeKind.CLOUDFLARE,
        ),
        (403, {"server": "ddos-guard"}, "Checking your browser", BrowserChallengeKind.DDOS_GUARD),
        (
            429,
            {},
            '<form id="challenge-form">Enable JavaScript and cookies</form>',
            BrowserChallengeKind.BROWSER_CHALLENGE,
        ),
        (403, {}, "Forbidden", None),
        (200, {"server": "cloudflare"}, "Normal source page", None),
        (404, {}, "Just a moment...", None),
    ],
)
def test_challenge_detection_is_bounded_and_explicit(
    status: int,
    headers: dict[str, str],
    body: str,
    expected: BrowserChallengeKind | None,
) -> None:
    assert detect_browser_challenge(status, headers, body) is expected


async def test_non_challenge_response_never_calls_resolver() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_after_challenge(
            OrdinaryHttpResponse(status_code=403, headers={}, body="Forbidden"),
            source_url="https://source.example/comics",
            profile=_profile(),
            http_client=client,
            target_resolver=_resolve_public,
        )

    assert result is None
    assert calls == 0


async def test_recognized_challenge_calls_standard_v1_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert str(request.url) == "http://resolver:8191/v1"
        assert request.headers["Authorization"] == f"Bearer {AUTH_SECRET}"
        assert json.loads(request.content) == {
            "cmd": "request.get",
            "url": "https://source.example/comics",
            "maxTimeout": 60000,
        }
        return httpx.Response(200, json=_solution())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await resolve_after_challenge(
            OrdinaryHttpResponse(
                status_code=503,
                headers={"server": "cloudflare", "cf-ray": "abc"},
                body="Just a moment...",
            ),
            source_url="https://source.example/comics",
            profile=_profile(),
            http_client=client,
            target_resolver=_resolve_public,
        )

    assert calls == 1
    assert result is not None
    assert result.challenge is BrowserChallengeKind.CLOUDFLARE
    assert result.solution.status_code == 200
    assert result.solution.html == "<html>Comics</html>"
    assert AUTH_SECRET not in repr(_profile())
    assert COOKIE_SECRET not in repr(result)
    assert "not-retained" not in repr(result)


@pytest.mark.parametrize(
    "source_url",
    [
        "https://evil.example/comics",
        "https://source.example.evil.test/comics",
        "https://user:pass@source.example/comics",
        "file:///etc/passwd",
    ],
)
async def test_source_url_cannot_escape_declared_domains(source_url: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_solution()))
    ) as client:
        with pytest.raises(ProviderResolverError):
            await resolve_after_challenge(
                OrdinaryHttpResponse(
                    status_code=503,
                    headers={"server": "cloudflare"},
                    body="Just a moment...",
                ),
                source_url=source_url,
                profile=_profile(),
                http_client=client,
                target_resolver=_resolve_public,
            )


async def test_source_url_cannot_resolve_to_an_internal_network() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=_solution()))
    ) as client:
        with pytest.raises(ProviderResolverError) as exc_info:
            await resolve_after_challenge(
                OrdinaryHttpResponse(
                    status_code=503,
                    headers={"server": "cloudflare"},
                    body="Just a moment...",
                ),
                source_url="https://source.example/comics",
                profile=_profile(),
                http_client=client,
                target_resolver=_resolve_private,
            )

    assert exc_info.value.code == "resolver_target_rejected"


async def test_returned_url_and_endpoint_redirects_fail_closed() -> None:
    for response, code in (
        (
            httpx.Response(200, json=_solution(url="https://evil.example/escape")),
            "resolver_redirect_rejected",
        ),
        (
            httpx.Response(302, headers={"Location": "http://169.254.169.254/"}),
            "resolver_endpoint_redirect_rejected",
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request, value=response: value)
        ) as client:
            with pytest.raises(ProviderResolverError) as exc_info:
                await resolve_after_challenge(
                    OrdinaryHttpResponse(
                        status_code=503,
                        headers={"server": "cloudflare"},
                        body="Just a moment...",
                    ),
                    source_url="https://source.example/comics",
                    profile=_profile(),
                    http_client=client,
                    target_resolver=_resolve_public,
                )
            assert exc_info.value.code == code


async def test_solver_output_is_bounded_and_malformed_output_is_classified() -> None:
    for response, code in (
        (httpx.Response(200, content=b"not-json"), "resolver_malformed_response"),
        (
            httpx.Response(200, content=b"{" + b" " * (8 * 1024 * 1024) + b"}"),
            "resolver_response_too_large",
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request, value=response: value)
        ) as client:
            with pytest.raises(ProviderResolverError) as exc_info:
                await resolve_after_challenge(
                    OrdinaryHttpResponse(
                        status_code=503,
                        headers={"server": "cloudflare"},
                        body="Just a moment...",
                    ),
                    source_url="https://source.example/comics",
                    profile=_profile(),
                    http_client=client,
                    target_resolver=_resolve_public,
                )
            assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("response", "expected_code", "retryable"),
    [
        (httpx.Response(401), "resolver_authentication_failed", False),
        (httpx.Response(429), "resolver_rate_limited", True),
        (httpx.Response(503), "resolver_unavailable", True),
        (
            httpx.Response(200, json={**_solution(), "status": "error"}),
            "resolver_challenge_failed",
            False,
        ),
    ],
)
async def test_resolver_http_failures_are_classified(
    response: httpx.Response,
    expected_code: str,
    retryable: bool,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        with pytest.raises(ProviderResolverError) as exc_info:
            await resolve_after_challenge(
                OrdinaryHttpResponse(
                    status_code=503,
                    headers={"server": "cloudflare"},
                    body="Just a moment...",
                ),
                source_url="https://source.example/comics",
                profile=_profile(),
                http_client=client,
                target_resolver=_resolve_public,
                runtime=ProviderResolverRuntime(max_concurrency=1),
            )

    assert exc_info.value.code == expected_code
    assert exc_info.value.retryable is retryable


@pytest.mark.parametrize(
    "endpoint",
    [
        "file:///tmp/resolver",
        "http://user:password@resolver:8191",
        "http://resolver:8191/admin",
    ],
)
async def test_resolver_endpoint_must_be_a_safe_service_root(endpoint: str) -> None:
    profile = _profile().model_copy(update={"endpoint": endpoint})

    with pytest.raises(ProviderResolverError) as exc_info:
        await resolve_after_challenge(
            OrdinaryHttpResponse(
                status_code=503,
                headers={"server": "cloudflare"},
                body="Just a moment...",
            ),
            source_url="https://source.example/comics",
            profile=profile,
            target_resolver=_resolve_public,
        )

    assert exc_info.value.code == "resolver_endpoint_rejected"


@pytest.mark.parametrize(
    "authentication_headers",
    [
        {"Cookie": "secret"},
        {"X-Unsafe": "value\r\ninjected: true"},
        {f"X-Key-{index}": "value" for index in range(5)},
    ],
)
async def test_resolver_authentication_headers_are_bounded_and_safe(
    authentication_headers: dict[str, str],
) -> None:
    profile = _profile().model_copy(update={"authentication_headers": authentication_headers})

    with pytest.raises(ProviderResolverError) as exc_info:
        await resolve_after_challenge(
            OrdinaryHttpResponse(
                status_code=503,
                headers={"server": "cloudflare"},
                body="Just a moment...",
            ),
            source_url="https://source.example/comics",
            profile=profile,
            target_resolver=_resolve_public,
        )

    assert exc_info.value.code == "resolver_auth_rejected"


@pytest.mark.parametrize(
    "addresses",
    [(), ("not-an-ip",)],
)
async def test_source_url_requires_a_valid_public_dns_answer(
    addresses: tuple[str, ...],
) -> None:
    async def resolve(_host: str, _port: int) -> Sequence[str]:
        return addresses

    with pytest.raises(ProviderResolverError) as exc_info:
        await resolve_after_challenge(
            OrdinaryHttpResponse(
                status_code=503,
                headers={"server": "cloudflare"},
                body="Just a moment...",
            ),
            source_url="https://source.example/comics",
            profile=_profile(),
            target_resolver=resolve,
        )

    assert exc_info.value.code == "resolver_target_rejected"


async def test_provider_runtime_circuit_is_fail_fast_and_recovers() -> None:
    clock = [100.0]
    runtime = ProviderResolverRuntime(
        max_concurrency=1,
        failure_threshold=2,
        cooldown_seconds=30,
        clock=lambda: clock[0],
    )

    await runtime.record_failure()
    await runtime.record_failure()
    assert runtime.state == "open"
    with pytest.raises(ProviderResolverError) as opened:
        async with runtime.slot():
            pass
    assert opened.value.code == "resolver_circuit_open"

    clock[0] += 31
    async with runtime.slot():
        assert runtime.state == "half_open"
        with pytest.raises(ProviderResolverError) as busy:
            async with runtime.slot():
                pass
        assert busy.value.code == "resolver_busy"
        await runtime.record_success()
    assert runtime.state == "closed"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrency": 0},
        {"max_concurrency": 1, "failure_threshold": 0},
        {"max_concurrency": 1, "cooldown_seconds": 0},
    ],
)
def test_provider_runtime_rejects_unbounded_or_invalid_limits(
    kwargs: dict[str, int],
) -> None:
    with pytest.raises(ValueError):
        ProviderResolverRuntime(**kwargs)
