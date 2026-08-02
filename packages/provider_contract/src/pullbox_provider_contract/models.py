"""Canonical Python DTOs for direct-download provider protocol v1."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pullbox_provider_contract.configuration import validate_configuration_schema

PROTOCOL_VERSION = "direct-download-provider/v1"
MAX_SEARCH_RESULTS = 100


class ContractModel(BaseModel):
    """Base DTO policy for additive fields within a compatible protocol major."""

    model_config = ConfigDict(extra="ignore")


class ProviderStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RATE_LIMITED = "rate_limited"
    CHALLENGE_REQUIRED = "challenge_required"
    AUTHENTICATION_REQUIRED = "authentication_required"
    INCOMPATIBLE = "incompatible"
    UNAVAILABLE = "unavailable"


class ArtifactRoute(StrEnum):
    DIRECT_ARTIFACT = "direct_artifact"
    TORRENT_FILE = "torrent_file"
    MAGNET = "magnet"


class ProviderCapabilities(ContractModel):
    search: bool
    resolve: bool
    browser_challenge: bool = False
    health: bool = True
    quota: bool = False
    configuration_schema: bool = False


class ManifestResponse(ContractModel):
    protocol_version: str = PROTOCOL_VERSION
    provider_id: str
    display_name: str
    description: str
    provider_version: str
    supported_protocol_versions: list[str]
    publisher: str
    license: str
    homepage_url: str | None = None
    documentation_url: str | None = None
    support_url: str | None = None
    source_domains: list[str]
    artifact_host_patterns: list[str] = Field(default_factory=list)
    capabilities: ProviderCapabilities
    configuration_schema: dict[str, Any]
    min_pullbox_version: str | None = None
    max_pullbox_version: str | None = None
    build: dict[str, str] = Field(default_factory=dict)

    @field_validator("configuration_schema")
    @classmethod
    def configuration_must_use_native_controls(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_configuration_schema(value)
        return value


DiagnosticScalar = str | int | float | bool | None


class HealthResponse(ContractModel):
    protocol_version: str = PROTOCOL_VERSION
    process_status: ProviderStatus
    source_status: ProviderStatus
    message: str
    retry_after_seconds: int | None = Field(default=None, ge=0)
    diagnostics: dict[str, DiagnosticScalar] = Field(default_factory=dict)


class ResolverProfile(ContractModel):
    endpoint: str
    mode: Literal["flaresolverr_v1", "trawl_scrape"] = "flaresolverr_v1"
    timeout_seconds: float = Field(gt=0, le=300)
    max_concurrency: int = Field(ge=1, le=4)
    declared_domains: list[str]
    authentication_headers: dict[str, str] = Field(default_factory=dict, repr=False)


class SearchIntent(ContractModel):
    series_title: str = Field(min_length=1, max_length=500)
    normalized_title: str = Field(min_length=1, max_length=500)
    alternate_titles: list[str] = Field(default_factory=list, max_length=25)
    issue_number: str | None = Field(default=None, max_length=50)
    issue_type: str | None = Field(default=None, max_length=40)
    volume: str | None = Field(default=None, max_length=100)
    issue_title: str | None = Field(default=None, max_length=500)
    series_year: int | None = Field(default=None, ge=1800, le=2200)
    release_year: int | None = Field(default=None, ge=1800, le=2200)
    year: int | None = Field(default=None, ge=1800, le=2200)
    publisher: str | None = Field(default=None, max_length=300)
    language: str | None = Field(default=None, max_length=20)
    preferred_formats: list[str] = Field(default_factory=list, max_length=20)
    quality_preferences: list[str] = Field(default_factory=list, max_length=20)


class DeadlineRequest(ContractModel):
    protocol_version: str
    request_id: UUID
    deadline: datetime
    provider_config: dict[str, Any] = Field(default_factory=dict, repr=False)
    source_credentials: dict[str, str] = Field(default_factory=dict, repr=False)
    resolver_profile: ResolverProfile | None = Field(default=None, repr=False)

    @field_validator("deadline")
    @classmethod
    def deadline_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("deadline must include a timezone")
        return value


class SearchRequest(DeadlineRequest):
    intent: SearchIntent
    limit: int = Field(default=20, ge=1, le=MAX_SEARCH_RESULTS)


class ParsedCandidate(ContractModel):
    series_title: str
    issue_numbers: list[str] = Field(default_factory=list)
    volume: str | None = None
    year: int | None = None
    publisher: str | None = None
    language: str | None = None
    edition: str | None = None
    format: str | None = None
    release_group: str | None = None
    quality: str | None = None


class Candidate(ContractModel):
    provider_candidate_id: str = Field(min_length=1, max_length=500)
    source_reference: str = Field(min_length=1, max_length=2000)
    display_title: str = Field(min_length=1, max_length=1000)
    raw_title: str = Field(min_length=1, max_length=2000)
    parsed: ParsedCandidate
    provider_confidence: float = Field(ge=0, le=1)
    provenance: dict[str, DiagnosticScalar] = Field(default_factory=dict)
    can_resolve: bool = True
    expires_at: datetime | None = None


class SearchResponse(ContractModel):
    protocol_version: str = PROTOCOL_VERSION
    request_id: UUID
    candidates: list[Candidate]
    truncated: bool = False


class ResolveRequest(DeadlineRequest):
    provider_candidate_id: str = Field(min_length=1, max_length=500)


class ArtifactCoverage(ContractModel):
    issue_numbers: list[str] = Field(default_factory=list)
    issue_ids: list[str] = Field(default_factory=list)
    volume: str | None = None
    description: str | None = None


class Mirror(ContractModel):
    mirror_id: str
    host_kind: str
    share_url: str | None = Field(default=None, repr=False)
    final_url: str | None = Field(default=None, repr=False)
    source_headers: dict[str, str] = Field(default_factory=dict, repr=False)
    size_bytes: int | None = Field(default=None, ge=0)
    checksum: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    expires_at: datetime | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_one_location(self) -> Self:
        if not self.share_url and not self.final_url:
            raise ValueError("mirror requires share_url or final_url")
        return self


class Artifact(ContractModel):
    artifact_id: str
    coverage: ArtifactCoverage
    route: ArtifactRoute
    format: str | None = None
    quality: str | None = None
    language: str | None = None
    edition: str | None = None
    release_group: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    size_is_estimate: bool = False
    mirrors: list[Mirror] = Field(default_factory=list, max_length=50)
    magnet_uri: str | None = None
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_route_payload(self) -> Self:
        if self.route == ArtifactRoute.MAGNET:
            if not self.magnet_uri:
                raise ValueError("magnet route requires magnet_uri")
        elif not self.mirrors:
            raise ValueError("non-magnet route requires at least one mirror")
        return self


class QuotaStatus(ContractModel):
    """Optional source capacity that intentionally excludes account history."""

    remaining: int | None = Field(default=None, ge=0, le=1_000_000)
    limit: int | None = Field(default=None, ge=0, le=1_000_000)
    window_seconds: int | None = Field(default=None, ge=1, le=31_536_000)
    reset_at: datetime | None = None


class ResolveResponse(ContractModel):
    protocol_version: str = PROTOCOL_VERSION
    request_id: UUID
    artifacts: list[Artifact] = Field(max_length=100)
    quota: QuotaStatus | None = None
