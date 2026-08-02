from __future__ import annotations

from pullbox_provider_contract.comic_parser import parse_comic_title


def test_decimal_ranges_are_not_coerced_into_integer_issue_coverage() -> None:
    first = parse_comic_title("Example Heroes #1.5-2.5")
    second = parse_comic_title("Example Heroes #0.1-0.3")

    assert first.series_title == "Example Heroes"
    assert first.issue_numbers == ()
    assert second.series_title == "Example Heroes"
    assert second.issue_numbers == ()


def test_scene_style_numbered_pdf_extracts_group_series_and_issue() -> None:
    evidence = parse_comic_title("bb-Sacrificers.No.7.pdf")

    assert evidence.series_title == "Sacrificers"
    assert evidence.issue_numbers == ("7",)
    assert evidence.release_group == "bb"
    assert evidence.format == "pdf"
