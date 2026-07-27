"""Shared contracts for Pullbox direct-download providers."""

from pullbox_provider_contract.compatibility import negotiate_protocol_version
from pullbox_provider_contract.configuration import validate_configuration_schema
from pullbox_provider_contract.models import (
    PROTOCOL_VERSION,
    Artifact,
    ArtifactCoverage,
    Candidate,
    HealthResponse,
    ManifestResponse,
    Mirror,
    ResolveRequest,
    ResolveResponse,
    SearchIntent,
    SearchRequest,
    SearchResponse,
)

__all__ = [
    "PROTOCOL_VERSION",
    "Artifact",
    "ArtifactCoverage",
    "Candidate",
    "HealthResponse",
    "ManifestResponse",
    "Mirror",
    "ResolveRequest",
    "ResolveResponse",
    "SearchIntent",
    "SearchRequest",
    "SearchResponse",
    "negotiate_protocol_version",
    "validate_configuration_schema",
]
