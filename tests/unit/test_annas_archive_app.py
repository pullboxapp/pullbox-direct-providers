from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pullbox_provider_annas_archive.app import create_app
from pullbox_provider_annas_archive.service import AnnasArchiveResolveResult
from pullbox_provider_contract.errors import ProtocolError
from pullbox_provider_contract.models import Candidate, ParsedCandidate, QuotaStatus

from tests.conftest import TEST_TOKEN, resolve_payload, search_payload

MEMBER_SECRET = "member-secret-never-rendered"


class _AnnaService:
    def __init__(
        self,
        *,
        available: bool = True,
        error: Exception | None = None,
        candidates: list[Candidate] | None = None,
    ) -> None:
        self.available = available
        self.error = error
        self.candidates = candidates or []
        self.search_kwargs: dict[str, object] | None = None
        self.resolve_kwargs: dict[str, object] | None = None

    async def source_available(self) -> bool:
        return self.available

    async def source_reachability(self) -> dict[str, bool]:
        return {
            "annas-archive.gl": self.available,
            "annas-archive.pk": self.available,
            "annas-archive.gd": self.available,
        }

    async def search(self, _intent: object, **kwargs: object) -> list[Candidate]:
        self.search_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return self.candidates[: int(kwargs["limit"])]

    async def resolve(self, _candidate_id: str, **kwargs: object) -> AnnasArchiveResolveResult:
        self.resolve_kwargs = kwargs
        if self.error is not None:
            raise self.error
        return AnnasArchiveResolveResult(
            artifacts=[],
            quota=QuotaStatus(remaining=22, limit=25, window_seconds=64_800),
        )


def _app(service: _AnnaService | None = None) -> FastAPI:
    return create_app(bearer_token=TEST_TOKEN, service=service or _AnnaService())


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    authenticated: bool = True,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"} if authenticated else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://provider.test",
    ) as client:
        return await client.request(method, path, headers=headers, json=json)


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/v1/manifest", None),
        ("GET", "/v1/health", None),
        ("POST", "/v1/search", search_payload()),
        ("POST", "/v1/resolve", resolve_payload()),
    ],
)
async def test_anna_operations_require_provider_bearer_token(
    method: str,
    path: str,
    payload: dict[str, Any] | None,
) -> None:
    response = await _request(_app(), method, path, json=payload, authenticated=False)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "provider_authentication_failed"


async def test_anna_manifest_marks_member_key_secret_and_official_urls_editable() -> None:
    response = await _request(_app(), "GET", "/v1/manifest")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider_id"] == "pullbox.annas_archive"
    assert payload["provider_version"] == "1.0.0"
    assert payload["source_domains"] == [
        "annas-archive.gl",
        "annas-archive.pk",
        "annas-archive.gd",
        "libgen.gl",
        "libgen.li",
        "libgen.vg",
        "libgen.la",
        "libgen.bz",
    ]
    assert payload["artifact_host_patterns"] == ["generic_https"]
    schema = payload["configuration_schema"]
    domain = schema["properties"]["domain"]
    assert domain["default"] == "https://annas-archive.gd"
    assert domain["format"] == "uri"
    assert domain["enum"] == [
        "https://annas-archive.gl",
        "https://annas-archive.pk",
        "https://annas-archive.gd",
    ]
    assert schema["properties"]["member_secret_key"]["x-pullbox-secret"] is True
    assert schema["required"] == ["member_secret_key"]


