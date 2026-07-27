from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pullbox_provider_contract.models import SearchRequest
from pydantic import ValidationError

from tests.conftest import search_payload


def test_contract_accepts_additive_unknown_optional_fields() -> None:
    request = SearchRequest.model_validate(
        search_payload(future_optional_field={"provider_extension": True})
    )

    assert request.protocol_version == "direct-download-provider/v1"


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
