"""FastAPI application for the synthetic provider."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pullbox_provider_contract.auth import bearer_token_matches
from pullbox_provider_contract.errors import ProtocolError
from pullbox_provider_contract.models import (
    PROTOCOL_VERSION,
    HealthResponse,
    ManifestResponse,
    ProviderCapabilities,
    ProviderStatus,
    ResolveRequest,
    ResolveResponse,
    SearchRequest,
    SearchResponse,
)

from pullbox_provider_synthetic.service import SyntheticProviderService

_SECURITY = HTTPBearer(auto_error=False)
_MIN_BEARER_TOKEN_LENGTH = 32


def _error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


class _BearerAuthenticator:
    def __init__(self, expected_token: str) -> None:
        self._expected_token = expected_token

    async def __call__(
        self,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_SECURITY)],
    ) -> None:
        presented = credentials.credentials if credentials else None
        if not bearer_token_matches(presented, self._expected_token):
            raise ProtocolError(
                401,
                "provider_authentication_failed",
                "Valid provider bearer authentication is required.",
            )


def _validate_request(request_protocol: str, deadline: datetime) -> None:
    if request_protocol != PROTOCOL_VERSION:
        raise ProtocolError(409, "incompatible_protocol", "Unsupported protocol version.")
    if deadline <= datetime.now(UTC):
        raise ProtocolError(408, "deadline_exceeded", "The provider request deadline has passed.")


def create_app(
    *,
    bearer_token: str | None = None,
    service: SyntheticProviderService | None = None,
) -> FastAPI:
    """Create an isolated synthetic provider application."""
    expected_token = bearer_token or os.environ.get("PULLBOX_PROVIDER_TOKEN", "")
    if len(expected_token) < _MIN_BEARER_TOKEN_LENGTH:
        raise ValueError("PULLBOX_PROVIDER_TOKEN must contain at least 32 characters")
    provider_service = service or SyntheticProviderService()
    authenticate = _BearerAuthenticator(expected_token)
    app = FastAPI(title="Pullbox Synthetic Direct Provider", version="0.1.0.dev0")

    @app.exception_handler(ProtocolError)
    async def handle_protocol_error(_request: Request, exc: ProtocolError) -> JSONResponse:
        return _error_response(exc.code, exc.message, exc.status_code)

    @app.get(
        "/v1/manifest",
        response_model=ManifestResponse,
        dependencies=[Depends(authenticate)],
    )
    async def manifest() -> ManifestResponse:
        return ManifestResponse(
            provider_id="pullbox.synthetic",
            display_name="Pullbox Synthetic Provider",
            description="Deterministic reference provider for protocol conformance.",
            provider_version="0.1.0.dev0",
            supported_protocol_versions=[PROTOCOL_VERSION],
            publisher="Pullbox",
            license="GPL-3.0-or-later",
            homepage_url="https://github.com/pullboxapp/pullbox-direct-providers",
            documentation_url="https://github.com/pullboxapp/pullbox-direct-providers",
            support_url="https://github.com/pullboxapp/pullbox-direct-providers/issues",
            source_domains=["provider.test"],
            capabilities=ProviderCapabilities(
                search=True,
                resolve=True,
                configuration_schema=True,
            ),
            configuration_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            build={"revision": "synthetic"},
        )

    @app.get(
        "/v1/health",
        response_model=HealthResponse,
        dependencies=[Depends(authenticate)],
    )
    async def health() -> HealthResponse:
        return HealthResponse(
            process_status=ProviderStatus.HEALTHY,
            source_status=ProviderStatus.HEALTHY,
            message="Synthetic provider is ready.",
            diagnostics={"fixture": "deterministic"},
        )

    @app.post(
        "/v1/search",
        response_model=SearchResponse,
        dependencies=[Depends(authenticate)],
    )
    async def search(payload: SearchRequest) -> SearchResponse:
        _validate_request(payload.protocol_version, payload.deadline)
        candidates = provider_service.search(payload.intent)
        return SearchResponse(
            request_id=payload.request_id,
            candidates=candidates[: payload.limit],
            truncated=len(candidates) > payload.limit,
        )

    @app.post(
        "/v1/resolve",
        response_model=ResolveResponse,
        dependencies=[Depends(authenticate)],
    )
    async def resolve(payload: ResolveRequest) -> ResolveResponse:
        _validate_request(payload.protocol_version, payload.deadline)
        artifacts = provider_service.resolve(payload.provider_candidate_id)
        if artifacts is None:
            raise ProtocolError(404, "candidate_not_found", "Provider candidate was not found.")
        return ResolveResponse(request_id=payload.request_id, artifacts=artifacts)

    return app
