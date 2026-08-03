from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pullbox_provider_contract.models import Artifact, Candidate, ParsedCandidate
from pullbox_provider_contract.resolver import ProviderResolverError
from pullbox_provider_contract.source_http import BrowserChallengeRequiredError
from pullbox_provider_getcomics.app import create_app

from tests.conftest import TEST_TOKEN, resolve_payload, search_payload


class _GetComicsService:
    def __init__(
        self,
        *,
        available: bool = True,
        error: Exception | None = None,
        candidates: list[Candidate] | None = None,
    ) -> None:
        self.available = available
        self.error = error
        self.candidates = candidates or []
        self.search_kwargs: dict[str, object] | None = None
        self.resolve_kwargs: dict[str, object] | None = None

    async def source_available(self) -> bool:
        return self.available

    async def search(self, _intent: object, **kwargs: object) -> list[Candidate]:
        self.search_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.candidates[: int(kwargs["limit"])]

    async def resolve(self, _candidate_id: str, **kwargs: object) -> list[Artifact]:
        self.resolve_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return []


def _app(service: _GetComicsService | None = None) -> FastAPI:
    return create_app(bearer_token=TEST_TOKEN, service=service or _GetComicsService())


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    authenticated: bool = True,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"} if authenticated else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://provider.test",
    ) as client:
        return await client.request(method, path, headers=headers, json=json)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/v1/manifest", None),
        ("GET", "/v1/health", None),
        ("POST", "/v1/search", search_payload()),
        ("POST", "/v1/resolve", resolve_payload()),
    ],
)
async def test_getcomics_operations_require_provider_bearer_token(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    response = await _request(_app(), method, path, json=payload, authenticated=False)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_authentication_failed"


async def test_getcomics_manifest_declares_narrow_stateless_capabilities() -> None:
    response = await _request(_app(), "GET", "/v1/manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "pullbox.getcomics"
    assert payload["source_domains"] == ["getcomics.org"]
    assert payload["artifact_host_patterns"] == [
        "generic_https",
        "pixeldrain",
        "mega",
        "rootz",
        "mediafire",
        "terabox",
        "datanodes",
    ]
    assert payload["capabilities"]["browser_challenge"] is True
    assert payload["configuration_schema"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


async def test_getcomics_search_and_resolve_receive_only_request_scoped_resolver() -> None:
    service = _GetComicsService()
    app = _app(service)
    resolver = {
        "endpoint": "http://resolver:8191",
        "mode": "flaresolverr_v1",
        "timeout_seconds": 30,
        "max_concurrency": 1,
        "declared_domains": ["getcomics.org"],
        "authentication_headers": {},
    }

    search = await _request(
        app,
        "POST",
        "/v1/search",
        json=search_payload(resolver_profile=resolver),
    )
    resolve = await _request(
        app,
        "POST",
        "/v1/resolve",
        json=resolve_payload(resolver_profile=resolver),
    )

    assert search.status_code == 200
    assert resolve.status_code == 200
    assert service.search_kwargs is not None
    assert service.resolve_kwargs is not None
    assert service.search_kwargs["resolver_profile"].endpoint == "http://resolver:8191"
    assert service.resolve_kwargs["resolver_profile"].declared_domains == ["getcomics.org"]


async def test_getcomics_health_and_source_errors_are_explicit_and_safe() -> None:
    degraded = await _request(_app(_GetComicsService(available=False)), "GET", "/v1/health")
    failed = await _request(
        _app(_GetComicsService(error=RuntimeError("secret upstream detail"))),
        "POST",
        "/v1/search",
        json=search_payload(),
    )

    assert degraded.status_code == 200
    assert degraded.json()["source_status"] == "degraded"
    assert failed.status_code == 503
    assert failed.json()["error"] == {
        "code": "source_unavailable",
        "message": "GetComics is temporarily unavailable.",
    }
    assert "secret upstream detail" not in failed.text


async def test_getcomics_preserves_browser_challenge_classification() -> None:
    response = await _request(
        _app(_GetComicsService(error=BrowserChallengeRequiredError())),
        "POST",
        "/v1/search",
        json=search_payload(),
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "browser_challenge_required",
        "message": "GetComics requires browser challenge handling.",
    }

    resolver_failure = await _request(
        _app(
            _GetComicsService(
                error=ProviderResolverError(
                    "resolver_timed_out",
                    "secret resolver detail",
                    retryable=True,
                )
            )
        ),
        "POST",
        "/v1/search",
        json=search_payload(),
    )
    assert resolver_failure.status_code == 503
    assert resolver_failure.json()["error"] == {
        "code": "resolver_timed_out",
        "message": "GetComics browser resolver attempt failed.",
    }
    assert "secret resolver detail" not in resolver_failure.text


def _candidate(number: int) -> Candidate:
    title = f"Example {number}"
    return Candidate(
        provider_candidate_id=f"candidate-{number}",
        source_reference=f"https://getcomics.org/example-{number}/",
        display_title=title,
        raw_title=title,
        parsed=ParsedCandidate(series_title="Example", issue_numbers=[str(number)]),
        provider_confidence=1,
    )


@pytest.mark.parametrize(
    ("candidate_count", "expected_truncated"),
    [(2, False), (3, True)],
)
async def test_getcomics_search_reports_truncation_only_when_results_were_dropped(
    candidate_count: int,
    expected_truncated: bool,
) -> None:
    service = _GetComicsService(candidates=[_candidate(index) for index in range(candidate_count)])

    response = await _request(
        _app(service),
        "POST",
        "/v1/search",
        json=search_payload(limit=2),
    )

    assert response.status_code == 200
    assert len(response.json()["candidates"]) == 2
    assert response.json()["truncated"] is expected_truncated
    assert service.search_kwargs is not None
    assert service.search_kwargs["limit"] == 3
