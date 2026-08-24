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


@pytest.mark.parametrize(
    ("location", "resolver"),
    [
        ("http://downloads.example/file.cbz", _public_resolver),
        ("https://user:secret@downloads.example/file.cbz", _public_resolver),
        ("https://downloads.example/file.cbz", _private_destination_resolver),
        ("/relative/file.cbz", _public_resolver),
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
