"""FastAPI application for the optional Anna's Archive provider."""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI
from pullbox_provider_contract.api import (
    BearerAuthenticator,
    install_protocol_handlers,
    require_bearer_token,
    validate_request,
    within_deadline,
)
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

from pullbox_provider_annas_archive.service import AnnasArchiveProviderService

_VERSION = "0.1.0.dev0"
_OFFICIAL_DOMAIN = "https://annas-archive.gd"


def create_app(
    *,
    bearer_token: str | None = None,
    service: AnnasArchiveProviderService | None = None,
) -> FastAPI:
    """Create the stateless Anna's Archive provider application."""
    token = require_bearer_token(bearer_token or os.environ.get("PULLBOX_PROVIDER_TOKEN", ""))
    provider_service = service or AnnasArchiveProviderService()
    authenticate = BearerAuthenticator(token)
    app = FastAPI(title="Pullbox Anna's Archive Direct Provider", version=_VERSION)
    install_protocol_handlers(app)

    @app.get(
        "/v1/manifest",
        response_model=ManifestResponse,
        dependencies=[Depends(authenticate)],
    )
    async def manifest() -> ManifestResponse:
        return ManifestResponse(
            provider_id="pullbox.annas_archive",
            display_name="Anna's Archive",
            description="Optional member fast-download discovery provider for Pullbox.",
            provider_version=_VERSION,
            supported_protocol_versions=[PROTOCOL_VERSION],
            publisher="Pullbox",
            license="GPL-3.0-or-later",
            homepage_url="https://github.com/pullboxapp/pullbox-direct-providers",
            documentation_url="https://github.com/pullboxapp/pullbox-direct-providers",
            support_url="https://github.com/pullboxapp/pullbox-direct-providers/issues",
            source_domains=["annas-archive.gd"],
            capabilities=ProviderCapabilities(
                search=True,
                resolve=True,
                browser_challenge=True,
                quota=True,
                configuration_schema=True,
            ),
            configuration_schema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "title": "Official domain",
                        "description": "Exact official Anna's Archive domain supported by Pullbox.",
                        "default": _OFFICIAL_DOMAIN,
                        "enum": [_OFFICIAL_DOMAIN],
                    },
                    "member_secret_key": {
                        "type": "string",
                        "title": "Member secret key",
                        "description": "Anna's Archive member fast-download secret key.",
                        "minLength": 1,
                        "maxLength": 4096,
                        "x-pullbox-secret": True,
                    },
                },
                "required": ["member_secret_key"],
                "additionalProperties": False,
            },
            build={"revision": os.environ.get("PULLBOX_PROVIDER_REVISION", "development")},
        )

    @app.get(
        "/v1/health",
        response_model=HealthResponse,
        dependencies=[Depends(authenticate)],
    )
    async def health() -> HealthResponse:
        available = await provider_service.source_available()
        return HealthResponse(
            process_status=ProviderStatus.HEALTHY,
            source_status=(ProviderStatus.HEALTHY if available else ProviderStatus.DEGRADED),
            message=(
                "Anna's Archive provider is ready."
                if available
                else "Anna's Archive source reachability needs attention."
            ),
            diagnostics={"source": "reachable" if available else "unreachable"},
        )

    @app.post(
        "/v1/search",
        response_model=SearchResponse,
        dependencies=[Depends(authenticate)],
    )
    async def search(payload: SearchRequest) -> SearchResponse:
        validate_request(payload.protocol_version, payload.deadline)
        try:
            candidates = await within_deadline(
                provider_service.search(
                    payload.intent,
                    provider_config=payload.provider_config,
                    limit=payload.limit,
                    resolver_profile=payload.resolver_profile,
                ),
                payload.deadline,
            )
        except RuntimeError as exc:
            raise ProtocolError(
                503,
                "source_unavailable",
                "Anna's Archive is temporarily unavailable.",
            ) from exc
        return SearchResponse(
            request_id=payload.request_id,
            candidates=candidates,
            truncated=len(candidates) >= payload.limit,
        )

    @app.post(
        "/v1/resolve",
        response_model=ResolveResponse,
        dependencies=[Depends(authenticate)],
    )
    async def resolve(payload: ResolveRequest) -> ResolveResponse:
        validate_request(payload.protocol_version, payload.deadline)
        try:
            artifacts = await within_deadline(
                provider_service.resolve(
                    payload.provider_candidate_id,
                    provider_config=payload.provider_config,
                    source_credentials=payload.source_credentials,
                ),
                payload.deadline,
            )
        except RuntimeError as exc:
            raise ProtocolError(
                503,
                "source_unavailable",
                "Anna's Archive is temporarily unavailable.",
            ) from exc
        return ResolveResponse(request_id=payload.request_id, artifacts=artifacts)

    return app
