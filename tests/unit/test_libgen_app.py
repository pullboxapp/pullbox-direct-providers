from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pullbox_provider_contract.models import Artifact, Candidate
from pullbox_provider_libgen.app import create_app
from pullbox_provider_libgen.metadata import LibGenMetadataError
from pullbox_provider_libgen.parser import LibGenLayoutError
from pullbox_provider_libgen.service import LibGenSourceOriginError
from pullbox_provider_libgen.transport import LibGenSourceError

from tests.conftest import TEST_TOKEN, resolve_payload, search_payload


class _LibGenService:
    def __init__(
        self,
        *,
        available: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.available = available
        self.error = error
        self.search_kwargs: dict[str, object] | None = None
        self.resolve_kwargs: dict[str, object] | None = None

    async def source_available(self, **_kwargs: object) -> bool:
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


def _app(service: _LibGenService | None = None) -> FastAPI:
    return create_app(bearer_token=TEST_TOKEN, service=service or _LibGenService())


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
async def test_libgen_operations_require_provider_bearer_token(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    response = await _request(_app(), method, path, json=payload, authenticated=False)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_authentication_failed"


async def test_libgen_manifest_declares_open_source_origin_configuration() -> None:
    response = await _request(_app(), "GET", "/v1/manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "pullbox.libgen"
    assert payload["source_domains"] == [
        "libgen.gl",
        "libgen.li",
        "libgen.vg",
        "libgen.la",
        "libgen.bz",
    ]
    assert payload["artifact_host_patterns"] == ["generic_https"]
    assert payload["capabilities"] == {
        "search": True,
        "resolve": True,
        "browser_challenge": True,
        "health": True,
        "quota": False,
        "configuration_schema": True,
    }
    schema = payload["configuration_schema"]
    assert schema["required"] == []
    assert schema["additionalProperties"] is False
    assert tuple(schema["properties"]) == ("source_url",)
    source_url = schema["properties"]["source_url"]
    assert source_url["default"] == "https://libgen.gl"
    assert source_url["format"] == "uri"
    assert source_url["x-pullbox-source-origin"] is True
    assert source_url["x-pullbox-suggestions"] == [
        "https://libgen.gl",
        "https://libgen.li",
        "https://libgen.vg",
        "https://libgen.la",
        "https://libgen.bz",
    ]
    assert "enum" not in source_url


async def test_libgen_search_and_resolve_forward_only_request_scoped_inputs() -> None:
    service = _LibGenService()
    app = _app(service)
    resolver = {
        "endpoint": "http://resolver:8191",
        "mode": "trawl_scrape",
        "timeout_seconds": 30,
        "max_concurrency": 1,
        "declared_domains": ["libgen.gl"],
        "authentication_headers": {},
    }
    provider_config = {"source_url": "https://custom-libgen.example"}

    search = await _request(
        app,
        "POST",
        "/v1/search",
        json=search_payload(provider_config=provider_config, resolver_profile=resolver),
    )
    resolve = await _request(
        app,
        "POST",
        "/v1/resolve",
        json=resolve_payload(provider_config=provider_config, resolver_profile=resolver),
    )

    assert search.status_code == 200
    assert resolve.status_code == 200
    assert service.search_kwargs is not None
    assert service.resolve_kwargs is not None
    assert service.search_kwargs["provider_config"] == provider_config
    assert service.search_kwargs["limit"] == 21
    assert service.resolve_kwargs["provider_config"] == provider_config
    assert service.search_kwargs["resolver_profile"].endpoint == "http://resolver:8191"
    assert service.resolve_kwargs["resolver_profile"].declared_domains == ["libgen.gl"]


@pytest.mark.parametrize(
    ("path", "error", "status", "code"),
    [
        (
            "/v1/search",
            LibGenSourceOriginError("unsafe"),
            422,
            "provider_configuration_invalid",
        ),
        (
            "/v1/search",
            LibGenLayoutError("changed"),
            503,
            "source_contract_changed",
        ),
        (
            "/v1/resolve",
            LibGenMetadataError("conflict"),
            422,
            "candidate_invalid",
        ),
        (
            "/v1/resolve",
            ValueError("candidate"),
            422,
            "candidate_invalid",
        ),
        (
            "/v1/search",
            LibGenSourceError("source_rate_limited", "limited"),
            429,
            "source_rate_limited",
        ),
        (
            "/v1/resolve",
            LibGenSourceError("artifact_unavailable", "missing"),
            503,
            "artifact_unavailable",
        ),
    ],
)
async def test_libgen_errors_map_to_actionable_protocol_codes(
    path: str,
    error: Exception,
    status: int,
    code: str,
) -> None:
    payload = search_payload() if path.endswith("search") else resolve_payload()

    response = await _request(_app(_LibGenService(error=error)), "POST", path, json=payload)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
