from __future__ import annotations

from pathlib import Path

import pytest
from pullbox_provider_annas_archive.parser import (
    AnnasArchiveLayoutError,
    parse_search_html,
)
from pullbox_provider_contract.resolver import ProviderResolverError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "annas_archive"


def test_search_parser_preserves_md5_identity_and_comic_filename_evidence() -> None:
    candidates = parse_search_html(
        (FIXTURES / "search-results.html").read_text(encoding="utf-8"),
        source_domain="annas-archive.gd",
    )

    assert [item.provider_candidate_id for item in candidates] == [
        "anna:11111111111111111111111111111111",
        "anna:22222222222222222222222222222222",
    ]
    assert candidates[0].parsed.series_title == "Example Heroes"
    assert candidates[0].parsed.issue_numbers == ["7"]
    assert candidates[0].parsed.year == 2026
    assert candidates[0].parsed.format == "cbz"
    assert candidates[0].provenance == {"layout": "search-v1", "source_kind": "metadata"}


def test_search_parser_fails_closed_when_search_shell_changes() -> None:
    with pytest.raises(AnnasArchiveLayoutError, match="layout"):
        parse_search_html("<html><main>changed</main></html>", source_domain="annas-archive.gd")


def test_search_parser_rejects_non_md5_source_identity() -> None:
    html = """
    <html><form action="/search"></form>
      <a href="/md5/not-a-hash" class="font-semibold text-lg">Example #1.cbz</a>
    </html>
    """

    assert parse_search_html(html, source_domain="annas-archive.gd") == []


@pytest.mark.parametrize(
    "html",
    [
        "<title>DDoS-Guard</title><p>Checking your browser</p>",
        "<title>Loading https://annas-archive.gd/search?q=Batman</title>",
    ],
)
def test_unfinished_search_page_is_retryable_not_a_layout_change(html: str) -> None:
    with pytest.raises(ProviderResolverError) as exc:
        parse_search_html(html, source_domain="annas-archive.gd")
    assert exc.value.code == "resolver_challenge_failed"


def test_unrecognized_page_diagnostics_do_not_log_source_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with pytest.raises(AnnasArchiveLayoutError):
        parse_search_html(
            "<title>private-query-token</title><body>cookie-secret</body>",
            source_domain="annas-archive.gd",
        )
    assert "anna_search_page_unrecognized" in caplog.text
    assert "private-query-token" not in caplog.text
    assert "cookie-secret" not in caplog.text


@pytest.mark.parametrize("title", ["Loading https://other.example/search", "Loading https://[bad"])
def test_unrelated_loading_title_remains_an_unknown_layout(title: str) -> None:
    with pytest.raises(AnnasArchiveLayoutError):
        parse_search_html(f"<title>{title}</title>", source_domain="annas-archive.gd")
