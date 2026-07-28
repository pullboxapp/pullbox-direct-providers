"""Fail-closed GetComics HTML normalization."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlsplit

from pullbox_provider_contract.comic_parser import parse_comic_title
from pullbox_provider_contract.models import (
    Artifact,
    ArtifactCoverage,
    ArtifactRoute,
    Candidate,
    Mirror,
    ParsedCandidate,
)

_SIZE = re.compile(r"\bSize\s*:\s*(\d+(?:\.\d+)?)\s*(KB|MB|GB)\b", re.IGNORECASE)
_IGNORED_TITLES = frozenset({"READ ONLINE", "VIKINGFILE"})


class GetComicsLayoutError(RuntimeError):
    """The source no longer matches a recognized safe layout."""


@dataclass(frozen=True, slots=True)
class _Link:
    href: str
    classes: frozenset[str]
    title: str
    text: str
    context_classes: frozenset[str]


@dataclass(slots=True)
class _ReleaseGroup:
    text_parts: list[str] = field(default_factory=list)
    links: list[_Link] = field(default_factory=list)


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_Link] = []
        self.all_classes: set[str] = set()
        self.text_parts: list[str] = []
        self._contexts: list[frozenset[str]] = []
        self._link: tuple[str, frozenset[str], str, frozenset[str], list[str]] | None = None
        self.release_groups: list[_ReleaseGroup] = []
        self._active_group: _ReleaseGroup | None = None
        self._block_tag: str | None = None
        self._block_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = frozenset(values.get("class", "").split())
        self.all_classes.update(classes)
        self._contexts.append(classes)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p"} and self._block_tag is None:
            self._block_tag = tag
            self._block_parts = []
        if tag == "a":
            context = frozenset().union(*self._contexts)
            self._link = (values.get("href", ""), classes, values.get("title", ""), context, [])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link is not None:
            href, classes, title, context, parts = self._link
            self.links.append(
                _Link(
                    href=href.strip(),
                    classes=classes,
                    title=title.strip(),
                    text=" ".join(" ".join(parts).split()),
                    context_classes=context,
                )
            )
            if any(class_name.startswith("aio-") for class_name in classes):
                group = self._active_group or self._start_release_group()
                group.links.append(self.links[-1])
            self._link = None
        if tag == self._block_tag:
            text = " ".join(" ".join(self._block_parts).split())
            if text and _looks_like_release_metadata(text):
                if self._active_group is None or self._active_group.links:
                    self._active_group = self._start_release_group()
                self._active_group.text_parts.append(text)
            self._block_tag = None
            self._block_parts = []
        if self._contexts:
            self._contexts.pop()

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if not normalized:
            return
        self.text_parts.append(normalized)
        if self._block_tag is not None:
            self._block_parts.append(normalized)
        if self._link is not None:
            self._link[4].append(normalized)

    def _start_release_group(self) -> _ReleaseGroup:
        group = _ReleaseGroup()
        self.release_groups.append(group)
        return group


def parse_search_html(html: str, *, source_domain: str) -> list[Candidate]:
    document = _parse(html)
    if "search-title" not in document.all_classes:
        raise GetComicsLayoutError("GetComics search layout is no longer recognized.")

    candidates: list[Candidate] = []
    seen: set[str] = set()
    for link in document.links:
        if "post-title" not in link.context_classes or not link.text:
            continue
        if not _is_source_url(link.href, source_domain) or link.href in seen:
            continue
        seen.add(link.href)
        evidence = parse_comic_title(link.text)
        candidates.append(
            Candidate(
                provider_candidate_id=_candidate_identity(link.href),
                source_reference=link.href,
                display_title=link.text,
                raw_title=link.text,
                parsed=ParsedCandidate(
                    series_title=evidence.series_title,
                    issue_numbers=list(evidence.issue_numbers),
                    volume=evidence.volume,
                    year=evidence.year,
                    format=evidence.format,
                    language="en",
                ),
                provider_confidence=_parse_confidence(evidence.issue_numbers, evidence.year),
                provenance={"layout": "wordpress-search-v1", "source_kind": "release_page"},
            )
        )
    return candidates


def parse_release_html(html: str, *, source_url: str) -> list[Artifact]:
    document = _parse(html)
    if "post-contents" not in document.all_classes:
        raise GetComicsLayoutError("GetComics release layout is no longer recognized.")

    groups = [group for group in document.release_groups if group.links]
    if not groups:
        raise GetComicsLayoutError("GetComics release page has no recognized download controls.")

    artifacts: list[Artifact] = []
    for group_index, group in enumerate(groups):
        mirrors = _normalize_mirrors(group.links, source_url=source_url)
        if not mirrors:
            continue
        group_text = " ".join(group.text_parts)
        title = _release_title(group_text, source_url)
        evidence = parse_comic_title(title)
        artifacts.append(
            Artifact(
                artifact_id=_identity("artifact", f"{source_url}:{group_index}:{title}"),
                coverage=ArtifactCoverage(
                    issue_numbers=list(evidence.issue_numbers),
                    volume=evidence.volume,
                ),
                route=ArtifactRoute.DIRECT_ARTIFACT,
                format=evidence.format,
                language="en",
                size_bytes=_parse_size(group_text),
                mirrors=mirrors,
            )
        )
    if not artifacts:
        raise GetComicsLayoutError("GetComics release page has no supported download controls.")
    return artifacts


def _normalize_mirrors(links: list[_Link], *, source_url: str) -> list[Mirror]:
    mirrors: list[Mirror] = []
    seen: set[str] = set()
    for link in links:
        label = (link.title or link.text).strip().upper()
        if label in _IGNORED_TITLES or not link.href.startswith("https://") or link.href in seen:
            continue
        seen.add(link.href)
        mirrors.append(
            Mirror(
                mirror_id=_identity("mirror", f"{source_url}:{link.href}"),
                host_kind=_host_kind(link.href),
                share_url=link.href,
            )
        )
    return mirrors


def _parse(html: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(html)
    parser.close()
    return parser


def _release_title(page_text: str, source_url: str) -> str:
    match = re.search(
        r"The Story\s*[-\u2013]\s*(.+?)(?:Language\s*:|$)",
        page_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    metadata = re.split(
        r"\b(?:Language|Image\s+Format|Year|Size)\s*:",
        page_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" -\u2013|")
    if metadata:
        return metadata
    slug = urlsplit(source_url).path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ")


def _looks_like_release_metadata(text: str) -> bool:
    evidence = parse_comic_title(text)
    return bool(
        _SIZE.search(text)
        or evidence.issue_numbers
        or evidence.volume
        or evidence.year is not None
        or evidence.format is not None
    )


def _parse_size(page_text: str) -> int | None:
    match = _SIZE.search(page_text)
    if not match:
        return None
    multiplier = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}[match.group(2).upper()]
    return round(float(match.group(1)) * multiplier)


def _host_kind(raw_url: str) -> str:
    hostname = (urlsplit(raw_url).hostname or "").casefold()
    families = (
        ("pixeldrain", ("pixeldrain.com", "pixeldrain.net")),
        ("mega", ("mega.nz", "mega.co.nz")),
        ("rootz", ("rootz.so",)),
        ("mediafire", ("mediafire.com",)),
        ("terabox", ("terabox.com", "1024terabox.com", "1024tera.com")),
        ("datanodes", ("datanodes.to",)),
    )
    for kind, domains in families:
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return kind
    return "generic_https"


def _is_source_url(raw_url: str, source_domain: str) -> bool:
    parsed = urlsplit(raw_url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    expected = source_domain.casefold().rstrip(".")
    return parsed.scheme == "https" and hostname == expected and not parsed.username


def _identity(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _candidate_identity(source_url: str) -> str:
    path = urlsplit(source_url).path
    encoded = base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")
    if not encoded or len(encoded) > 480:
        raise GetComicsLayoutError("GetComics candidate path exceeds the supported limit.")
    return f"getcomics:{encoded}"


def _parse_confidence(issue_numbers: tuple[str, ...], year: int | None) -> float:
    if issue_numbers and year is not None:
        return 0.95
    if issue_numbers or year is not None:
        return 0.85
    return 0.70
