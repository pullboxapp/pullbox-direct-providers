from __future__ import annotations

from collections.abc import Sequence

import httpx
import pytest
from pullbox_provider_contract.models import ResolverProfile
from pullbox_provider_contract.resolver import (
    BrowserChallengeKind,
    ProviderResolverCookie,
    ProviderResolverOutcome,
    ProviderResolverSolution,
)
from pullbox_provider_contract.source_http import BrowserChallengeRequiredError
from pullbox_provider_libgen.transport import (
    LibGenSourceError,
    LibGenSourceSession,
    _validate_public_destination,
)

_GATE = """<html><head><title>Welcome to nginx!</title></head><body>
<h1>Welcome to nginx!</h1><p>Further configuration is required.</p></body></html>"""


async def _public_resolver(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34",)


async def _private_destination_resolver(host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34",) if host == "libgen.gl" else ("127.0.0.1",)


def _profile() -> ResolverProfile:
    return ResolverProfile(
        endpoint="http://resolver:8191",
        timeout_seconds=30,
        max_concurrency=1,
        declared_domains=["libgen.gl"],
    )


def _resolver_outcome(
    *,
    status_code: int = 200,
    html: str = "<table id='tablelibgen'></table>",
    cookies: tuple[ProviderResolverCookie, ...] = (),
    user_agent: str | None = None,
) -> ProviderResolverOutcome:
    return ProviderResolverOutcome(
        challenge=BrowserChallengeKind.BROWSER_CHALLENGE,
        solution=ProviderResolverSolution(
            final_url="https://libgen.gl/index.php?req=test",
            status_code=status_code,
            html=html,
            cookies=cookies,
            user_agent=user_agent,
        ),
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://libgen.gl",
        "https://libgen.gl/path",
        "https://user:secret@libgen.gl",
        "https://libgen.gl:444",
        "https://libgen.gl?query=unsafe",
        "https://libgen.gl#unsafe",
    ],
)
def test_source_session_rejects_non_origin_configuration(origin: str) -> None:
    with pytest.raises(LibGenSourceError) as exc_info:
        LibGenSourceSession(origin)

    assert exc_info.value.code == "source_origin_invalid"


async def test_ordinary_source_success_does_not_invoke_browser_resolver() -> None:
    resolver_calls = 0

    async def browser_resolver(*_args: object, **_kwargs: object) -> object:
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("browser resolver should not run")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="catalog", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        assert await session.fetch_text("https://libgen.gl/index.php?req=test") == "catalog"
        await session.aclose()

    assert resolver_calls == 0


async def test_nginx_gate_uses_resolver_and_reuses_state_only_inside_session() -> None:
    source_requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        source_requests.append(
            (
                request.url.path,
                request.headers.get("user-agent", ""),
                request.headers.get("cookie", ""),
            )
        )
        if request.url.path == "/index.php":
            return httpx.Response(
                200,
                text=_GATE,
                headers={"server": "cloudflare", "content-type": "text/html"},
                request=request,
            )
        return httpx.Response(200, text='{"record": {}}', request=request)

    resolver_calls = 0

    async def browser_resolver(*_args: object, **kwargs: object) -> ProviderResolverOutcome:
        nonlocal resolver_calls
        resolver_calls += 1
        assert kwargs["recognized_challenge"] is BrowserChallengeKind.BROWSER_CHALLENGE
        return ProviderResolverOutcome(
            challenge=BrowserChallengeKind.BROWSER_CHALLENGE,
            solution=ProviderResolverSolution(
                final_url="https://libgen.gl/index.php?req=test",
                status_code=200,
                html="<table id='tablelibgen'></table>",
                cookies=(
                    ProviderResolverCookie(
                        name="source_clearance",
                        value="request-secret",
                        domain=".libgen.gl",
                        path="/",
                    ),
                ),
                user_agent="Resolver Browser",
            ),
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        first = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        assert "tablelibgen" in await first.fetch_text("https://libgen.gl/index.php?req=test")
        assert await first.fetch_text("https://libgen.gl/json.php?object=f") == '{"record": {}}'
        await first.aclose()

        second = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        assert await second.fetch_text("https://libgen.gl/json.php?object=f") == '{"record": {}}'
        await second.aclose()

    assert resolver_calls == 1
    assert source_requests == [
        ("/index.php", "PullboxDirectProvider/0.1", ""),
        ("/json.php", "Resolver Browser", "source_clearance=request-secret"),
        ("/json.php", "PullboxDirectProvider/0.1", ""),
    ]
    assert "request-secret" not in repr(first)


async def test_nginx_gate_without_resolver_is_actionable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=_GATE,
            headers={"server": "cloudflare"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(BrowserChallengeRequiredError):
            await session.fetch_text("https://libgen.gl/index.php?req=test")


async def test_similar_nginx_text_does_not_trigger_resolver() -> None:
    resolver_calls = 0

    async def browser_resolver(*_args: object, **_kwargs: object) -> object:
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("browser resolver should not run")

    body = "<html><title>Welcome to nginx!</title><p>Ordinary documentation.</p></html>"
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        assert await session.fetch_text("https://libgen.gl/index.php?req=test") == body

    assert resolver_calls == 0


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [(429, "source_rate_limited"), (503, "source_unavailable")],
)
async def test_source_http_status_is_classified_without_response_detail(
    status_code: int,
    expected_code: str,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code,
            text="private upstream response",
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test")

    assert exc_info.value.code == expected_code
    assert "private upstream response" not in str(exc_info.value)


async def test_source_network_failure_is_bounded_and_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private DNS detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test")

    assert exc_info.value.code == "source_unavailable"
    assert exc_info.value.retryable is True
    assert "private DNS detail" not in str(exc_info.value)


async def test_source_response_respects_operation_specific_size_limit() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"123456", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/json.php?object=f", max_bytes=5)

    assert exc_info.value.code == "source_response_too_large"


async def test_streaming_source_response_has_a_hard_global_bound() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b"x" * (2 * 1024 * 1024 + 1),
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test")

    assert exc_info.value.code == "source_response_too_large"


async def test_source_page_redirect_is_rejected() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "https://libgen.gl/elsewhere"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test")

    assert exc_info.value.code == "source_redirect_rejected"


@pytest.mark.parametrize("resolver_status", [None, 503])
async def test_unusable_browser_resolution_is_rejected(
    resolver_status: int | None,
) -> None:
    async def browser_resolver(*_args: object, **_kwargs: object) -> ProviderResolverOutcome | None:
        return None if resolver_status is None else _resolver_outcome(status_code=resolver_status)

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_GATE, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test")

    assert exc_info.value.code == "browser_resolution_failed"


async def test_resolved_page_respects_response_size_limit() -> None:
    async def browser_resolver(*_args: object, **_kwargs: object) -> ProviderResolverOutcome:
        return _resolver_outcome(html="123456")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_GATE, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test", max_bytes=5)

    assert exc_info.value.code == "source_response_too_large"


async def test_invalid_resolver_user_agent_is_rejected() -> None:
    async def browser_resolver(*_args: object, **_kwargs: object) -> ProviderResolverOutcome:
        return _resolver_outcome(user_agent="unsafe\r\nheader")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=_GATE, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.fetch_text("https://libgen.gl/index.php?req=test")

    assert exc_info.value.code == "browser_resolution_failed"


async def test_invalid_resolver_cookie_is_not_replayed() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, text=_GATE, request=request)
        return httpx.Response(200, text="catalog", request=request)

    async def browser_resolver(*_args: object, **_kwargs: object) -> ProviderResolverOutcome:
        return _resolver_outcome(
            cookies=(
                ProviderResolverCookie(
                    name="bad cookie",
                    value="private-value",
                    domain="libgen.gl",
                    path="/",
                ),
            )
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        await session.fetch_text("https://libgen.gl/index.php?req=test")
        await session.fetch_text("https://libgen.gl/json.php?object=f")

    assert requests[1].headers.get("cookie") is None


async def test_redirect_returns_one_public_https_destination() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            307,
            headers={"location": "https://downloads.example/file.cbz?token=opaque"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        destination = await session.resolve_redirect(
            "https://libgen.gl/get.php?md5=0123456789abcdef0123456789abcdef"
        )

    assert destination == "https://downloads.example/file.cbz?token=opaque"


async def test_redirect_challenge_retries_with_resolver_state() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, text=_GATE, request=request)
        return httpx.Response(
            307,
            headers={"location": "https://downloads.example/file.cbz"},
            request=request,
        )

    async def browser_resolver(*_args: object, **_kwargs: object) -> ProviderResolverOutcome:
        return _resolver_outcome()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            resolver_profile=_profile(),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )
        destination = await session.resolve_redirect(
            "https://libgen.gl/get.php?md5=0123456789abcdef0123456789abcdef"
        )

    assert destination == "https://downloads.example/file.cbz"
    assert requests == 2


async def test_non_redirect_artifact_response_is_unavailable() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="not a file redirect", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=_public_resolver,
        )
        with pytest.raises(LibGenSourceError) as exc_info:
            await session.resolve_redirect(
                "https://libgen.gl/get.php?md5=0123456789abcdef0123456789abcdef"
            )

    assert exc_info.value.code == "artifact_unavailable"


@pytest.mark.parametrize(
    ("location", "resolver"),
    [
        ("http://downloads.example/file.cbz", _public_resolver),
        ("https://user:secret@downloads.example/file.cbz", _public_resolver),
        ("https://downloads.example/file.cbz", _private_destination_resolver),
        ("/relative/file.cbz", _public_resolver),
        ("https://downloads.example:444/file.cbz", _public_resolver),
        ("https://downloads.example/file.cbz#fragment", _public_resolver),
    ],
)
async def test_redirect_rejects_unsafe_destinations(location: str, resolver) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(307, headers={"location": location}, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        session = LibGenSourceSession(
            "https://libgen.gl",
            http_client=client,
            target_resolver=resolver,
        )
        with pytest.raises(LibGenSourceError, match="destination"):
            await session.resolve_redirect(
                "https://libgen.gl/get.php?md5=0123456789abcdef0123456789abcdef"
            )


async def test_redirect_rejects_missing_or_unresolvable_destination() -> None:
    async def unavailable(host: str, _port: int) -> Sequence[str]:
        if host == "libgen.gl":
            return ("93.184.216.34",)
        raise OSError("private DNS detail")

    for location in (None, "https://downloads.example/file.cbz"):
        transport = httpx.MockTransport(
            lambda request, value=location: httpx.Response(
                307,
                headers={} if value is None else {"location": value},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            session = LibGenSourceSession(
                "https://libgen.gl",
                http_client=client,
                target_resolver=unavailable,
            )
            with pytest.raises(LibGenSourceError) as exc_info:
                await session.resolve_redirect(
                    "https://libgen.gl/get.php?md5=0123456789abcdef0123456789abcdef"
                )

        assert exc_info.value.code == "artifact_unavailable"


async def test_destination_with_malformed_port_is_rejected() -> None:
    with pytest.raises(LibGenSourceError) as exc_info:
        await _validate_public_destination(
            "https://downloads.example:invalid/file.cbz",
            resolver=_public_resolver,
        )

    assert exc_info.value.code == "artifact_unavailable"
    assert exc_info.value.retryable is False
