from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pullbox_provider_contract.models import Artifact, Candidate
from pullbox_provider_getcomics.app import create_app

from tests.conftest import TEST_TOKEN, resolve_payload, search_payload


class _GetComicsService:
    def __init__(self, *, available: bool = True, error: Exception | None = None) -> None:
        self.available = available
        self.error = error
        self.search_kwargs: dict[str, object] | None = None
        self.resolve_kwargs: dict[str, object] | None = None

    async def source_available(self) -> bool:
        return self.available

    async def search(self, _intent: object, **kwargs: object) -> list[Candidate]:
        self.search_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return []

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
