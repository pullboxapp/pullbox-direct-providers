from __future__ import annotations

from pullbox_provider_contract.redaction import redact_sensitive


def test_nested_secrets_and_signed_urls_are_redacted_without_mutating_input() -> None:
    payload = {
        "authorization": "Bearer super-secret",
        "nested": {
            "api_key": "source-secret",
            "url": "https://files.example/book.cbz?token=signed-secret&safe=yes",
        },
        "safe": "visible",
    }

    redacted = redact_sensitive(payload)

    assert redacted == {
        "authorization": "[REDACTED]",
        "nested": {
            "api_key": "[REDACTED]",
            "url": "https://files.example/book.cbz?token=%5BREDACTED%5D&safe=yes",
        },
        "safe": "visible",
    }
    assert payload["authorization"] == "Bearer super-secret"
