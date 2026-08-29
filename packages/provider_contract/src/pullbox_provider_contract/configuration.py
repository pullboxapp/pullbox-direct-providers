"""Constrained native-control schema validation for provider settings."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

type ConfigurationValue = str | int | float | bool | None

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_FIELDS = 50
_MAX_TEXT_LENGTH = 2_000
_TOP_LEVEL_KEYS = {
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
}
_FIELD_KEYS = {
    "type",
    "title",
    "description",
    "default",
    "enum",
    "format",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "x-pullbox-secret",
    "x-pullbox-placeholder",
    "x-pullbox-suggestions",
    "x-pullbox-source-origin",
}
_FIELD_TYPES = frozenset({"string", "boolean", "integer", "number"})
_FIELD_FORMATS = frozenset({"uri"})
_SPECIAL_USE_SOURCE_SUFFIXES = (
    "localhost",
    "local",
    "onion",
    "internal",
    "home.arpa",
)


class ConfigurationSchemaError(ValueError):
    """Raised when provider-controlled settings cannot map to native controls."""


@dataclass(frozen=True, slots=True)
class ConfigurationField:
    """One validated provider setting rendered by Pullbox-owned UI."""

    name: str
    value_type: str
    title: str
    description: str | None
    required: bool
    secret: bool
    input_format: str | None
    default: ConfigurationValue
    choices: tuple[ConfigurationValue, ...]
    minimum: float | None
    maximum: float | None
    min_length: int | None
    max_length: int | None
    placeholder: str | None
    suggestions: tuple[str, ...]
    source_origin: bool


@dataclass(frozen=True, slots=True)
class ConfigurationSchema:
    """A safe, normalized provider settings schema."""

    title: str | None
    description: str | None
    properties: dict[str, ConfigurationField]


def validate_configuration_schema(raw: object) -> ConfigurationSchema:
    """Validate a provider schema against Pullbox's finite native-control set."""
    schema = _mapping(raw, "configuration schema")
    _reject_unknown_keys(schema, _TOP_LEVEL_KEYS, "configuration schema")
    if schema.get("type") != "object":
        raise ConfigurationSchemaError("Configuration schema type must be object.")
    if schema.get("additionalProperties") is not False:
        raise ConfigurationSchemaError("Configuration schema must reject additional properties.")

    raw_properties = _mapping(schema.get("properties"), "configuration properties")
    if len(raw_properties) > _MAX_FIELDS:
        raise ConfigurationSchemaError("Configuration schema exceeds the 50-field limit.")
    required = _required_names(schema.get("required", []), raw_properties)
    properties: dict[str, ConfigurationField] = {}
    for name, raw_field in raw_properties.items():
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
            raise ConfigurationSchemaError("Configuration field names must use snake_case.")
        properties[name] = _validate_field(name, raw_field, name in required)

    return ConfigurationSchema(
        title=_optional_text(schema.get("title"), "schema title"),
        description=_optional_text(schema.get("description"), "schema description"),
        properties=properties,
    )


def _validate_field(name: str, raw: object, required: bool) -> ConfigurationField:
    field = _mapping(raw, f"configuration field {name}")
    _reject_unknown_keys(field, _FIELD_KEYS, f"configuration field {name}")
    value_type = field.get("type")
    if not isinstance(value_type, str) or value_type not in _FIELD_TYPES:
        raise ConfigurationSchemaError(f"Configuration field {name} has an unsupported type.")
    secret = field.get("x-pullbox-secret", False)
    if not isinstance(secret, bool):
        raise ConfigurationSchemaError(f"Configuration field {name} has an invalid secret marker.")
    if secret and value_type != "string":
        raise ConfigurationSchemaError("Secret configuration fields must use string controls.")
    input_format = field.get("format")
    if input_format is not None and (
        input_format not in _FIELD_FORMATS or value_type != "string" or secret
    ):
        raise ConfigurationSchemaError(f"Configuration field {name} has an unsupported format.")

    choices = _choices(field.get("enum"), value_type, name)
    suggestions = _suggestions(
        field.get("x-pullbox-suggestions"),
        value_type=value_type,
        input_format=input_format,
        secret=secret,
        name=name,
    )
    source_origin = field.get("x-pullbox-source-origin", False)
    if not isinstance(source_origin, bool):
        raise ConfigurationSchemaError(
            f"Configuration field {name} has an invalid source-origin marker."
        )
    if source_origin and (value_type != "string" or input_format != "uri" or secret):
        raise ConfigurationSchemaError(
            f"Configuration field {name} has an invalid source-origin control."
        )
    default = field.get("default")
    if "default" in field:
        _validate_typed_value(default, value_type, name)
        if choices and default not in choices:
            raise ConfigurationSchemaError(f"Configuration field {name} has an invalid default.")
        if (source_origin or suggestions) and (
            not isinstance(default, str) or not _is_safe_https_origin(default)
        ):
            raise ConfigurationSchemaError(f"Configuration field {name} has an unsafe URI default.")

    minimum = _optional_number(field.get("minimum"), f"{name} minimum")
    maximum = _optional_number(field.get("maximum"), f"{name} maximum")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ConfigurationSchemaError(f"Configuration field {name} has invalid bounds.")
    min_length = _optional_nonnegative_int(field.get("minLength"), f"{name} minLength")
    max_length = _optional_nonnegative_int(field.get("maxLength"), f"{name} maxLength")
    if value_type in {"integer", "number"}:
        if min_length is not None or max_length is not None:
            raise ConfigurationSchemaError(
                f"Numeric configuration field {name} cannot use length bounds."
            )
    elif minimum is not None or maximum is not None:
        raise ConfigurationSchemaError(
            f"Non-numeric configuration field {name} cannot use numeric bounds."
        )
    if min_length is not None and max_length is not None and min_length > max_length:
        raise ConfigurationSchemaError(f"Configuration field {name} has invalid length bounds.")

    return ConfigurationField(
        name=name,
        value_type=value_type,
        title=_optional_text(field.get("title"), f"{name} title") or name.replace("_", " ").title(),
        description=_optional_text(field.get("description"), f"{name} description"),
        required=required,
        secret=secret,
        input_format=input_format if isinstance(input_format, str) else None,
        default=default
        if isinstance(default, (str, int, float, bool)) or default is None
        else None,
        choices=choices,
        minimum=minimum,
        maximum=maximum,
        min_length=min_length,
        max_length=max_length,
        placeholder=_optional_text(field.get("x-pullbox-placeholder"), f"{name} placeholder"),
        suggestions=suggestions,
        source_origin=source_origin,
    )


