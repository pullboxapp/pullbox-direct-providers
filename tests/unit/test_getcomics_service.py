from __future__ import annotations

from pathlib import Path

import pytest
from pullbox_provider_contract.models import SearchIntent
from pullbox_provider_getcomics.service import GetComicsProviderService

FIXTURES = Path(__file__).parents[1] / "fixtures" / "getcomics"


class _Pages:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str, **_kwargs: object) -> str:
        self.urls.append(url)
        if "/dc/example-heroes" in url:
            return (FIXTURES / "release.html").read_text(encoding="utf-8")
        return (FIXTURES / "search-results.html").read_text(encoding="utf-8")


async def test_service_builds_bounded_search_and_resolves_stateless_candidate() -> None:
    pages = _Pages()
    service = GetComicsProviderService(page_fetcher=pages)
    intent = SearchIntent(
        series_title="Example Heroes",
        normalized_title="example heroes",
        issue_number="7",
        year=2026,
    )

    candidates = await service.search(intent, limit=1)
    artifacts = await service.resolve(candidates[0].provider_candidate_id)

    assert len(candidates) == 1
    assert candidates[0].parsed.issue_numbers == ["7"]
    assert len(artifacts) == 1
    assert pages.urls[0].startswith("https://getcomics.org/?s=")
    assert "Example+Heroes+7+2026" in pages.urls[0]
    assert pages.urls[1] == candidates[0].source_reference


async def test_service_rejects_forged_candidate_identifier() -> None:
    service = GetComicsProviderService(page_fetcher=_Pages())

    with pytest.raises(RuntimeError, match="candidate"):
        await service.resolve("getcomics:not-valid-base64!")
