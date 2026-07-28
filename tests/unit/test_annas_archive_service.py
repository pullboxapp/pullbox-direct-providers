from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from pullbox_provider_annas_archive import service as service_module
from pullbox_provider_annas_archive.service import (
    AnnasArchiveProviderService,
    _fetch_fast_download,
    validate_official_domain,
)
from pullbox_provider_contract.errors import ProtocolError
from pullbox_provider_contract.models import SearchIntent

FIXTURES = Path(__file__).parents[1] / "fixtures" / "annas_archive"
MEMBER_KEY = "member-key-that-must-never-appear"
SIGNED_URL = "https://download.example/signed?token=must-never-appear"
_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class _Source:
    async def page(self, _url: str, **_kwargs: object) -> str:
        return (FIXTURES / "search-results.html").read_text(encoding="utf-8")

    async def fast(
        self,
        *,
        domain: str,
        md5: str,
        member_secret_key: str,
    ) -> tuple[int, dict[str, object]]:
        assert domain == "https://annas-archive.gd"
        assert md5 == "11111111111111111111111111111111"
        assert member_secret_key == MEMBER_KEY
        return 200, {
            "download_url": SIGNED_URL,
            "account_fast_download_info": {"downloads_left": 9},
        }


async def test_member_search_and_fast_resolve_use_secret_only_for_active_request() -> None:
    source = _Source()
    service = AnnasArchiveProviderService(
        page_fetcher=source.page,
        fast_download_fetcher=source.fast,
    )
    candidates = await service.search(
        SearchIntent(
            series_title="Example Heroes",
            normalized_title="example heroes",
            issue_number="7",
            year=2026,
        ),
        provider_config={"domain": "https://annas-archive.gd"},
        limit=20,
    )
    artifacts = await service.resolve(
        candidates[0].provider_candidate_id,
        provider_config={"domain": "https://annas-archive.gd"},
        source_credentials={"member_secret_key": MEMBER_KEY},
    )

    assert candidates[0].provider_candidate_id.startswith("anna:")
    assert artifacts[0].mirrors[0].final_url == SIGNED_URL
    assert MEMBER_KEY not in repr(artifacts)
    assert SIGNED_URL not in repr(artifacts)


async def test_missing_membership_and_quota_are_distinct_safe_failures() -> None:
    service = AnnasArchiveProviderService(
        page_fetcher=_Source().page,
        fast_download_fetcher=_Source().fast,
    )
    with pytest.raises(ProtocolError) as missing:
        await service.resolve(
            "anna:11111111111111111111111111111111",
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={},
        )
    assert missing.value.status_code == 401
    assert missing.value.code == "source_authentication_required"

    async def quota(**_kwargs: object) -> tuple[int, dict[str, object]]:
        return 429, {"download_url": None, "error": "No fast downloads remaining"}

    quota_service = AnnasArchiveProviderService(
        page_fetcher=_Source().page,
        fast_download_fetcher=quota,
    )
    with pytest.raises(ProtocolError) as limited:
        await quota_service.resolve(
            "anna:11111111111111111111111111111111",
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={"member_secret_key": MEMBER_KEY},
        )
    assert limited.value.status_code == 429
    assert limited.value.code == "source_quota_limited"
    assert MEMBER_KEY not in str(limited.value)


@pytest.mark.parametrize(
    "domain",
    [
        "http://annas-archive.gd",
        "https://annas-archive.example",
        "https://annas-archive.gd.evil.example",
        "https://user@annas-archive.gd",
        "https://annas-archive.gd:invalid",
        "https://annas-archive.gd/path",
    ],
)
def test_only_exact_official_domain_is_accepted(domain: str) -> None:
    with pytest.raises(ProtocolError, match="official domain"):
        validate_official_domain(domain)


def test_current_official_domain_is_normalized() -> None:
    assert validate_official_domain("https://annas-archive.gd/") == "https://annas-archive.gd"


async def test_empty_successful_fast_download_response_fails_closed() -> None:
    async def empty(**_kwargs: object) -> tuple[int, dict[str, object]]:
        return 204, {}

    service = AnnasArchiveProviderService(
        page_fetcher=_Source().page,
        fast_download_fetcher=empty,
    )

    with pytest.raises(ProtocolError) as malformed:
        await service.resolve(
            "anna:11111111111111111111111111111111",
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={"member_secret_key": MEMBER_KEY},
        )

    assert malformed.value.code == "source_malformed_response"


