"""FastAPI application for the optional GetComics provider."""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version

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
from pullbox_provider_contract.resolver import ProviderResolverError
from pullbox_provider_contract.source_http import BrowserChallengeRequiredError

from pullbox_provider_getcomics.service import GetComicsProviderService


def _provider_version() -> str:
    try:
        return version("pullbox-provider-getcomics")
    except PackageNotFoundError:
        return version("pullbox-direct-providers")


_VERSION = _provider_version()


def create_app(
    *,
    bearer_token: str | None = None,
    service: GetComicsProviderService | None = None,
) -> FastAPI:
    """Create the stateless GetComics provider application."""
    token = require_bearer_token(bearer_token or os.environ.get("PULLBOX_PROVIDER_TOKEN", ""))
    provider_service = service or GetComicsProviderService()
    authenticate = BearerAuthenticator(token)
    app = FastAPI(title="Pullbox GetComics Direct Provider", version=_VERSION)
    install_protocol_handlers(app)

    @app.get(
        "/v1/manifest",
        response_model=ManifestResponse,
        dependencies=[Depends(authenticate)],
    )
    async def manifest() -> ManifestResponse:
        return ManifestResponse(
            provider_id="pullbox.getcomics",
            display_name="GetComics",
            description="Optional GetComics discovery provider for Pullbox.",
            provider_version=_VERSION,
            supported_protocol_versions=[PROTOCOL_VERSION],
            publisher="Pullbox",
            license="GPL-3.0-or-later",
            homepage_url="https://github.com/pullboxapp/pullbox-direct-providers",
            documentation_url="https://github.com/pullboxapp/pullbox-direct-providers",
            support_url="https://github.com/pullboxapp/pullbox-direct-providers/issues",
            source_domains=["getcomics.org"],
            artifact_host_patterns=[
                "generic_https",
                "pixeldrain",
                "mega",
                "rootz",
                "mediafire",
                "terabox",
                "datanodes",
            ],
            capabilities=ProviderCapabilities(
                search=True,
                resolve=True,
                browser_challenge=True,
                configuration_schema=True,
            ),
            configuration_schema={
                "type": "object",
                "properties": {},
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
                "GetComics provider is ready."
                if available
                else "GetComics source reachability needs attention."
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
                    limit=payload.limit + 1,
                    resolver_profile=payload.resolver_profile,
                ),
                payload.deadline,
            )
        except BrowserChallengeRequiredError as exc:
            raise ProtocolError(
                503,
                exc.code,
                "GetComics requires browser challenge handling.",
            ) from exc
        except ProviderResolverError as exc:
            raise ProtocolError(
                503,
                exc.code,
                "GetComics browser resolver attempt failed.",
            ) from exc
        except RuntimeError as exc:
            raise ProtocolError(
                503,
                "source_unavailable",
                "GetComics is temporarily unavailable.",
            ) from exc
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
        validate_request(payload.protocol_version, payload.deadline)
        try:
            artifacts = await within_deadline(
                provider_service.resolve(
                    payload.provider_candidate_id,
                    resolver_profile=payload.resolver_profile,
                ),
                payload.deadline,
            )
        except BrowserChallengeRequiredError as exc:
            raise ProtocolError(
                503,
                exc.code,
                "GetComics requires browser challenge handling.",
            ) from exc
        except ProviderResolverError as exc:
            raise ProtocolError(
                503,
                exc.code,
                "GetComics browser resolver attempt failed.",
            ) from exc
        except RuntimeError as exc:
            raise ProtocolError(
                503,
                "source_unavailable",
                "GetComics is temporarily unavailable.",
            ) from exc
        return ResolveResponse(request_id=payload.request_id, artifacts=artifacts)

    return app
