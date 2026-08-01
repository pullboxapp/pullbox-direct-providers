"""Conservative filename evidence parsing shared by official providers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

_YEAR = re.compile(r"\((?P<year>18\d{2}|19\d{2}|20\d{2}|21\d{2})\)")
_FORMAT = re.compile(
    r"\((?P<format>TPB|HC|Omnibus|Compendium|GN|OGN|One[- ]Shot|Special)\)",
    re.IGNORECASE,
)
_VOLUME = re.compile(r"\bvol(?:ume)?\.?\s*(?P<volume>\d+)\b", re.IGNORECASE)
_HASH_RANGE = re.compile(
    r"\s+#?\s*(?P<start>\d+(?:\.\d+)?)\s*[-\u2013]\s*#?\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*$"
)
_HASH_ISSUE = re.compile(r"\s+#\s*(?P<issue>\d+(?:\.\d+)?[A-Za-z]?)\s*$")
_TRAILING_ISSUE = re.compile(r"\s+(?P<issue>\d{1,5}(?:\.\d+)?[A-Za-z]?)\s*$")
_EXTENSION = re.compile(r"\.(?P<extension>cbz|cbr|cb7|pdf)\s*$", re.IGNORECASE)
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ComicTitleEvidence:
    series_title: str
    issue_numbers: tuple[str, ...]
    volume: str | None
    year: int | None
    format: str | None


def parse_comic_title(raw_title: str) -> ComicTitleEvidence:
    """Extract only explicit suffix evidence; Pullbox remains the matcher."""
    title = _SPACE.sub(" ", unescape(raw_title)).strip()
    extension_match = _EXTENSION.search(title)
    extension = extension_match.group("extension").casefold() if extension_match else None
    title = _EXTENSION.sub("", title).strip()

    year_matches = list(_YEAR.finditer(title))
    year = int(year_matches[-1].group("year")) if year_matches else None
    title_without_year = _YEAR.sub("", title).strip()

    format_match = _FORMAT.search(title_without_year)
    format_value = (
        format_match.group("format").casefold().replace(" ", "_").replace("-", "_")
        if format_match
        else extension
    )
    working = _FORMAT.sub("", title_without_year).strip()

    volume_match = _VOLUME.search(working)
    volume = volume_match.group("volume") if volume_match else None
    if volume_match:
        working = f"{working[: volume_match.start()]} {working[volume_match.end() :]}".strip()

    issue_numbers: tuple[str, ...] = ()
    range_match = _HASH_RANGE.search(working)
    if range_match:
        start_value = range_match.group("start")
        end_value = range_match.group("end")
        working = working[: range_match.start()].strip()
        if "." not in start_value and "." not in end_value:
            start = int(start_value)
            end = int(end_value)
            if start <= end and end - start < 100:
                issue_numbers = tuple(str(value) for value in range(start, end + 1))
    if not issue_numbers:
        issue_match = _HASH_ISSUE.search(working) or _TRAILING_ISSUE.search(working)
        if issue_match:
            issue_numbers = (_normalize_issue(issue_match.group("issue")),)
            working = working[: issue_match.start()].strip()

    series_title = working.strip(" -\u2013:()")
    return ComicTitleEvidence(
        series_title=series_title or title,
        issue_numbers=issue_numbers,
        volume=volume,
        year=year,
        format=format_value,
    )


def _normalize_issue(value: str) -> str:
    numeric = value.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
    suffix = value[len(numeric) :].casefold()
    if "." in numeric:
        numeric = numeric.rstrip("0").rstrip(".")
    else:
        numeric = str(int(numeric))
    return f"{numeric}{suffix}"
