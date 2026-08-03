"""Recursive redaction for provider diagnostics and errors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "session",
    "token",
}


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc or not parts.query:
        return value
    query = [
        (key, _REDACTED if key.casefold() in _SECRET_KEYS else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_sensitive(value: Any) -> Any:
    """Return a redacted deep copy suitable for diagnostics."""
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if str(key).casefold() in _SECRET_KEYS else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return _redact_url(value)
    return value
