from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pullbox_provider_annas_archive.app import create_app
from pullbox_provider_contract.models import Artifact, Candidate

from tests.conftest import TEST_TOKEN, resolve_payload, search_payload

MEMBER_SECRET = "member-secret-never-rendered"


class _AnnaService:
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


def _app(service: _AnnaService | None = None) -> FastAPI:
    return create_app(bearer_token=TEST_TOKEN, service=service or _AnnaService())


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
async def test_anna_operations_require_provider_bearer_token(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    response = await _request(_app(), method, path, json=payload, authenticated=False)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_authentication_failed"


async def test_anna_manifest_marks_member_key_secret_and_official_domain_fixed() -> None:
    response = await _request(_app(), "GET", "/v1/manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "pullbox.annas_archive"
    assert payload["source_domains"] == ["annas-archive.gd"]
    schema = payload["configuration_schema"]
    assert schema["properties"]["domain"]["default"] == "https://annas-archive.gd"
    assert schema["properties"]["member_secret_key"]["x-pullbox-secret"] is True
    assert schema["required"] == ["member_secret_key"]


async def test_anna_search_and_resolve_forward_secrets_only_to_active_operation() -> None:
    service = _AnnaService()
    app = _app(service)
    search = await _request(
        app,
        "POST",
        "/v1/search",
        json=search_payload(
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={"member_secret_key": MEMBER_SECRET},
        ),
    )
    resolve = await _request(
        app,
        "POST",
        "/v1/resolve",
        json=resolve_payload(
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={"member_secret_key": MEMBER_SECRET},
        ),
    )

    assert search.status_code == 200
    assert resolve.status_code == 200
    assert service.search_kwargs is not None
    assert service.resolve_kwargs is not None
    assert service.search_kwargs["provider_config"] == {"domain": "https://annas-archive.gd"}
    assert "source_credentials" not in service.search_kwargs
    assert service.resolve_kwargs["source_credentials"] == {"member_secret_key": MEMBER_SECRET}
    assert MEMBER_SECRET not in search.text
    assert MEMBER_SECRET not in resolve.text


async def test_anna_health_and_unexpected_source_errors_are_explicit_and_safe() -> None:
    degraded = await _request(_app(_AnnaService(available=False)), "GET", "/v1/health")
    failed = await _request(
        _app(_AnnaService(error=RuntimeError(MEMBER_SECRET))),
        "POST",
        "/v1/search",
        json=search_payload(),
    )

    assert degraded.status_code == 200
    assert degraded.json()["source_status"] == "degraded"
    assert failed.status_code == 503
    assert failed.json()["error"] == {
        "code": "source_unavailable",
        "message": "Anna's Archive is temporarily unavailable.",
    }
    assert MEMBER_SECRET not in failed.text
