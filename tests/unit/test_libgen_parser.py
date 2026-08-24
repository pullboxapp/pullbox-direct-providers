from __future__ import annotations

from pathlib import Path

import pytest
from pullbox_provider_libgen.parser import (
    LibGenLayoutError,
    parse_search_html,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "libgen"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def test_parse_search_html_extracts_issue_and_collection_evidence() -> None:
    records = parse_search_html(
        _fixture("search-results-v1.html"),
        source_origin="https://libgen.gl",
    )

    assert len(records) == 2
    issue, collection = records
    assert issue.md5 == "0123456789abcdef0123456789abcdef"
    assert issue.source_reference == "https://libgen.gl/file.php?id=1201"
    assert issue.display_title == "Clockwork Harbor #3"
    assert issue.raw_title == "Clockwork Harbor 003 (2024) (Digital) 1600x2400px"
    assert issue.file_id == 1201
    assert issue.edition_id == 910
    assert issue.source_series_id == 410
    assert issue.author == "Sample Creator"
    assert issue.publisher == "Example Press"
    assert issue.year == 2024
    assert issue.language == "English"
    assert issue.pages == 24
    assert issue.size_bytes == 18 * 1024 * 1024
    assert issue.extension == "cbz"

    assert collection.md5 == "fedcba9876543210fedcba9876543210"
    assert collection.display_title == "Clockwork Harbor Deluxe Collection Vol. 2"
    assert collection.raw_title == "Clockwork Harbor Deluxe Collection v02 (2025)"
    assert collection.size_bytes == round(1.5 * 1024**3)
    assert collection.extension == "pdf"
    assert collection.author is None
    assert collection.language is None
    assert collection.pages is None


def test_parse_search_html_isolates_malformed_rows_and_external_links() -> None:
    records = parse_search_html(
        _fixture("search-results-v1.html"),
        source_origin="https://libgen.gl",
    )

    assert {record.md5 for record in records} == {
        "0123456789abcdef0123456789abcdef",
        "fedcba9876543210fedcba9876543210",
    }


def test_parse_search_html_deduplicates_md5_in_source_order() -> None:
    html = _fixture("search-results-v1.html").replace(
        "fedcba9876543210fedcba9876543210",
        "0123456789abcdef0123456789abcdef",
    )

    records = parse_search_html(html, source_origin="https://libgen.gl")

    assert len(records) == 1
    assert records[0].display_title == "Clockwork Harbor #3"


def test_parse_search_html_distinguishes_zero_results_from_contract_drift() -> None:
    assert (
        parse_search_html(
            _fixture("search-zero-v1.html"),
            source_origin="https://libgen.gl",
        )
        == []
    )

    with pytest.raises(LibGenLayoutError, match="layout"):
        parse_search_html(
            _fixture("search-drift.html"),
            source_origin="https://libgen.gl",
        )


@pytest.mark.parametrize(
    "source_origin",
    [
        "http://libgen.gl",
        "https://user:secret@libgen.gl",
        "https://libgen.gl/path",
        "https://libgen.gl:444",
    ],
)
def test_parse_search_html_rejects_unsafe_source_origins(source_origin: str) -> None:
    with pytest.raises(LibGenLayoutError, match="origin"):
        parse_search_html(
            _fixture("search-results-v1.html"),
            source_origin=source_origin,
        )