async def test_anna_search_and_resolve_forward_secrets_only_to_active_operation() -> None:
    service = _AnnaService()
    app = _app(service)
    search = await _request(
        app,
        "POST",
        "/v1/search",
        json=search_payload(
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={"member_secret_key": MEMBER_SECRET},
        ),
    )
    resolve = await _request(
        app,
        "POST",
        "/v1/resolve",
        json=resolve_payload(
            provider_config={"domain": "https://annas-archive.gd"},
            source_credentials={"member_secret_key": MEMBER_SECRET},
        ),
    )

    assert search.status_code == 200
    assert resolve.status_code == 200
    assert service.search_kwargs is not None
    assert service.resolve_kwargs is not None
    assert service.search_kwargs["provider_config"] == {"domain": "https://annas-archive.gd"}
    assert "source_credentials" not in service.search_kwargs
    assert service.resolve_kwargs["source_credentials"] == {"member_secret_key": MEMBER_SECRET}
    assert MEMBER_SECRET not in search.text
    assert MEMBER_SECRET not in resolve.text
    assert resolve.json()["quota"] == {
        "remaining": 22,
        "limit": 25,
        "window_seconds": 64_800,
        "reset_at": None,
    }


async def test_anna_quota_error_returns_safe_retry_window() -> None:
    response = await _request(
        _app(
            _AnnaService(
                error=ProtocolError(
                    429,
                    "source_quota_limited",
                    "Fast-download quota is unavailable.",
                    retry_after_seconds=64_800,
                )
            )
        ),
        "POST",
        "/v1/resolve",
        json=resolve_payload(),
    )

    assert response.status_code == 429
    assert response.json()["error"] == {
        "code": "source_quota_limited",
        "message": "Fast-download quota is unavailable.",
        "retry_after_seconds": 64_800,
    }


async def test_anna_health_and_unexpected_source_errors_are_explicit_and_safe() -> None:
    degraded = await _request(_app(_AnnaService(available=False)), "GET", "/v1/health")
    failed = await _request(
        _app(_AnnaService(error=RuntimeError(MEMBER_SECRET))),
        "POST",
        "/v1/search",
        json=search_payload(),
    )

    assert degraded.status_code == 200
    assert degraded.json()["source_status"] == "degraded"
    assert degraded.json()["diagnostics"] == {
        "source": "unreachable",
        "source.annas-archive.gl": "unreachable",
        "source.annas-archive.pk": "unreachable",
        "source.annas-archive.gd": "unreachable",
    }
    assert failed.status_code == 503
    assert failed.json()["error"] == {
        "code": "source_unavailable",
        "message": "Anna's Archive is temporarily unavailable.",
    }
    assert MEMBER_SECRET not in failed.text


async def test_anna_health_reports_each_official_domain_independently() -> None:
    service = _AnnaService()

    async def reachability() -> dict[str, bool]:
        return {
            "annas-archive.gl": True,
            "annas-archive.pk": False,
            "annas-archive.gd": False,
        }

    service.source_reachability = reachability  # type: ignore[method-assign]

    response = await _request(_app(service), "GET", "/v1/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_status"] == "healthy"
    assert payload["diagnostics"] == {
        "source": "reachable",
        "source.annas-archive.gl": "reachable",
        "source.annas-archive.pk": "unreachable",
        "source.annas-archive.gd": "unreachable",
    }


def _candidate(number: int) -> Candidate:
    title = f"Example {number}"
    return Candidate(
        provider_candidate_id=f"anna:{number:032x}",
        source_reference=f"https://annas-archive.gd/md5/{number:032x}",
        display_title=title,
        raw_title=title,
        parsed=ParsedCandidate(series_title="Example", issue_numbers=[str(number)]),
        provider_confidence=1,
    )


@pytest.mark.parametrize(
    ("candidate_count", "expected_truncated"),
    [(2, False), (3, True)],
)
async def test_anna_search_reports_truncation_only_when_results_were_dropped(
    candidate_count: int,
    expected_truncated: bool,
) -> None:
    service = _AnnaService(candidates=[_candidate(index) for index in range(candidate_count)])

    response = await _request(
        _app(service),
        "POST",
        "/v1/search",
        json=search_payload(limit=2),
    )

    assert response.status_code == 200
    assert len(response.json()["candidates"]) == 2
    assert response.json()["truncated"] is expected_truncated
    assert service.search_kwargs is not None
    assert service.search_kwargs["limit"] == 3
