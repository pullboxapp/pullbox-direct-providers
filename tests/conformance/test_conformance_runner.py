from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pullbox_provider_contract.conformance import (
    ConformanceError,
    run_provider_conformance,
)

from tests.conftest import TEST_TOKEN

FIXTURES = Path(__file__).parents[1] / "fixtures" / "protocol-v1"


class _OversizedChunkStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.emitted_chunks = 0

    async def __aiter__(self):
        for _index in range(10):
            self.emitted_chunks += 1
            yield b"x" * (1024 * 1024)


def _fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _protocol_handler(
    *,
    manifest: dict[str, object] | None = None,
    search_candidates: list[object] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/manifest":
            return httpx.Response(200, json=manifest or _fixture("manifest-response.json"))
        if request.url.path == "/v1/health":
            return httpx.Response(200, json=_fixture("health-response.json"))
        request_payload = json.loads(request.content)
        if request.url.path == "/v1/search":
            payload = _fixture("search-response.json")
            payload["request_id"] = request_payload["request_id"]
            if search_candidates is not None:
                payload["candidates"] = search_candidates
            return httpx.Response(200, json=payload)
        payload = _fixture("resolve-response.json")
        payload["request_id"] = request_payload["request_id"]
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


async def test_runner_exercises_all_four_protocol_operations(app) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://provider.test",
    ) as client:
        report = await run_provider_conformance(
            client=client,
            bearer_token=TEST_TOKEN,
        )

    assert report.provider_id == "pullbox.synthetic"
    assert report.negotiated_protocol == "direct-download-provider/v1"
    assert report.operations == ("manifest", "health", "search", "resolve")
    assert report.candidate_count > 0
    assert report.artifact_count > 0


async def test_runner_reports_authentication_failure_without_exposing_token(app) -> None:
    token = "wrong-provider-token-that-must-not-appear"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://provider.test",
    ) as client:
        with pytest.raises(ConformanceError) as exc_info:
            await run_provider_conformance(client=client, bearer_token=token)

    assert exc_info.value.code == "provider_authentication_failed"
    assert token not in str(exc_info.value)


async def test_runner_rejects_incompatible_manifest_before_source_operations() -> None:
    manifest = _fixture("manifest-response.json")
    manifest["supported_protocol_versions"] = ["direct-download-provider/v2"]
    async with httpx.AsyncClient(
        transport=_protocol_handler(manifest=manifest),
        base_url="https://provider.test",
    ) as client:
        with pytest.raises(ConformanceError) as exc_info:
            await run_provider_conformance(client=client, bearer_token=TEST_TOKEN)

    assert exc_info.value.code == "incompatible_manifest"


async def test_runner_rejects_malformed_json_without_echoing_response() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, content=b"not-json-secret-material")
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://provider.test",
    ) as client:
        with pytest.raises(ConformanceError) as exc_info:
            await run_provider_conformance(client=client, bearer_token=TEST_TOKEN)

    assert exc_info.value.code == "malformed_response"
    assert "secret-material" not in str(exc_info.value)


async def test_runner_aborts_stream_as_soon_as_response_exceeds_limit() -> None:
    stream = _OversizedChunkStream()
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, stream=stream))
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://provider.test",
    ) as client:
        with pytest.raises(ConformanceError) as exc_info:
            await run_provider_conformance(client=client, bearer_token=TEST_TOKEN)

    assert exc_info.value.code == "response_too_large"
    assert stream.emitted_chunks == 3


async def test_runner_requires_a_resolvable_search_candidate() -> None:
    async with httpx.AsyncClient(
        transport=_protocol_handler(search_candidates=[]),
        base_url="https://provider.test",
    ) as client:
        with pytest.raises(ConformanceError) as exc_info:
            await run_provider_conformance(client=client, bearer_token=TEST_TOKEN)

    assert exc_info.value.code == "no_resolvable_candidate"
