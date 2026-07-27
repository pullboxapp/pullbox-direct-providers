from __future__ import annotations

from pathlib import Path

import yaml

SPEC_PATH = Path(__file__).parents[2] / "spec" / "direct-download-provider-v1.openapi.yaml"


def test_canonical_openapi_declares_only_the_four_provider_operations() -> None:
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))

    assert document["openapi"].startswith("3.1.")
    assert document["info"]["version"] == "1.0.0"
    assert document["info"]["x-pullbox-protocol-version"] == ("direct-download-provider/v1")
    assert set(document["paths"]) == {
        "/v1/manifest",
        "/v1/health",
        "/v1/search",
        "/v1/resolve",
    }
    assert document["components"]["securitySchemes"]["providerBearer"] == {
        "type": "http",
        "scheme": "bearer",
    }


def test_every_operation_requires_bearer_auth_and_protocol_responses() -> None:
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))

    for path_item in document["paths"].values():
        for operation in path_item.values():
            assert operation["security"] == [{"providerBearer": []}]
            assert "401" in operation["responses"]
            assert "x-pullbox-protocol-version" in operation


def test_search_and_resolve_are_bounded_and_have_no_callback_contract() -> None:
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    assert schemas["SearchRequest"]["properties"]["limit"]["maximum"] == 100
    assert "deadline" in schemas["SearchRequest"]["required"]
    assert "deadline" in schemas["ResolveRequest"]["required"]
    assert "callback_url" not in str(document)
    assert "destination_path" not in str(document)
