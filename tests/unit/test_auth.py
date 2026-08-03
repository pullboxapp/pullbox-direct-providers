from __future__ import annotations

from unittest.mock import patch

import pytest
from pullbox_provider_contract.auth import bearer_token_matches
from pullbox_provider_synthetic.app import create_app


def test_bearer_comparison_uses_constant_time_primitive() -> None:
    with patch("pullbox_provider_contract.auth.hmac.compare_digest", return_value=True) as compare:
        assert bearer_token_matches("presented", "expected") is True

    compare.assert_called_once_with(b"presented", b"expected")


def test_bearer_comparison_rejects_missing_or_empty_values() -> None:
    assert bearer_token_matches(None, "expected") is False
    assert bearer_token_matches("", "expected") is False
    assert bearer_token_matches("presented", "") is False


@pytest.mark.parametrize("token", [None, "", "too-short"])
def test_provider_refuses_to_start_without_strong_bearer_token(
    token: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PULLBOX_PROVIDER_TOKEN", raising=False)

    with pytest.raises(ValueError, match="at least 32 characters"):
        create_app(bearer_token=token)
