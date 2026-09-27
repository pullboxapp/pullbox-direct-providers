from __future__ import annotations

import asyncio

import httpx
import pytest
from pullbox_provider_contract.models import ProviderStatus, ResolverProfile
from pullbox_provider_libgen import service as service_module
from pullbox_provider_libgen.app import create_app
from pullbox_provider_libgen.service import KNOWN_SOURCE_URLS, LibGenProviderService

from tests.conftest import TEST_TOKEN


class _HealthSession:
    def __init__(
        self,
        *,
        slow_fetch: bool = False,
        slow_close: bool = False,
        close_release: asyncio.Event | None = None,
        suppress_close_cancellation: bool = False,
    ) -> None:
        self.slow_fetch = slow_fetch
        self.slow_close = slow_close
        self.close_release = close_release
        self.suppress_close_cancellation = suppress_close_cancellation
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.closed_event = asyncio.Event()
        self.fetch_count = 0
        self.fetch_cancelled = False
        self.close_called = False
        self.close_cancelled = False
        self.closed = False

    async def fetch_text(self, _url: str, *, max_bytes: int = 0) -> str:
        self.fetch_count += 1
        self.started.set()
        if self.slow_fetch:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.fetch_cancelled = True
                raise
        return "source is reachable"

    async def resolve_redirect(self, _url: str) -> str:
        raise AssertionError("Health checks must not resolve downloads")

    async def aclose(self) -> None:
        self.close_called = True
        self.close_started.set()
        if self.slow_close:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                if self.suppress_close_cancellation:
                    assert self.close_release is not None
                    await self.close_release.wait()
                    self.closed = True
                    self.closed_event.set()
                    return
                raise
        if self.close_release is not None:
            try:
                await self.close_release.wait()
            except asyncio.CancelledError:
                self.close_cancelled = True
                raise
        self.closed = True
        self.closed_event.set()


@pytest.fixture
def short_health_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "_SOURCE_HEALTH_TIMEOUT_SECONDS", 0.05, raising=False)
    monkeypatch.setattr(service_module, "_SOURCE_HEALTH_CLOSE_TIMEOUT_SECONDS", 0.05, raising=False)


def _service(sessions: dict[str, _HealthSession]) -> LibGenProviderService:
    def factory(origin: str, profile: ResolverProfile | None) -> _HealthSession:
        assert profile is None
        return sessions[origin]

    return LibGenProviderService(session_factory=factory)


@pytest.mark.parametrize("slow_origins", [(KNOWN_SOURCE_URLS[0],), KNOWN_SOURCE_URLS])
async def test_source_health_bounds_slow_mirrors_and_preserves_fast_results(
    short_health_budget: None, slow_origins: tuple[str, ...]
) -> None:
    sessions = {
        origin: _HealthSession(slow_fetch=origin in slow_origins) for origin in KNOWN_SOURCE_URLS
    }
    try:
        async with asyncio.timeout(1):
            health = await _service(sessions).source_health()
    except TimeoutError:
        pytest.fail("Source health exceeded its budget while waiting for an upstream mirror")

    assert health == {
        origin.removeprefix("https://"): (
            ProviderStatus.UNAVAILABLE if origin in slow_origins else ProviderStatus.HEALTHY
        )
        for origin in KNOWN_SOURCE_URLS
    }
    assert all(session.fetch_count == 1 and session.closed for session in sessions.values())
    assert all(sessions[origin].fetch_cancelled for origin in slow_origins)


async def test_source_health_bounds_session_cleanup(short_health_budget: None) -> None:
    sessions = {origin: _HealthSession(slow_close=True) for origin in KNOWN_SOURCE_URLS}
    try:
        async with asyncio.timeout(1):
            health = await _service(sessions).source_health()
    except TimeoutError:
        pytest.fail("Source health exceeded its budget while closing an upstream session")

    assert set(health.values()) == {ProviderStatus.UNAVAILABLE}
    assert all(session.close_called and session.close_cancelled for session in sessions.values())


async def test_source_health_does_not_wait_for_cleanup_that_suppresses_cancellation(
    short_health_budget: None,
) -> None:
    close_release = asyncio.Event()
    sessions = {
        origin: _HealthSession(
            slow_close=True,
            close_release=close_release,
            suppress_close_cancellation=True,
        )
        for origin in KNOWN_SOURCE_URLS
    }
    task = asyncio.create_task(_service(sessions).source_health())
    try:
        await asyncio.wait_for(
            asyncio.gather(*(session.close_started.wait() for session in sessions.values())),
            timeout=1,
        )
        await asyncio.sleep(0.15)
        assert task.done(), "Source health waited indefinitely for cancellation-resistant cleanup"
    finally:
        close_release.set()
        await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(
            asyncio.gather(*(session.closed_event.wait() for session in sessions.values())),
            timeout=1,
        )

    assert all(session.close_cancelled and session.closed for session in sessions.values())


async def test_cancelling_health_cleans_up_all_probes(short_health_budget: None) -> None:
    sessions = {origin: _HealthSession(slow_fetch=True) for origin in KNOWN_SOURCE_URLS}
    task = asyncio.create_task(_service(sessions).source_health())
    try:
        await asyncio.wait_for(
            asyncio.gather(*(session.started.wait() for session in sessions.values())), timeout=1
        )
    except TimeoutError:
        pytest.fail("A slow first mirror prevented the remaining health probes from starting")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert all(session.fetch_cancelled and session.closed for session in sessions.values())


async def test_cancelling_health_during_cleanup_allows_bounded_close_to_finish(
    short_health_budget: None,
) -> None:
    close_release = asyncio.Event()
    sessions = {origin: _HealthSession(close_release=close_release) for origin in KNOWN_SOURCE_URLS}
    task = asyncio.create_task(_service(sessions).source_health())
    await asyncio.wait_for(
        asyncio.gather(*(session.close_started.wait() for session in sessions.values())), timeout=1
    )

    task.cancel()
    await asyncio.sleep(0.01)
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(session.closed and not session.close_cancelled for session in sessions.values())


async def test_health_api_reports_healthy_process_when_all_mirrors_time_out(
    short_health_budget: None,
) -> None:
    sessions = {origin: _HealthSession(slow_fetch=True) for origin in KNOWN_SOURCE_URLS}
    app = create_app(bearer_token=TEST_TOKEN, service=_service(sessions))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://provider.test"
    ) as client:
        try:
            async with asyncio.timeout(1):
                response = await client.get(
                    "/v1/health", headers={"Authorization": f"Bearer {TEST_TOKEN}"}
                )
        except TimeoutError:
            pytest.fail("Health API did not respond when upstream mirrors were unavailable")

    assert response.status_code == 200
    payload = response.json()
    assert payload["process_status"] == "healthy"
    assert payload["source_status"] == "unavailable"
    assert payload["diagnostics"] == {
        "source": "unavailable",
        **{f"source.{origin.removeprefix('https://')}": "unavailable" for origin in sessions},
    }
    assert all(session.closed for session in sessions.values())
