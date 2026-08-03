from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pullbox_provider_synthetic.app import create_app

TEST_TOKEN = "test-provider-token-with-sufficient-entropy"


@pytest.fixture
def app() -> FastAPI:
    return create_app(bearer_token=TEST_TOKEN)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://provider.test") as value:
        yield value


def future_deadline() -> str:
    return (datetime.now(UTC) + timedelta(minutes=1)).isoformat()


def search_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": "direct-download-provider/v1",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "deadline": future_deadline(),
        "limit": 20,
        "intent": {
            "series_title": "Synthetic Adventures",
            "normalized_title": "synthetic adventures",
            "alternate_titles": [],
            "issue_number": "1",
            "issue_type": "issue",
            "year": 2026,
            "publisher": "Pullbox Labs",
            "language": "en",
            "preferred_formats": ["cbz"],
        },
        "provider_config": {},
        "source_credentials": {},
    }
    payload.update(overrides)
    return payload


def resolve_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": "direct-download-provider/v1",
        "request_id": "22222222-2222-4222-8222-222222222222",
        "deadline": future_deadline(),
        "provider_candidate_id": "synthetic-issue-1",
        "provider_config": {},
        "source_credentials": {},
    }
    payload.update(overrides)
    return payload