def _required_names(raw: object, properties: Mapping[object, object]) -> set[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ConfigurationSchemaError("Configuration schema required must be a string list.")
    if len(raw) > _MAX_FIELDS:
        raise ConfigurationSchemaError("Configuration schema exceeds the required field limit.")
    required = set(raw)
    if len(required) != len(raw):
        raise ConfigurationSchemaError("Configuration schema has duplicate required fields.")
    if not required.issubset(properties):
        raise ConfigurationSchemaError("Configuration schema requires an unknown field.")
    return required


def _choices(raw: object, value_type: str, name: str) -> tuple[ConfigurationValue, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        raise ConfigurationSchemaError(f"Configuration field {name} has invalid choices.")
    choices: list[ConfigurationValue] = []
    for value in raw:
        _validate_typed_value(value, value_type, name)
        if value in choices:
            raise ConfigurationSchemaError(f"Configuration field {name} has duplicate choices.")
        choices.append(value)
    return tuple(choices)


def _suggestions(
    raw: object,
    *,
    value_type: object,
    input_format: object,
    secret: bool,
    name: str,
) -> tuple[str, ...]:
    if raw is None:
        return ()
    if value_type != "string" or input_format != "uri" or secret:
        raise ConfigurationSchemaError(f"Configuration field {name} has invalid URI suggestions.")
    if not isinstance(raw, list) or not raw or len(raw) > 20:
        raise ConfigurationSchemaError(f"Configuration field {name} has invalid URI suggestions.")
    suggestions: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not _is_safe_https_origin(value):
            raise ConfigurationSchemaError(
                f"Configuration field {name} has invalid URI suggestions."
            )
        if value in suggestions:
            raise ConfigurationSchemaError(
                f"Configuration field {name} has duplicate URI suggestions."
            )
        suggestions.append(value)
    return tuple(suggestions)


def _is_safe_https_origin(raw: str) -> bool:
    if not raw or len(raw) > _MAX_TEXT_LENGTH:
        return False
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname
    if hostname is None:
        return False
    normalized_hostname = hostname.casefold().rstrip(".")
    if not _is_public_source_hostname_syntax(normalized_hostname):
        return False

    return bool(
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _is_public_source_hostname_syntax(hostname: str) -> bool:
    if any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _SPECIAL_USE_SOURCE_SUFFIXES
    ):
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return False
    return not _looks_like_legacy_ipv4_literal(hostname)


def _looks_like_legacy_ipv4_literal(hostname: str) -> bool:
    labels = hostname.split(".")
    if not 1 <= len(labels) <= 4:
        return False
    for label in labels:
        if not label:
            return False
        if label.startswith("0x"):
            digits = label[2:]
            if not digits or any(character not in "0123456789abcdef" for character in digits):
                return False
        elif not label.isascii() or not label.isdigit():
            return False
    return True


def _validate_typed_value(value: object, value_type: str, name: str) -> None:
    valid = (
        (value_type == "string" and isinstance(value, str))
        or (value_type == "boolean" and isinstance(value, bool))
        or (value_type == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (
            value_type == "number"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
    )
    if not valid:
        raise ConfigurationSchemaError(f"Configuration field {name} has an invalid value type.")


def _mapping(raw: object, label: str) -> Mapping[object, object]:
    if not isinstance(raw, Mapping):
        raise ConfigurationSchemaError(f"{label.capitalize()} must be an object.")
    return raw


def _reject_unknown_keys(
    value: Mapping[object, object],
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ConfigurationSchemaError(f"{label.capitalize()} contains unsupported controls.")


def _optional_text(raw: object, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > _MAX_TEXT_LENGTH:
        raise ConfigurationSchemaError(f"{label.capitalize()} must be bounded text.")
    return raw.strip()


def _optional_number(raw: object, label: str) -> float | None:
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise ConfigurationSchemaError(f"{label.capitalize()} must be numeric.")
    return float(raw)


def _optional_nonnegative_int(raw: object, label: str) -> int | None:
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ConfigurationSchemaError(f"{label.capitalize()} must be nonnegative.")
    return raw
