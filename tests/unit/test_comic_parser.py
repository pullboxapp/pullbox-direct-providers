from __future__ import annotations

from pullbox_provider_contract.comic_parser import parse_comic_title


def test_decimal_ranges_are_not_coerced_into_integer_issue_coverage() -> None:
    first = parse_comic_title("Example Heroes #1.5-2.5")
    second = parse_comic_title("Example Heroes #0.1-0.3")

    assert first.series_title == "Example Heroes"
    assert first.issue_numbers == ()
    assert second.series_title == "Example Heroes"
    assert second.issue_numbers == ()
