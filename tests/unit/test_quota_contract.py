"""Additive quota telemetry contracts."""

from uuid import UUID

from pullbox_provider_contract.models import QuotaStatus, ResolveResponse


def test_resolve_quota_excludes_source_account_history() -> None:
    response = ResolveResponse.model_validate(
        {
            "request_id": str(UUID("22222222-2222-4222-8222-222222222222")),
            "artifacts": [],
            "quota": {
                "remaining": 22,
                "limit": 25,
                "window_seconds": 64_800,
                "recently_downloaded_md5s": ["must-not-cross-the-boundary"],
            },
        }
    )

    assert response.quota == QuotaStatus(remaining=22, limit=25, window_seconds=64_800)
    assert "recently_downloaded" not in response.model_dump_json()
