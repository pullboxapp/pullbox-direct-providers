from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pullbox_provider_contract.models import (
    PROTOCOL_VERSION,
    ResolverProfile,
    SearchIntent,
    SearchRequest,
)
from pydantic import ValidationError

from tests.conftest import search_payload


def test_contract_accepts_additive_unknown_optional_fields() -> None:
    request = SearchRequest.model_validate(
        search_payload(future_optional_field={"provider_extension": True})
    )

    assert request.protocol_version == "direct-download-provider/v1"


def test_search_intent_preserves_collection_title_and_distinct_years() -> None:
    intent = SearchIntent(
        series_title="Immortal Thor",
        normalized_title="immortal thor",
        issue_number="3",
        issue_type="volume",
        volume="3",
        issue_title="Vol. 3: The End of All Songs",
        series_year=2024,
        release_year=2025,
        year=2025,
    )

    assert intent.issue_title == "Vol. 3: The End of All Songs"
    assert intent.series_year == 2024
    assert intent.release_year == 2025
    assert intent.year == 2025


def test_contract_rejects_invalid_request_id() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(search_payload(request_id="not-a-uuid"))


def test_contract_parses_timezone_aware_deadline() -> None:
    deadline = datetime.now(UTC) + timedelta(minutes=1)
    request = SearchRequest.model_validate(search_payload(deadline=deadline.isoformat()))

    assert request.deadline.tzinfo is not None


def test_contract_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError):
        SearchRequest.model_validate(search_payload(deadline="2026-07-27T12:00:00"))


def test_request_and_resolver_profile_repr_redact_all_secret_material() -> None:
    profile = ResolverProfile(
        endpoint="http://resolver:8191",
        timeout_seconds=60,
        max_concurrency=1,
        declared_domains=["source.example"],
        authentication_headers={"Authorization": "resolver-secret"},
    )
    request = SearchRequest(
        protocol_version=PROTOCOL_VERSION,
        request_id=uuid4(),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        intent=SearchIntent(series_title="Example", normalized_title="example"),
        provider_config={"account": "provider-secret"},
        source_credentials={"member_key": "source-secret"},
        resolver_profile=profile,
    )

    representation = repr(request)
    assert "resolver-secret" not in repr(profile)
    assert "resolver-secret" not in representation
    assert "provider-secret" not in representation
    assert "source-secret" not in representation
