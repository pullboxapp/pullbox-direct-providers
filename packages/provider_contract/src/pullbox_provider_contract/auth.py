"""Authentication helpers shared by provider services."""

from __future__ import annotations

import hmac


def bearer_token_matches(presented: str | None, expected: str) -> bool:
    """Compare non-empty bearer tokens with a constant-time primitive."""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())
