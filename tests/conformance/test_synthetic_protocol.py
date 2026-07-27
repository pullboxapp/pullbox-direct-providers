from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tests.conftest import resolve_payload, search_payload


@pytest.mark.parametrize(
    "path,method,json_body",
    [
        ("/v1/manifest", "GET", None),
        ("/v1/health", "GET", None),
        ("/v1/search", "POST", search_payload()),
        ("/v1/resolve", "POST", resolve_payload()),
    ],
)
async def test_all_provider_operations_require_valid_bearer_auth(
    client: httpx.AsyncClient,
    path: str,
    method: str,
    json_body: dict[str, object] | None,
) -> None:
    missing = await client.request(method, path, json=json_body)
    invalid = await client.request(
        method,
        path,
        json=json_body,
        headers={"Authorization": "Bearer definitely-wrong"},
    )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.json()["error"]["code"] == "provider_authentication_failed"
    assert invalid.json()["error"]["code"] == "provider_authentication_failed"


def test_authentication_is_not_exposed_as_a_query_parameter(app) -> None:
    schema = app.openapi()

    for path in ("/v1/manifest", "/v1/health", "/v1/search", "/v1/resolve"):
        for operation in schema["paths"][path].values():
            query_names = {
                parameter["name"]
                for parameter in operation.get("parameters", [])
                if parameter["in"] == "query"
            }
            assert "credentials" not in query_names
            assert "_auth" not in query_names


async def test_manifest_exposes_stable_identity_and_capability_contract(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/v1/manifest", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["protocol_version"] == "direct-download-provider/v1"
    assert payload["provider_id"] == "pullbox.synthetic"
    assert payload["supported_protocol_versions"] == ["direct-download-provider/v1"]
    assert payload["source_domains"] == ["provider.test"]
    assert payload["capabilities"]["search"] is True
    assert payload["capabilities"]["resolve"] is True
    assert payload["capabilities"]["configuration_schema"] is True
    assert payload["configuration_schema"]["type"] == "object"


async def test_health_separates_process_and_source_status(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/v1/health", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "protocol_version": "direct-download-provider/v1",
        "process_status": "healthy",
        "source_status": "healthy",
        "message": "Synthetic provider is ready.",
        "retry_after_seconds": None,
        "diagnostics": {"fixture": "deterministic"},
    }


async def test_search_preserves_request_identity_and_enforces_result_limit(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/search",
        headers=auth_headers,
        json=search_payload(limit=1),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == "11111111-1111-4111-8111-111111111111"
    assert payload["protocol_version"] == "direct-download-provider/v1"
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["provider_candidate_id"] == "synthetic-issue-1"
    assert payload["candidates"][0]["provider_confidence"] == 1.0


async def test_resolve_returns_coverage_and_host_normalized_mirrors(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/resolve",
        headers=auth_headers,
        json=resolve_payload(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == "22222222-2222-4222-8222-222222222222"
    artifact = payload["artifacts"][0]
    assert artifact["coverage"]["issue_numbers"] == ["1"]
    assert artifact["route"] == "direct_artifact"
    assert artifact["mirrors"][0]["host_kind"] == "generic_https"
    assert "destination_path" not in str(payload)


async def test_unknown_candidate_fails_without_guessing(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/resolve",
        headers=auth_headers,
        json=resolve_payload(provider_candidate_id="missing"),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "candidate_not_found"


async def test_incompatible_protocol_fails_before_source_work(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/search",
        headers=auth_headers,
        json=search_payload(protocol_version="direct-download-provider/v2"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "incompatible_protocol"


async def test_expired_deadline_fails_before_source_work(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    response = await client.post(
        "/v1/search",
        headers=auth_headers,
        json=search_payload(deadline=expired),
    )

    assert response.status_code == 408
    assert response.json()["error"]["code"] == "deadline_exceeded"


@pytest.mark.parametrize("limit", [0, 101])
async def test_search_rejects_unbounded_result_limits(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    limit: int,
) -> None:
    response = await client.post(
        "/v1/search",
        headers=auth_headers,
        json=search_payload(limit=limit),
    )

    assert response.status_code == 422


async def test_malformed_search_payload_is_rejected(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.post(
        "/v1/search",
        headers=auth_headers,
        json={"protocol_version": "direct-download-provider/v1"},
    )

    assert response.status_code == 422
