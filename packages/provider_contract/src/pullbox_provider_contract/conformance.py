"""Reusable black-box conformance runner for provider implementations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from pydantic import ValidationError

from pullbox_provider_contract.compatibility import (
    IncompatibleProtocolError,
    negotiate_protocol_version,
)
from pullbox_provider_contract.configuration import (
    ConfigurationSchemaError,
    validate_configuration_schema,
)
from pullbox_provider_contract.models import (
    PROTOCOL_VERSION,
    HealthResponse,
    ManifestResponse,
    ResolveRequest,
    ResolveResponse,
    SearchIntent,
    SearchRequest,
    SearchResponse,
)

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ConformanceError(RuntimeError):
    """A redacted provider conformance failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Safe summary proving the four-operation provider boundary."""

    provider_id: str
    negotiated_protocol: str
    operations: tuple[str, ...]
    candidate_count: int
    artifact_count: int


async def run_provider_conformance(
    *,
    client: httpx.AsyncClient,
    bearer_token: str,
) -> ConformanceReport:
    """Exercise manifest, health, search, and resolve against one provider."""
    headers = {"Authorization": f"Bearer {bearer_token}"}
    manifest_payload = await _request_json(client, "GET", "/v1/manifest", headers=headers)
    manifest = _validate_response(ManifestResponse, manifest_payload, "manifest")
    try:
        negotiated = negotiate_protocol_version(
            pullbox_versions=[PROTOCOL_VERSION],
            provider_versions=manifest.supported_protocol_versions,
        )
        validate_configuration_schema(manifest.configuration_schema)
    except (IncompatibleProtocolError, ConfigurationSchemaError) as exc:
        raise ConformanceError("incompatible_manifest", str(exc)) from exc

    health_payload = await _request_json(client, "GET", "/v1/health", headers=headers)
    health = _validate_response(HealthResponse, health_payload, "health")
    if health.protocol_version != negotiated:
        raise ConformanceError("incompatible_protocol", "Health response protocol is incompatible.")

    deadline = datetime.now(UTC) + timedelta(seconds=30)
    search_request = SearchRequest(
        protocol_version=negotiated,
        request_id=uuid4(),
        deadline=deadline,
        intent=SearchIntent(
            series_title="Synthetic Adventures",
            normalized_title="synthetic adventures",
            issue_number="1",
            year=2026,
            preferred_formats=["cbz"],
        ),
        limit=5,
    )
    search_payload = await _request_json(
        client,
        "POST",
        "/v1/search",
        headers=headers,
        json_body=search_request.model_dump(mode="json"),
    )
    search = _validate_response(SearchResponse, search_payload, "search")
    if search.protocol_version != negotiated or search.request_id != search_request.request_id:
        raise ConformanceError(
            "invalid_response_identity", "Search response identity did not match."
        )
    candidate = next((item for item in search.candidates if item.can_resolve), None)
    if candidate is None:
        raise ConformanceError(
            "no_resolvable_candidate", "Search returned no resolvable candidate."
        )

    resolve_request = ResolveRequest(
        protocol_version=negotiated,
        request_id=uuid4(),
        deadline=deadline,
        provider_candidate_id=candidate.provider_candidate_id,
    )
    resolve_payload = await _request_json(
        client,
        "POST",
        "/v1/resolve",
        headers=headers,
        json_body=resolve_request.model_dump(mode="json"),
    )
    resolve = _validate_response(ResolveResponse, resolve_payload, "resolve")
    if resolve.protocol_version != negotiated or resolve.request_id != resolve_request.request_id:
        raise ConformanceError(
            "invalid_response_identity", "Resolve response identity did not match."
        )
    if not resolve.artifacts:
        raise ConformanceError("no_artifacts", "Resolve returned no artifacts.")

    return ConformanceReport(
        provider_id=manifest.provider_id,
        negotiated_protocol=negotiated,
        operations=("manifest", "health", "search", "resolve"),
        candidate_count=len(search.candidates),
        artifact_count=len(resolve.artifacts),
    )


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any] | None = None,
) -> object:
    try:
        async with client.stream(method, path, headers=headers, json=json_body) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ConformanceError(
                        "response_too_large", "Provider response exceeded the limit."
                    )
                content.extend(chunk)
            status_code = response.status_code
    except httpx.HTTPError as exc:
        raise ConformanceError("provider_unavailable", "Provider request failed.") from exc
    if status_code >= 400:
        code = "provider_request_failed"
        try:
            payload = json.loads(content)
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
        except (ValueError, json.JSONDecodeError):
            pass
        raise ConformanceError(code, f"Provider returned HTTP {status_code}.")
    try:
        return json.loads(content)
    except ValueError as exc:
        raise ConformanceError("malformed_response", "Provider returned malformed JSON.") from exc


def _validate_response(model_type: type[Any], payload: object, operation: str) -> Any:
    try:
        return model_type.model_validate(payload)
    except ValidationError as exc:
        raise ConformanceError(
            "malformed_response",
            f"Provider returned an invalid {operation} response.",
        ) from exc
