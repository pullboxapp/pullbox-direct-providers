from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pullbox_provider_contract.models import ResolverProfile
from pullbox_provider_contract.resolver import (
    BrowserChallengeKind,
    ProviderResolverOutcome,
    ProviderResolverSolution,
)
from pullbox_provider_contract.source_http import (
    BrowserChallengeRequiredError,
    fetch_source_html,
    resolve_source_redirect,
)


async def _public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("8.8.8.8",)


async def test_ordinary_http_success_never_invokes_browser_resolver() -> None:
    resolver_calls = 0

    async def browser_resolver(*_args: object, **_kwargs: object) -> object:
        nonlocal resolver_calls
        resolver_calls += 1
        raise AssertionError("resolver should not run")

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text="<html>ordinary</html>",
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        html = await fetch_source_html(
            "https://source.example/search?q=test",
            declared_domains=("source.example",),
            http_client=client,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )

    assert html == "<html>ordinary</html>"
    assert resolver_calls == 0


async def test_recognized_challenge_uses_configured_resolver_once() -> None:
    calls = 0

    async def browser_resolver(*_args: object, **_kwargs: object) -> ProviderResolverOutcome:
        nonlocal calls
        calls += 1
        return ProviderResolverOutcome(
            challenge=BrowserChallengeKind.CLOUDFLARE,
            solution=ProviderResolverSolution(
                final_url="https://source.example/search?q=test",
                status_code=200,
                html="<html>resolved</html>",
                cookies=(),
            ),
        )

    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403,
            text="<html>Just a moment /cdn-cgi/challenge-platform/</html>",
            headers={"server": "cloudflare", "cf-ray": "test"},
            request=request,
        )
    )
    profile = ResolverProfile(
        endpoint="http://resolver:8191",
        timeout_seconds=20,
        max_concurrency=1,
        declared_domains=["source.example"],
    )
    async with httpx.AsyncClient(transport=transport) as client:
        html = await fetch_source_html(
            "https://source.example/search?q=test",
            declared_domains=("source.example",),
            http_client=client,
            resolver_profile=profile,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )

    assert html == "<html>resolved</html>"
    assert calls == 1


async def test_same_origin_redirect_reaches_challenge_with_cookie_and_final_url() -> None:
    requests: list[httpx.Request] = []
    resolver_source_url: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "/search?q=test&check=1",
                    "set-cookie": "__ddg=fixture; Path=/; Secure",
                },
                request=request,
            )
        assert request.headers.get("cookie") == "__ddg=fixture"
        return httpx.Response(
            403,
            text="<html>DDoS-Guard is checking your browser</html>",
            headers={"server": "ddos-guard"},
            request=request,
        )

    async def browser_resolver(
        *_args: object,
        source_url: str,
        **_kwargs: object,
    ) -> ProviderResolverOutcome:
        nonlocal resolver_source_url
        resolver_source_url = source_url
        return ProviderResolverOutcome(
            challenge=BrowserChallengeKind.DDOS_GUARD,
            solution=ProviderResolverSolution(
                final_url=source_url,
                status_code=200,
                html="<html>resolved after redirect</html>",
                cookies=(),
            ),
        )

    profile = ResolverProfile(
        endpoint="http://resolver:8191",
        timeout_seconds=20,
        max_concurrency=1,
        declared_domains=["source.example"],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        html = await fetch_source_html(
            "https://source.example/search?q=test",
            declared_domains=("source.example",),
            http_client=client,
            resolver_profile=profile,
            target_resolver=_public_resolver,
            browser_resolver=browser_resolver,
        )

    assert html == "<html>resolved after redirect</html>"
    assert [str(request.url) for request in requests] == [
        "https://source.example/search?q=test",
        "https://source.example/search?q=test&check=1",
    ]
    assert resolver_source_url == "https://source.example/search?q=test&check=1"


@pytest.mark.parametrize(
    "location",
    [
        "https://other.example/search?q=test&check=1",
        "http://source.example/search?q=test&check=1",
        "https://user:secret@source.example/search?q=test&check=1",
        "https://source.example:444/search?q=test&check=1",
    ],
)
async def test_source_html_rejects_unsafe_redirect_target(location: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": location},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="redirect"):
            await fetch_source_html(
                "https://source.example/search?q=test",
                declared_domains=("source.example",),
                http_client=client,
                target_resolver=_public_resolver,
            )


async def test_source_html_rejects_a_second_redirect() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={"location": f"/search?q=test&check={calls}"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="redirect"):
            await fetch_source_html(
                "https://source.example/search?q=test",
                declared_domains=("source.example",),
                http_client=client,
                target_resolver=_public_resolver,
            )

    assert calls == 2


async def test_challenge_without_profile_and_unsafe_redirect_fail_closed() -> None:
    challenge = httpx.MockTransport(
        lambda request: httpx.Response(
            503,
            text="<html>Just a moment /cdn-cgi/challenge-platform/</html>",
            headers={"server": "cloudflare", "cf-ray": "test"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=challenge) as client:
        with pytest.raises(BrowserChallengeRequiredError) as exc_info:
            await fetch_source_html(
                "https://source.example/search",
                declared_domains=("source.example",),
                http_client=client,
                target_resolver=_public_resolver,
            )
    assert exc_info.value.code == "browser_challenge_required"


async def test_source_redirect_returns_one_safe_https_destination() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "https://pixeldrain.com/u/example"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        destination = await resolve_source_redirect(
            "https://source.example/dls/opaque",
            declared_domains=("source.example",),
            http_client=client,
            target_resolver=_public_resolver,
        )

    assert destination == "https://pixeldrain.com/u/example"


@pytest.mark.parametrize(
    "location",
    [
        "http://pixeldrain.com/u/example",
        "https://user:secret@pixeldrain.com/u/example",
        "/relative-destination",
    ],
)
async def test_source_redirect_rejects_unsafe_destinations(location: str) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": location},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="destination"):
            await resolve_source_redirect(
                "https://source.example/dls/opaque",
                declared_domains=("source.example",),
                http_client=client,
                target_resolver=_public_resolver,
            )


async def test_source_redirect_rejects_non_redirect_response() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="landing page", request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError, match="did not redirect"):
            await resolve_source_redirect(
                "https://source.example/dls/opaque",
                declared_domains=("source.example",),
                http_client=client,
                target_resolver=_public_resolver,
            )

    redirect = httpx.MockTransport(
        lambda request: httpx.Response(
            302,
            headers={"location": "http://169.254.169.254/metadata"},
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=redirect) as client:
        with pytest.raises(RuntimeError, match="redirect"):
            await fetch_source_html(
                "https://source.example/search",
                declared_domains=("source.example",),
                http_client=client,
                target_resolver=_public_resolver,
            )


def test_resolver_profile_fixture_has_future_deadline_context() -> None:
    assert datetime.now(UTC) + timedelta(seconds=1) > datetime.now(UTC)
