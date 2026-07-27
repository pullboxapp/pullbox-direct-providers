from __future__ import annotations

import os

import httpx
import pytest

from tests.conftest import resolve_payload, search_payload

BASE_URL = os.environ.get("PULLBOX_PROVIDER_BASE_URL")
TOKEN = os.environ.get("PULLBOX_PROVIDER_TOKEN")

pytestmark = pytest.mark.skipif(
    not BASE_URL or not TOKEN,
    reason="live provider endpoint is not configured",
)


def _headers() -> dict[str, str]:
    assert TOKEN is not None
    return {"Authorization": f"Bearer {TOKEN}"}


def test_live_provider_conforms_over_private_network() -> None:
    assert BASE_URL is not None

    with httpx.Client(base_url=BASE_URL, timeout=5.0) as client:
        manifest = client.get("/v1/manifest", headers=_headers())
        health = client.get("/v1/health", headers=_headers())
        search = client.post("/v1/search", headers=_headers(), json=search_payload())
        resolve = client.post("/v1/resolve", headers=_headers(), json=resolve_payload())

    assert manifest.status_code == 200
    assert manifest.json()["provider_id"] == "pullbox.synthetic"
    assert health.status_code == 200
    assert health.json()["process_status"] == "healthy"
    assert search.status_code == 200
    assert search.json()["candidates"]
    assert resolve.status_code == 200
    assert resolve.json()["artifacts"]
