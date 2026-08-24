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


def test_openapi_matches_runtime_resolver_modes_and_artifact_size_metadata() -> None:
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    assert schemas["ResolverProfile"]["properties"]["mode"]["enum"] == [
        "flaresolverr_v1",
        "trawl_scrape",
    ]
    assert schemas["Artifact"]["properties"]["size_is_estimate"] == {
        "type": "boolean",
        "default": False,
    }
    assert schemas["Candidate"]["properties"]["content_fingerprint"] == {
        "type": ["string", "null"],
        "pattern": "^md5:[0-9a-f]{32}$",
        "description": (
            "Optional stable provider-neutral identity for byte-identical content, using "
            "canonical lowercase MD5 hexadecimal. Changed bytes require a new value. Used "
            "only for deduplication and fallback grouping, never as a security checksum."
        ),
    }


def test_openapi_defines_the_native_provider_configuration_vocabulary() -> None:
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    schemas = document["components"]["schemas"]

    assert schemas["ManifestResponse"]["properties"]["configuration_schema"] == {
        "$ref": "#/components/schemas/ProviderConfigurationSchema"
    }
    configuration = schemas["ProviderConfigurationSchema"]
    assert configuration["additionalProperties"] is False
    assert configuration["properties"]["type"] == {"const": "object"}
    assert configuration["properties"]["additionalProperties"] == {"const": False}
    assert configuration["properties"]["properties"]["maxProperties"] == 50
    field = schemas["ProviderConfigurationField"]
    assert field["additionalProperties"] is False
    assert field["properties"]["enum"]["description"].startswith("Closed choices")
    assert field["properties"]["x-pullbox-suggestions"] == {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "uniqueItems": True,
        "description": "Editable HTTPS origin suggestions; values outside the list remain valid.",
        "items": {"type": "string", "format": "uri"},
    }
    assert field["properties"]["x-pullbox-source-origin"] == {
        "type": "boolean",
        "default": False,
        "description": (
            "Marks an HTTPS origin field for provider-scoped link and resolver policy."
        ),
    }


def test_openapi_documents_runtime_source_and_quota_failures() -> None:
    document = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    search_responses = document["paths"]["/v1/search"]["post"]["responses"]
    resolve_responses = document["paths"]["/v1/resolve"]["post"]["responses"]

    assert search_responses["503"] == {"$ref": "#/components/responses/SourceUnavailable"}
    assert resolve_responses["429"] == {"$ref": "#/components/responses/SourceQuotaLimited"}
    assert resolve_responses["503"] == {"$ref": "#/components/responses/SourceUnavailable"}
