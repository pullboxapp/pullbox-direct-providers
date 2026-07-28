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
from pullbox_provider_contract.source_http import fetch_source_html


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
        with pytest.raises(RuntimeError, match="resolver"):
            await fetch_source_html(
                "https://source.example/search",
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
