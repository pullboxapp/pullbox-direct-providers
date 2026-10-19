"""A blocked source must not disable a resolver for unrelated sources."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx
import pytest
from pullbox_provider_contract.models import ResolverProfile
from pullbox_provider_contract.resolver import (
    OrdinaryHttpResponse,
    ProviderResolverError,
    ProviderResolverRuntime,
    resolve_after_challenge,
)


async def _public(_host: str, _port: int) -> Sequence[str]:
    return ["8.8.8.8"]


def _blocked(*, reason: str = "ddos-guard-persistent") -> httpx.Response:
    return httpx.Response(
        500,
        json={
            "error": "Max tier reached without success",
            "timings": [{"tier": 3, "status": "blocked", "durationMs": 24000, "reason": reason}],
        },
    )


def _success() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "url": "https://other.example/search",
            "statusCode": 200,
            "html": "<html>Results</html>",
            "cookies": [],
        },
    )


async def _attempt(
    client: httpx.AsyncClient,
    runtime: ProviderResolverRuntime,
    url: str = "https://source.example/search?q=private",
) -> object:
    return await resolve_after_challenge(
        OrdinaryHttpResponse(403, {"server": "ddos-guard"}, "Checking your browser"),
        source_url=url,
        profile=ResolverProfile(
            endpoint="http://resolver:8191",
            mode="trawl_scrape",
            timeout_seconds=30,
            max_concurrency=1,
            declared_domains=["source.example", "other.example"],
        ),
        http_client=client,
        runtime=runtime,
        target_resolver=_public,
    )


@pytest.mark.parametrize(
    "reason",
    [
        "ddos-guard-persistent",
        "datacenter-ip-blocked (DDoS-Guard clearance cookie obtained "
        "but challenge persisted - needs residential proxy)",
    ],
)
async def test_trawl_block_preserves_challenge_reason_without_asserting_ip_block(
    reason: str,
) -> None:
    runtime = ProviderResolverRuntime(max_concurrency=1, failure_threshold=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _blocked(reason=reason))
    ) as client:
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime)
    assert exc.value.code == "resolver_source_blocked"
    assert "DDoS-Guard" in str(exc.value)
    assert "residential" not in str(exc.value)
    assert exc.value.retryable
    assert runtime.state == "closed", "A reachable resolver is not globally unavailable"


async def test_backoff_is_origin_scoped_and_success_elsewhere_does_not_clear_it() -> None:
    clock = [100.0]
    runtime = ProviderResolverRuntime(max_concurrency=1, clock=lambda: clock[0])
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _blocked() if len(calls) < 3 else _success()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(ProviderResolverError):
                await _attempt(client, runtime)
        for url in [
            "https://source.example/md5/another",
            "https://source.example:443/search?q=changed",
        ]:
            with pytest.raises(ProviderResolverError) as exc:
                await _attempt(client, runtime, url)
            assert exc.value.code == "resolver_source_backoff"
        assert len(calls) == 2
        assert await _attempt(client, runtime, "https://other.example/search") is not None
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime)
        assert exc.value.code == "resolver_source_backoff"
        assert len(calls) == 3
        clock[0] += 301
        assert await _attempt(client, runtime) is not None
        assert await _attempt(client, runtime) is not None
        assert len(calls) == 5


async def test_failed_recovery_probe_restarts_backoff() -> None:
    clock = [0.0]
    runtime = ProviderResolverRuntime(max_concurrency=1, clock=lambda: clock[0])
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _blocked()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(ProviderResolverError):
                await _attempt(client, runtime)
        clock[0] = 301
        with pytest.raises(ProviderResolverError) as probe:
            await _attempt(client, runtime)
        assert probe.value.code == "resolver_source_blocked"
        clock[0] = 302
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime)
        assert exc.value.code == "resolver_source_backoff"
        assert calls == 3


async def test_only_one_recovery_probe_is_admitted_and_cancellation_releases_it() -> None:
    clock = [0.0]
    runtime = ProviderResolverRuntime(max_concurrency=2, clock=lambda: clock[0])
    started = asyncio.Event()
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 3:
            started.set()
            await asyncio.Event().wait()
        return _blocked()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(ProviderResolverError):
                await _attempt(client, runtime)
        clock[0] = 301
        task = asyncio.create_task(_attempt(client, runtime))
        await started.wait()
        try:
            with pytest.raises(ProviderResolverError) as exc:
                await _attempt(client, runtime)
            assert exc.value.code == "resolver_source_backoff"
            assert calls == 3
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with pytest.raises(ProviderResolverError) as retry:
            await _attempt(client, runtime)
        assert retry.value.code == "resolver_source_blocked"
        assert calls == 4


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (
            500,
            {"timings": [{"tier": 3, "status": "timeout", "reason": "Scrape deadline exceeded"}]},
            "resolver_timed_out",
        ),
        (408, {}, "resolver_timed_out"),
        (
            500,
            {"timings": [{"tier": 3, "status": "error", "reason": "secret-token"}]},
            "resolver_unavailable",
        ),
        (
            500,
            {"timings": [{"tier": 3, "status": "blocked", "reason": "secret-token"}]},
            "resolver_source_blocked",
        ),
        (500, {"timings": "invalid", "error": "secret-token"}, "resolver_unavailable"),
        (401, {"timings": [{"tier": 3, "status": "blocked"}]}, "resolver_authentication_failed"),
        (429, {"timings": [{"tier": 3, "status": "blocked"}]}, "resolver_rate_limited"),
    ],
)
async def test_failure_classification_is_bounded_and_never_echoes_raw_messages(
    status: int, payload: dict, expected: str
) -> None:
    runtime = ProviderResolverRuntime(max_concurrency=1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=payload))
    ) as client:
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime)
    assert exc.value.code == expected
    assert "secret-token" not in str(exc.value)


async def test_source_backoff_does_not_create_an_extra_resolver_slot() -> None:
    runtime = ProviderResolverRuntime(max_concurrency=1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: _success())) as client:
        async with runtime.slot():
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.02):
                    await _attempt(client, runtime, "https://other.example/search")
    assert runtime.state == "closed"


@pytest.mark.parametrize("status", [408, 500])
async def test_explicit_browser_deadline_failure_backs_off_only_its_source(status: int) -> None:
    runtime = ProviderResolverRuntime(max_concurrency=1, failure_threshold=1)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={
                "timings": [{"tier": 3, "status": "timeout", "reason": "Scrape deadline exceeded"}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for _ in range(2):
            with pytest.raises(ProviderResolverError) as exc:
                await _attempt(client, runtime)
            assert exc.value.code == "resolver_timed_out"
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime)
        assert exc.value.code == "resolver_source_backoff"
        assert runtime.state == "closed"
        assert calls == 2
        assert "page-resolution deadline" in (exc.value.public_message or "")


async def test_transport_failure_keeps_endpoint_circuit_not_source_backoff() -> None:
    runtime = ProviderResolverRuntime(max_concurrency=1, failure_threshold=1)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private upstream detail")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime)
        assert exc.value.code == "resolver_timed_out"
        assert runtime.state == "open"
        with pytest.raises(ProviderResolverError) as exc:
            await _attempt(client, runtime, "https://other.example/search")
        assert exc.value.code == "resolver_circuit_open"


async def test_source_state_is_bounded_and_old_isolated_failures_do_not_accumulate() -> None:
    clock = [0.0]
    runtime = ProviderResolverRuntime(max_concurrency=1, clock=lambda: clock[0])
    error = ProviderResolverError("resolver_source_blocked", "safe", reason="DDoS-Guard")
    await runtime.record_source_failure("https://source.example:443", error)
    clock[0] = 301
    await runtime.record_source_failure("https://source.example:443", error)
    async with runtime.slot(source_origin="https://source.example:443"):
        pass
    for number in range(200):
        await runtime.record_source_failure(f"https://source{number}.example:443", error)
    assert len(runtime._sources) == 128
