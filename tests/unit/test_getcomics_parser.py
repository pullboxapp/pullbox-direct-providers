from __future__ import annotations

from pathlib import Path

import pytest
from pullbox_provider_getcomics.parser import (
    GetComicsLayoutError,
    extract_source_redirect_links,
    parse_release_html,
    parse_search_html,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "getcomics"


def test_search_parser_normalizes_issue_and_collection_candidates() -> None:
    candidates = parse_search_html(
        (FIXTURES / "search-results.html").read_text(encoding="utf-8"),
        source_domain="getcomics.org",
    )

    assert [candidate.parsed.series_title for candidate in candidates] == [
        "Example Heroes",
        "Example Heroes",
    ]
    assert candidates[0].parsed.issue_numbers == ["7"]
    assert candidates[0].parsed.year == 2026
    assert candidates[1].parsed.volume == "1"
    assert candidates[1].parsed.format == "tpb"
    assert candidates[0].provider_candidate_id.startswith("getcomics:")
    assert candidates[0].source_reference == ("https://getcomics.org/dc/example-heroes-7-2026/")


def test_release_parser_groups_supported_mirrors_and_excludes_non_downloads() -> None:
    artifacts = parse_release_html(
        (FIXTURES / "release.html").read_text(encoding="utf-8"),
        source_url="https://getcomics.org/dc/example-heroes-7-2026/",
    )

    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.coverage.issue_numbers == ["7"]
    assert artifact.size_bytes == 57 * 1024 * 1024
    assert [mirror.host_kind for mirror in artifact.mirrors] == [
        "generic_https",
        "pixeldrain",
        "mega",
    ]
    assert all("viking" not in (mirror.share_url or "") for mirror in artifact.mirrors)
    assert all("read.example" not in (mirror.share_url or "") for mirror in artifact.mirrors)
    assert all(mirror.source_headers == {} for mirror in artifact.mirrors)


def test_release_parser_keeps_nested_collection_artifacts_independent() -> None:
    html = """
    <html><body><section class="post-contents">
      <p><strong>Example Heroes #1-3 (2024)</strong><br>Size : 120 MB</p>
      <div><a class="aio-red" title="DOWNLOAD NOW"
        href="https://getcomics.org/dls/range">Range</a></div>
      <p><strong>Example Heroes Vol. 2 (TPB) (2025)</strong><br>Size : 450 MB</p>
      <div><a class="aio-orange" title="PIXELDRAIN"
        href="https://pixeldrain.com/u/volume">Volume</a></div>
      <div><a class="aio-blue" title="MEGA"
        href="https://mega.nz/file/volume#fragment">Mirror</a></div>
    </section></body></html>
    """

    artifacts = parse_release_html(
        html,
        source_url="https://getcomics.org/dc/example-heroes-collection/",
    )

    assert len(artifacts) == 2
    assert artifacts[0].coverage.issue_numbers == ["1", "2", "3"]
    assert artifacts[0].size_bytes == 120 * 1024 * 1024
    assert [mirror.host_kind for mirror in artifacts[0].mirrors] == ["generic_https"]
    assert artifacts[1].coverage.volume == "2"
    assert artifacts[1].format == "tpb"
    assert artifacts[1].size_bytes == 450 * 1024 * 1024
    assert [mirror.host_kind for mirror in artifacts[1].mirrors] == ["pixeldrain", "mega"]


def test_search_parser_accepts_real_empty_results_but_fails_closed_on_layout_drift() -> None:
    assert (
        parse_search_html(
            '<html><h1 class="search-title">Search Result</h1></html>',
            source_domain="getcomics.org",
        )
        == []
    )

    with pytest.raises(GetComicsLayoutError, match="layout"):
        parse_search_html("<html><main>changed</main></html>", source_domain="getcomics.org")


def test_release_parser_fails_closed_without_download_controls() -> None:
    with pytest.raises(GetComicsLayoutError, match="download controls"):
        parse_release_html(
            '<html><section class="post-contents"><h2>Example Heroes #7</h2></section></html>',
            source_url="https://getcomics.org/dc/example-heroes-7-2026/",
        )


def test_redirect_extraction_skips_intentionally_unsupported_controls() -> None:
    html = """
    <html><body><section class="post-contents">
      <a class="aio-red" title="PIXELDRAIN" href="https://getcomics.org/dls/pixel">Pixel</a>
      <a class="aio-red" title="VIKINGFILE" href="https://getcomics.org/dls/viking">Viking</a>
      <a class="aio-red" title="READ ONLINE" href="https://getcomics.org/dls/read">Read</a>
    </section></body></html>
    """

    assert extract_source_redirect_links(html, source_domain="getcomics.org") == [
        "https://getcomics.org/dls/pixel"
    ]


def test_release_parser_uses_all_supported_button_titles_for_opaque_links() -> None:
    labels = {
        "PIXELDRAIN": "https://pixeldrain.com/u/pixel",
        "MEGA": "https://mega.nz/file/mega#key",
        "ROOTZ": "https://rootz.so/file/rootz",
        "MEDIAFIRE": "https://www.mediafire.com/file/media/file.cbz",
        "TERABOX": "https://terabox.link/s/terabox",
        "DATANODES": "https://datanodes.to/download/data",
    }
    links = "".join(
        f'<a class="aio-red" title="{label}" href="https://getcomics.org/dls/{index}">{label}</a>'
        for index, label in enumerate(labels)
    )
    resolved_links = {
        f"https://getcomics.org/dls/{index}": destination
        for index, destination in enumerate(labels.values())
    }

    html = (
        '<html><body><section class="post-contents"><p>Example #1</p>'
        f"{links}</section></body></html>"
    )
    artifacts = parse_release_html(
        html,
        source_url="https://getcomics.org/dc/example-1/",
        resolved_links=resolved_links,
        require_resolved_source_links=True,
    )

    assert [mirror.host_kind for mirror in artifacts[0].mirrors] == [
        "pixeldrain",
        "mega",
        "rootz",
        "mediafire",
        "terabox",
        "datanodes",
    ]
    assert [mirror.share_url for mirror in artifacts[0].mirrors] == list(labels.values())


def test_release_parser_rejects_title_destination_mismatch_without_losing_siblings() -> None:
    pixel_source = "https://getcomics.org/dls/pixel"
    generic_source = "https://getcomics.org/dls/generic"
    html = f"""
    <html><body><section class="post-contents">
      <p>Example #1</p>
      <a class="aio-red" title="PIXELDRAIN" href="{pixel_source}">PixelDrain</a>
      <a class="aio-red" title="DOWNLOAD NOW" href="{generic_source}">Download</a>
    </section></body></html>
    """

    artifacts = parse_release_html(
        html,
        source_url="https://getcomics.org/dc/example-1/",
        resolved_links={
            pixel_source: "https://mega.nz/file/wrong#key",
            generic_source: "https://files.example.test/example.cbz",
        },
        require_resolved_source_links=True,
    )

    assert len(artifacts[0].mirrors) == 1
    assert artifacts[0].mirrors[0].host_kind == "generic_https"
    assert artifacts[0].mirrors[0].share_url == "https://files.example.test/example.cbz"


def test_release_parser_keeps_mirror_identity_stable_after_redirect_resolution() -> None:
    source = "https://getcomics.org/dls/pixel"
    html = f"""
    <html><body><section class="post-contents">
      <p>Example #1</p>
      <a class="aio-red" title="PIXELDRAIN" href="{source}">PixelDrain</a>
    </section></body></html>
    """

    first = parse_release_html(
        html,
        source_url="https://getcomics.org/dc/example-1/",
        resolved_links={source: "https://pixeldrain.com/u/first"},
        require_resolved_source_links=True,
    )
    second = parse_release_html(
        html,
        source_url="https://getcomics.org/dc/example-1/",
        resolved_links={source: "https://pixeldrain.com/u/second"},
        require_resolved_source_links=True,
    )

    assert first[0].mirrors[0].mirror_id == second[0].mirrors[0].mirror_id
