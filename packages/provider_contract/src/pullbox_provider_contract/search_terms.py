"""Shared collection search-term semantics for direct providers."""

from __future__ import annotations

import re

COLLECTION_ISSUE_TYPES = frozenset(
    {"compendium", "deluxe", "gn", "hc", "ogn", "omnibus", "tpb", "volume"}
)
_COLLECTION_PREFIX_RE = re.compile(
    r"^(?P<label>vol(?:ume)?|book|part)\.?\s*#?(?P<number>\d+(?:\.\d+)?)"
    r"\s*(?::|[-\u2013\u2014])?\s*(?P<subtitle>.*)$",
    re.IGNORECASE,
)
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


def collection_title_fragment(value: str | None) -> str | None:
    """Return meaningful title text with canonical volume syntax."""
    title = re.sub(r"\s+", " ", value or "").strip()
    if not title:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    if normalized in _GENERIC_COLLECTION_TITLES or _NUMBER_ONLY_TITLE_RE.fullmatch(title):
        return None

    prefix = _COLLECTION_PREFIX_RE.match(title)
    if prefix is None:
        return title
    subtitle = prefix.group("subtitle").strip()
    if not subtitle:
        return None
    label = prefix.group("label").casefold()
    canonical_label = "Vol" if label.startswith("vol") else label.title()
    return " ".join((canonical_label, prefix.group("number"), subtitle))
