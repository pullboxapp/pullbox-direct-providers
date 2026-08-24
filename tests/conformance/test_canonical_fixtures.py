from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pullbox_provider_contract.models import (
    HealthResponse,
    ManifestResponse,
    ResolveRequest,
    ResolveResponse,
    SearchRequest,
    SearchResponse,
)
from pydantic import BaseModel

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures" / "protocol-v1"


@pytest.mark.parametrize(
    ("fixture_name", "model"),
    [
        ("manifest-response.json", ManifestResponse),
        ("health-response.json", HealthResponse),
        ("search-request.json", SearchRequest),
        ("search-response.json", SearchResponse),
        ("resolve-request.json", ResolveRequest),
        ("resolve-response.json", ResolveResponse),
    ],
)
def test_canonical_fixture_round_trips_through_python_contract(
    fixture_name: str,
    model: type[BaseModel],
) -> None:
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / fixture_name).read_text(encoding="utf-8"))

    parsed = model.model_validate(payload)
    reparsed = model.model_validate_json(parsed.model_dump_json())

    assert reparsed == parsed


def test_canonical_fixtures_contain_no_live_hosts_or_credentials() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(FIXTURE_DIR.glob("*.json"))
    )

    assert "provider.test" in fixture_text
    assert "api_key" not in fixture_text.casefold()
    assert "authorization" not in fixture_text.casefold()
    assert "token=" not in fixture_text.casefold()


def test_canonical_fixtures_document_lg1_additive_contracts() -> None:
    manifest = json.loads((FIXTURE_DIR / "manifest-response.json").read_text(encoding="utf-8"))
    search = json.loads((FIXTURE_DIR / "search-response.json").read_text(encoding="utf-8"))

    source_url = manifest["configuration_schema"]["properties"]["source_url"]
    assert source_url["x-pullbox-suggestions"] == ["https://provider.test"]
    assert source_url["x-pullbox-source-origin"] is True
    assert search["candidates"][0]["content_fingerprint"] == (
        "md5:0123456789abcdef0123456789abcdef"
    )