@pytest.mark.parametrize(
    ("status", "payload", "expected_status", "expected_code"),
    [
        (403, {}, 401, "source_authentication_required"),
        (200, {"error": "Daily quota reached"}, 429, "source_quota_limited"),
        (500, {}, 503, "source_unavailable"),
        (
            200,
            {"download_url": "http://download.example/file.cbz"},
            503,
            "source_malformed_response",
        ),
        (
            200,
            {"download_url": "https://user@download.example/file.cbz"},
            503,
            "source_malformed_response",
        ),
    ],
)
async def test_fast_resolve_maps_auth_quota_source_and_url_failures(
    status: int,
    payload: dict[str, object],
    expected_status: int,
    expected_code: str,
) -> None:
    async def response(**_kwargs: object) -> tuple[int, dict[str, object]]:
        return status, payload

    service = AnnasArchiveProviderService(
        page_fetcher=_Source().page,
        fast_download_fetcher=response,
    )

    with pytest.raises(ProtocolError) as error:
        await service.resolve(
            "anna:11111111111111111111111111111111",
            provider_config={},
            source_credentials={"member_secret_key": MEMBER_KEY},
        )

    assert error.value.status_code == expected_status
    assert error.value.code == expected_code
    assert MEMBER_KEY not in str(error.value)


async def test_source_health_isolated_from_source_failure() -> None:
    healthy = AnnasArchiveProviderService(page_fetcher=_Source().page)

    async def unavailable(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("source unavailable")

    degraded = AnnasArchiveProviderService(page_fetcher=unavailable)

    assert await healthy.source_available() is True
    assert await degraded.source_available() is False


def _install_fast_download_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: httpx.AsyncBaseTransport,
) -> None:
    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return _HTTPX_ASYNC_CLIENT(**kwargs, transport=handler)

    monkeypatch.setattr(service_module.httpx, "AsyncClient", client_factory)


async def test_fast_download_http_client_reads_bounded_json_and_closes_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed = request
        return httpx.Response(200, json={"download_url": SIGNED_URL})

    _install_fast_download_transport(monkeypatch, httpx.MockTransport(handler))

    status, payload = await _fetch_fast_download(
        domain="https://annas-archive.gd",
        md5="11111111111111111111111111111111",
        member_secret_key=MEMBER_KEY,
    )

    assert status == 200
    assert payload == {"download_url": SIGNED_URL}
    assert observed is not None
    assert observed.url.path == "/dyn/api/fast_download.json"
    assert observed.url.params["md5"] == "11111111111111111111111111111111"
    assert observed.url.params["key"] == MEMBER_KEY


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        (httpx.Response(200, content=b"not-json"), "source_malformed_response"),
        (httpx.Response(200, json=["not", "an", "object"]), "source_malformed_response"),
        (
            httpx.Response(200, content=b"x" * (service_module._MAX_JSON_BYTES + 1)),
            "source_malformed_response",
        ),
    ],
)
async def test_fast_download_http_client_rejects_malformed_or_oversized_json(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    expected_code: str,
) -> None:
    _install_fast_download_transport(
        monkeypatch,
        httpx.MockTransport(lambda _request: response),
    )

    with pytest.raises(ProtocolError) as error:
        await _fetch_fast_download(
            domain="https://annas-archive.gd",
            md5="11111111111111111111111111111111",
            member_secret_key=MEMBER_KEY,
        )

    assert error.value.code == expected_code


async def test_fast_download_http_client_maps_network_failure_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    _install_fast_download_transport(monkeypatch, httpx.MockTransport(failed))
    with pytest.raises(ProtocolError) as unavailable:
        await _fetch_fast_download(
            domain="https://annas-archive.gd",
            md5="11111111111111111111111111111111",
            member_secret_key=MEMBER_KEY,
        )
    assert unavailable.value.code == "source_unavailable"

    async def cancelled(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    _install_fast_download_transport(monkeypatch, httpx.MockTransport(cancelled))
    with pytest.raises(asyncio.CancelledError):
        await _fetch_fast_download(
            domain="https://annas-archive.gd",
            md5="11111111111111111111111111111111",
            member_secret_key=MEMBER_KEY,
        )
