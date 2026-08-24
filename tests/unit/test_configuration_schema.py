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
                "x-pullbox-suggestions": [
                    "https://source-one.example",
                    "https://source-two.example",
                ],
                "x-pullbox-source-origin": True,
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
    assert validated.properties["source_url"].suggestions == (
        "https://source-one.example",
        "https://source-two.example",
    )
    assert validated.properties["source_url"].source_origin is True
    assert validated.properties["source_url"].choices == ()


def test_configuration_schema_keeps_uri_suggestions_open() -> None:
    schema = validate_configuration_schema(
        {
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "format": "uri",
                    "default": "https://custom.example",
                    "x-pullbox-suggestions": ["https://known.example"],
                    "x-pullbox-source-origin": True,
                }
            },
            "additionalProperties": False,
        }
    )

    assert schema.properties["source_url"].default == "https://custom.example"


def test_configuration_schema_keeps_suggestions_and_source_origin_independent() -> None:
    schema = validate_configuration_schema(
        {
            "type": "object",
            "properties": {
                "suggested_url": {
                    "type": "string",
                    "format": "uri",
                    "x-pullbox-suggestions": ["https://known.example"],
                },
                "custom_origin": {
                    "type": "string",
                    "format": "uri",
                    "x-pullbox-source-origin": True,
                },
            },
            "additionalProperties": False,
        }
    )

    assert schema.properties["suggested_url"].suggestions == ("https://known.example",)
    assert schema.properties["suggested_url"].source_origin is False
    assert schema.properties["custom_origin"].suggestions == ()
    assert schema.properties["custom_origin"].source_origin is True


@pytest.mark.parametrize(
    "field",
    [
        {
            "type": "boolean",
            "x-pullbox-suggestions": ["https://source.example"],
        },
        {
            "type": "string",
            "x-pullbox-suggestions": ["https://source.example"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["http://source.example"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.example/path"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://127.0.0.1"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://localhost"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://2130706433"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://0x7f000001"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.local"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.onion"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.internal"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.home.arpa"],
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-suggestions": ["https://source.example"] * 21,
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-source-origin": "yes",
        },
        {
            "type": "string",
            "format": "uri",
            "x-pullbox-secret": True,
            "x-pullbox-source-origin": True,
        },
    ],
)
def test_configuration_schema_rejects_unsafe_source_origin_extensions(
    field: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationSchemaError):
        validate_configuration_schema(
            {
                "type": "object",
                "properties": {"source_url": field},
                "additionalProperties": False,
            }
        )


@pytest.mark.parametrize(
    "default",
    [
        "http://source.example",
        "https://127.0.0.1",
        "https://2130706433",
        "https://source.local",
        "https://source.onion",
        "https://source.internal",
        "https://source.home.arpa",
    ],
)
def test_configuration_schema_rejects_unsafe_source_origin_default(default: str) -> None:
    with pytest.raises(ConfigurationSchemaError):
        validate_configuration_schema(
            {
                "type": "object",
                "properties": {
                    "source_url": {
                        "type": "string",
                        "format": "uri",
                        "default": default,
                        "x-pullbox-source-origin": True,
                    }
                },
                "additionalProperties": False,
            }
        )


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
