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


def test_explicit_issue_before_release_group_keeps_issue_coverage() -> None:
    evidence = parse_comic_title("War Wolf #2 (Empire)")

    assert evidence.series_title == "War Wolf"
    assert evidence.issue_numbers == ("2",)


def test_unprefixed_issue_before_release_labels_keeps_issue_coverage() -> None:
    evidence = parse_comic_title("Absolute Batman 022 (2026) (Digital) (Shan-Empire).cbz")

    assert evidence.series_title == "Absolute Batman"
    assert evidence.issue_numbers == ("22",)
    assert evidence.year == 2026
    assert evidence.format == "cbz"
