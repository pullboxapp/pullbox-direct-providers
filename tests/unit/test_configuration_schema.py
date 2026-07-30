from __future__ import annotations

import pytest
from pullbox_provider_contract.configuration import (
    ConfigurationSchemaError,
    validate_configuration_schema,
)


def test_configuration_schema_accepts_native_allowlisted_controls() -> None:
    schema = {
        "type": "object",
        "properties": {
            "member_token": {
                "type": "string",
                "title": "Member token",
                "description": "Optional provider account token.",
                "x-pullbox-secret": True,
                "maxLength": 4096,
            },
            "include_collections": {
                "type": "boolean",
                "title": "Include collections",
                "default": False,
            },
            "result_limit": {
                "type": "integer",
                "title": "Result limit",
                "minimum": 1,
                "maximum": 100,
                "default": 20,
            },
            "language": {
                "type": "string",
                "title": "Language",
                "enum": ["en", "fr"],
                "default": "en",
            },
            "source_url": {
                "type": "string",
                "title": "Source URL",
                "format": "uri",
                "enum": ["https://source-one.example", "https://source-two.example"],
                "default": "https://source-one.example",
            },
        },
        "additionalProperties": False,
        "required": ["language"],
    }

    validated = validate_configuration_schema(schema)

    assert tuple(validated.properties) == (
        "member_token",
        "include_collections",
        "result_limit",
        "language",
        "source_url",
    )
    assert validated.properties["member_token"].secret is True
    assert validated.properties["source_url"].input_format == "uri"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}, "additionalProperties": True},
        {
            "type": "object",
            "properties": {"nested": {"type": "object"}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"unsafe": {"type": "string", "html": "<script>"}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"unsafe": {"type": "boolean", "x-pullbox-secret": True}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"unsafe": {"type": "string", "format": "html"}},
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"bad-name": {"type": "string"}},
            "additionalProperties": False,
        },
    ],
)
def test_configuration_schema_rejects_non_native_or_executable_controls(
    schema: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationSchemaError):
        validate_configuration_schema(schema)


def test_configuration_schema_rejects_more_than_fifty_controls() -> None:
    schema = {
        "type": "object",
        "properties": {f"field_{index}": {"type": "string"} for index in range(51)},
        "additionalProperties": False,
    }

    with pytest.raises(ConfigurationSchemaError):
        validate_configuration_schema(schema)


@pytest.mark.parametrize(
    "field",
    [
        {"type": "integer", "minimum": 10, "maximum": 1},
        {"type": "string", "minLength": 10, "maxLength": 1},
        {"type": "integer", "minLength": 1},
        {"type": "string", "minimum": 1},
    ],
)
def test_configuration_schema_rejects_inconsistent_or_cross_type_bounds(
    field: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationSchemaError):
        validate_configuration_schema(
            {
                "type": "object",
                "properties": {"unsafe": field},
                "additionalProperties": False,
            }
        )
