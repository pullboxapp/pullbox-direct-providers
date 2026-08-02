from __future__ import annotations

import asyncio
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


class _Redirects:
    def __init__(self, destinations: dict[str, str | RuntimeError]) -> None:
        self.destinations = destinations
        self.urls: list[str] = []

    async def __call__(self, url: str) -> str:
        self.urls.append(url)
        destination = self.destinations[url]
        if isinstance(destination, RuntimeError):
            raise destination
        return destination


async def test_service_builds_bounded_search_and_resolves_stateless_candidate() -> None:
    pages = _Pages()
    service = GetComicsProviderService(
        page_fetcher=pages,
        redirect_resolver=_Redirects(
            {"https://getcomics.org/dls/opaque-primary": ("https://files.example.test/example.cbz")}
        ),
    )
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


async def test_service_retries_collection_search_without_synthetic_issue_number() -> None:
    urls: list[str] = []

    async def pages(url: str, **_kwargs: object) -> str:
        urls.append(url)
        if "+1+2025" in url:
            return '<html><body><h1 class="search-title">Search Result</h1></body></html>'
        return """
        <html><body>
          <h1 class="search-title">Search Result</h1>
          <article><h1 class="post-title">
            <a href="https://getcomics.org/other-comics/east-of-west-the-end-times-compendium-2025/">
              East of West - The End Times Compendium (2025)
            </a>
          </h1></article>
        </body></html>
        """

    service = GetComicsProviderService(page_fetcher=pages)
    intent = SearchIntent(
        series_title="East of West: The End Times Compendium",
        normalized_title="east of west the end times compendium",
        issue_number="1",
        issue_type="compendium",
        volume="1",
        year=2025,
    )

    candidates = await service.search(intent, limit=10)

    assert [candidate.display_title for candidate in candidates] == [
        "East of West - The End Times Compendium (2025)"
    ]
    assert len(urls) == 2
    assert "East+of+West%3A+The+End+Times+Compendium+1+2025" in urls[0]
    assert "East+of+West%3A+The+End+Times+Compendium+2025" in urls[1]


async def test_service_does_not_broaden_empty_standard_issue_search() -> None:
    urls: list[str] = []

    async def pages(url: str, **_kwargs: object) -> str:
        urls.append(url)
        return '<html><body><h1 class="search-title">Search Result</h1></body></html>'

    service = GetComicsProviderService(page_fetcher=pages)
    intent = SearchIntent(
        series_title="Example Heroes",
        normalized_title="example heroes",
        issue_number="7",
        issue_type="issue",
        year=2026,
    )

    assert await service.search(intent, limit=10) == []
    assert len(urls) == 1


async def test_service_rejects_forged_candidate_identifier() -> None:
    service = GetComicsProviderService(page_fetcher=_Pages())

    with pytest.raises(RuntimeError, match="candidate"):
        await service.resolve("getcomics:not-valid-base64!")


async def test_service_unwraps_all_opaque_artifact_host_buttons() -> None:
    source_urls = {
        "https://getcomics.org/dls/pixel": "https://pixeldrain.com/u/pixel",
        "https://getcomics.org/dls/mega": "https://mega.nz/file/mega#key",
        "https://getcomics.org/dls/rootz": "https://rootz.so/file/rootz",
        "https://getcomics.org/dls/mediafire": "https://mediafire.com/file/media/file.cbz",
        "https://getcomics.org/dls/terabox": "https://terabox.link/s/terabox",
        "https://getcomics.org/dls/datanodes": "https://datanodes.to/download/data",
    }
    buttons = "".join(
        f'<a class="aio-red" title="{label}" href="{source}">{label}</a>'
        for label, source in zip(
            ("PIXELDRAIN", "MEGA", "ROOTZ", "MEDIAFIRE", "TERABOX", "DATANODES"),
            source_urls,
            strict=True,
        )
    )

    async def pages(url: str, **_kwargs: object) -> str:
        if "/dc/example" in url:
            return (
                '<html><body><section class="post-contents"><p>Example #1</p>'
                f"{buttons}</section></body></html>"
            )
        return (FIXTURES / "search-results.html").read_text(encoding="utf-8")

    redirects = _Redirects(source_urls)
    service = GetComicsProviderService(page_fetcher=pages, redirect_resolver=redirects)

    artifacts = await service.resolve("getcomics:L2RjL2V4YW1wbGUv")

    assert [mirror.host_kind for mirror in artifacts[0].mirrors] == [
        "pixeldrain",
        "mega",
        "rootz",
        "mediafire",
        "terabox",
        "datanodes",
    ]
    assert redirects.urls == list(source_urls)


async def test_service_drops_failed_opaque_redirect_and_keeps_valid_sibling() -> None:
    failed = "https://getcomics.org/dls/pixel"
    valid = "https://getcomics.org/dls/mega"

    async def pages(_url: str, **_kwargs: object) -> str:
        return f"""
        <html><body><section class="post-contents">
          <p>Example #1</p>
          <a class="aio-red" title="PIXELDRAIN" href="{failed}">PixelDrain</a>
          <a class="aio-red" title="MEGA" href="{valid}">MEGA</a>
        </section></body></html>
        """

    service = GetComicsProviderService(
        page_fetcher=pages,
        redirect_resolver=_Redirects(
            {
                failed: RuntimeError("redirect unavailable"),
                valid: "https://mega.nz/file/mega#key",
            }
        ),
    )

    artifacts = await service.resolve("getcomics:L2RjL2V4YW1wbGUv")

    assert [mirror.host_kind for mirror in artifacts[0].mirrors] == ["mega"]


async def test_service_bounds_parallel_opaque_redirect_resolution() -> None:
    sources = [f"https://getcomics.org/dls/{index}" for index in range(8)]
    buttons = "".join(
        f'<a class="aio-red" title="DOWNLOAD NOW" href="{source}">Download</a>'
        for source in sources
    )
    active = 0
    peak = 0

    async def pages(_url: str, **_kwargs: object) -> str:
        return (
            '<html><body><section class="post-contents"><p>Example #1</p>'
            f"{buttons}</section></body></html>"
        )

    async def redirects(url: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return f"https://files.example.test/{url.rsplit('/', 1)[-1]}.cbz"

    service = GetComicsProviderService(
        page_fetcher=pages,
        redirect_resolver=redirects,
    )

    artifacts = await service.resolve("getcomics:L2RjL2V4YW1wbGUv")

    assert len(artifacts[0].mirrors) == len(sources)
    assert peak == 4
