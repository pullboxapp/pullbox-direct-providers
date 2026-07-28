"""Conservative Anna's Archive search-result normalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from pullbox_provider_contract.comic_parser import parse_comic_title
from pullbox_provider_contract.models import Candidate, ParsedCandidate

_MD5_PATH = re.compile(r"\A/md5/(?P<md5>[a-f0-9]{32})\Z")


class AnnasArchiveLayoutError(RuntimeError):
    """The official search page no longer matches the supported layout."""


@dataclass(frozen=True, slots=True)
class _ResultLink:
    href: str
    classes: frozenset[str]
    text: str


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_search_form = False
        self.links: list[_ResultLink] = []
        self._link: tuple[str, frozenset[str], list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form" and values.get("action") == "/search":
            self.has_search_form = True
        if tag == "a":
            self._link = (
                values.get("href", "").strip(),
                frozenset(values.get("class", "").split()),
                [],
            )

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._link is None:
            return
        href, classes, parts = self._link
        self.links.append(
            _ResultLink(
                href=href,
                classes=classes,
                text=" ".join(" ".join(parts).split()),
            )
        )
        self._link = None

    def handle_data(self, data: str) -> None:
        if self._link is not None:
            normalized = " ".join(data.split())
            if normalized:
                self._link[2].append(normalized)


def parse_search_html(html: str, *, source_domain: str) -> list[Candidate]:
    parser = _SearchParser()
    parser.feed(html)
    parser.close()
    if not parser.has_search_form:
        raise AnnasArchiveLayoutError("Anna's Archive search layout is no longer recognized.")

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for link in parser.links:
        match = _MD5_PATH.fullmatch(link.href)
        if (
            match is None
            or "font-semibold" not in link.classes
            or "text-lg" not in link.classes
            or not link.text
        ):
            continue
        md5 = match.group("md5")
        if md5 in seen:
            continue
        seen.add(md5)
        evidence = parse_comic_title(link.text)
        candidates.append(
            Candidate(
                provider_candidate_id=f"anna:{md5}",
                source_reference=f"https://{source_domain}/md5/{md5}",
                display_title=link.text,
                raw_title=link.text,
                parsed=ParsedCandidate(
                    series_title=evidence.series_title,
                    issue_numbers=list(evidence.issue_numbers),
                    volume=evidence.volume,
                    year=evidence.year,
                    format=evidence.format,
                ),
                provider_confidence=_parse_confidence(evidence.issue_numbers, evidence.year),
                provenance={"layout": "search-v1", "source_kind": "metadata"},
            )
        )
    return candidates


def _parse_confidence(issue_numbers: tuple[str, ...], year: int | None) -> float:
    if issue_numbers and year is not None:
        return 0.90
    if issue_numbers or year is not None:
        return 0.75
    return 0.55
