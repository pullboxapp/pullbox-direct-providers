"""Shared collection search-term semantics for direct providers."""

from __future__ import annotations

import re

COLLECTION_ISSUE_TYPES = frozenset(
    {"compendium", "deluxe", "gn", "hc", "ogn", "omnibus", "tpb", "volume"}
)
_COLLECTION_PREFIX_LABELS = ("volume", "vol", "book", "part")
_COLLECTION_PREFIX_SEPARATORS = frozenset({":", "-", "\u2013", "\u2014"})
_NUMBER_ONLY_TITLE_RE = re.compile(
    r"^(?:issue|no\.?|number|#|vol(?:ume)?\.?|book|part)?\s*#?\d+(?:\.\d+)?$",
    re.IGNORECASE,
)
_GENERIC_COLLECTION_TITLES = frozenset(
    {
        "gn",
        "graphic novel",
        "hardcover",
        "hc",
        "hc tpb",
        "issue",
        "ogn",
        "original graphic novel",
        "sc",
        "softcover",
        "tpb",
        "trade paperback",
        "vol",
        "volume",
    }
)


def is_collection_intent(issue_type: str | None) -> bool:
    return (issue_type or "").casefold() in COLLECTION_ISSUE_TYPES


def _collection_prefix_parts(title: str) -> tuple[str, str, str] | None:
    """Parse a collection prefix without backtracking over user-provided text."""
    folded = title.casefold()
    label = next(
        (candidate for candidate in _COLLECTION_PREFIX_LABELS if folded.startswith(candidate)),
        None,
    )
    if label is None:
        return None

    cursor = len(label)
    if cursor < len(title) and title[cursor] == ".":
        cursor += 1
    while cursor < len(title) and title[cursor].isspace():
        cursor += 1
    if cursor < len(title) and title[cursor] == "#":
        cursor += 1

    number_start = cursor
    while cursor < len(title) and title[cursor].isdecimal():
        cursor += 1
    if cursor == number_start:
        return None
    if cursor + 1 < len(title) and title[cursor] == "." and title[cursor + 1].isdecimal():
        cursor += 1
        while cursor < len(title) and title[cursor].isdecimal():
            cursor += 1
    number = title[number_start:cursor]

    while cursor < len(title) and title[cursor].isspace():
        cursor += 1
    if cursor < len(title) and title[cursor] in _COLLECTION_PREFIX_SEPARATORS:
        cursor += 1
    while cursor < len(title) and title[cursor].isspace():
        cursor += 1
    return label, number, title[cursor:]


def collection_title_fragment(value: str | None) -> str | None:
    """Return meaningful title text with canonical volume syntax."""
    title = re.sub(r"\s+", " ", value or "").strip()
    if not title:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    if normalized in _GENERIC_COLLECTION_TITLES or _NUMBER_ONLY_TITLE_RE.fullmatch(title):
        return None

    prefix = _collection_prefix_parts(title)
    if prefix is None:
        return title
    label, number, subtitle = prefix
    subtitle = subtitle.strip()
    if not subtitle:
        return None
    canonical_label = "Vol" if label.startswith("vol") else label.title()
    return " ".join((canonical_label, number, subtitle))


def collection_title_number(value: str | None) -> str | None:
    """Return an explicit collection ordinal even when the title has no subtitle."""
    title = re.sub(r"\s+", " ", value or "").strip()
    if not title:
        return None
    prefix = _collection_prefix_parts(title)
    return prefix[1] if prefix is not None else None
