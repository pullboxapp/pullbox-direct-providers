from __future__ import annotations

import os

import httpx
import pytest
from pullbox_provider_contract.conformance import run_provider_conformance

BASE_URL = os.environ.get("PULLBOX_PROVIDER_BASE_URL")
TOKEN = os.environ.get("PULLBOX_PROVIDER_TOKEN")

pytestmark = pytest.mark.skipif(
    not BASE_URL or not TOKEN,
    reason="live provider endpoint is not configured",
)


async def test_live_provider_conforms_over_private_network() -> None:
    assert BASE_URL is not None
    assert TOKEN is not None

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=5.0) as client:
        report = await run_provider_conformance(client=client, bearer_token=TOKEN)

    assert report.provider_id == "pullbox.synthetic"
    assert report.negotiated_protocol == "direct-download-provider/v1"
    assert report.operations == ("manifest", "health", "search", "resolve")
    assert report.candidate_count > 0
    assert report.artifact_count > 0
