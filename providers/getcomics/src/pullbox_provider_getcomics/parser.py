"""Fail-closed GetComics HTML normalization."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import TYPE_CHECKING
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
_QUALITY_VARIANT = re.compile(r"\s*\((?:HD|SD)[-\s]*Digital\)\s*", re.IGNORECASE)
_CONTROL_PREFIX = re.compile(
    r"^(?:(?:DOWNLOAD NOW|READ ONLINE|PIXELDRAIN|MEGA|MEGANZ|ROOTZ|MEDIAFIRE|"
    r"TERABOX|DATANODES|VIKINGFILE)\s+)+",
    re.IGNORECASE,
)
_IGNORED_TITLES = frozenset({"READ ONLINE", "VIKINGFILE"})
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source"}
)
_HOST_TITLE_KINDS = {
    "PIXELDRAIN": "pixeldrain",
    "MEGA": "mega",
    "MEGANZ": "mega",
    "ROOTZ": "rootz",
    "MEDIAFIRE": "mediafire",
    "TERABOX": "terabox",
    "DATANODES": "datanodes",
}

if TYPE_CHECKING:
    from collections.abc import Mapping


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
        self._contexts: list[tuple[str, frozenset[str]]] = []
        self._link: tuple[str, frozenset[str], str, frozenset[str], list[str]] | None = None
        self.release_groups: list[_ReleaseGroup] = []
        self._active_group: _ReleaseGroup | None = None
        self._block_tag: str | None = None
        self._block_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = frozenset(values.get("class", "").split())
        self.all_classes.update(classes)
        if tag not in _VOID_TAGS:
            self._contexts.append((tag, classes))
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p"} and self._block_tag is None:
            self._block_tag = tag
            self._block_parts = []
        if tag == "a":
            context = frozenset().union(*(classes for _, classes in self._contexts))
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
        for index in range(len(self._contexts) - 1, -1, -1):
            if self._contexts[index][0] == tag:
                del self._contexts[index:]
                break

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
        if not _is_source_url(link.href, source_domain):
            continue
        source_reference = _canonical_source_url(link.href)
        candidate_identity = _candidate_identity(source_reference)
        if candidate_identity in seen:
            continue
        seen.add(candidate_identity)
        evidence = parse_comic_title(link.text)
        candidates.append(
            Candidate(
                provider_candidate_id=candidate_identity,
                source_reference=source_reference,
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


def parse_release_html(
    html: str,
    *,
    source_url: str,
    resolved_links: Mapping[str, str | None] | None = None,
    require_resolved_source_links: bool = False,
) -> list[Artifact]:
    document = _parse(html)
    if "post-contents" not in document.all_classes:
        raise GetComicsLayoutError("GetComics release layout is no longer recognized.")

    groups = [group for group in document.release_groups if group.links]
    if not groups:
        raise GetComicsLayoutError("GetComics release page has no recognized download controls.")

    artifacts: list[Artifact] = []
    for group in groups:
        mirrors = _normalize_mirrors(
            group.links,
            source_url=source_url,
            resolved_links=resolved_links or {},
            require_resolved_source_links=require_resolved_source_links,
        )
        if not mirrors:
            continue
        group_text = " ".join(group.text_parts)
        title = _release_title(group.text_parts, source_url)
        evidence = parse_comic_title(_QUALITY_VARIANT.sub(" ", title))
        size_bytes = _parse_size(group_text)
        artifacts.append(
            Artifact(
                artifact_id=_identity("artifact", f"{source_url}:{title}"),
                coverage=ArtifactCoverage(
                    issue_numbers=list(evidence.issue_numbers),
                    volume=evidence.volume,
                    description=evidence.series_title,
                ),
                route=ArtifactRoute.DIRECT_ARTIFACT,
                format=evidence.format,
                language="en",
                size_bytes=size_bytes,
                size_is_estimate=size_bytes is not None,
                mirrors=mirrors,
            )
        )
    if not artifacts:
        raise GetComicsLayoutError("GetComics release page has no supported download controls.")
    return artifacts


def extract_source_redirect_links(html: str, *, source_domain: str) -> list[str]:
    """Return stable GetComics download-wrapper URLs found in recognized controls."""
    document = _parse(html)
    return list(
        dict.fromkeys(
            link.href
            for link in document.links
            if any(class_name.startswith("aio-") for class_name in link.classes)
            and (link.title or link.text).strip().upper() not in _IGNORED_TITLES
            and _is_source_redirect(link.href, source_domain)
        )
    )


def _normalize_mirrors(
    links: list[_Link],
    *,
    source_url: str,
    resolved_links: Mapping[str, str | None],
    require_resolved_source_links: bool,
) -> list[Mirror]:
    mirrors: list[Mirror] = []
    seen: set[str] = set()
    for link in links:
        label = (link.title or link.text).strip().upper()
        if label in _IGNORED_TITLES or not link.href.startswith("https://") or link.href in seen:
            continue
        seen.add(link.href)
        destination = link.href
        if _is_source_redirect(link.href, urlsplit(source_url).hostname or ""):
            if link.href in resolved_links:
                resolved = resolved_links[link.href]
                if resolved is None:
                    continue
                destination = resolved
            elif require_resolved_source_links:
                continue
        host_kind = _host_kind(destination)
        title_host_kind = _title_host_kind(label)
        if title_host_kind is not None and title_host_kind != host_kind:
            continue
        mirrors.append(
            Mirror(
                mirror_id=_identity("mirror", f"{source_url}:{link.href}"),
                host_kind=host_kind,
                share_url=destination,
            )
        )
    return mirrors


def _parse(html: str) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(html)
    parser.close()
    return parser


def _release_title(text_parts: list[str], source_url: str) -> str:
    for text in reversed(text_parts):
        if not _looks_like_release_metadata(text):
            continue
        cleaned = _CONTROL_PREFIX.sub("", text.strip())
        metadata = re.split(
            r"\b(?:Language|Image\s+Format|Year|Size)\s*:",
            cleaned,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -\u2013|")
        if metadata:
            return metadata

    page_text = " ".join(text_parts)
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
        (
            "terabox",
            (
                "1024terabox.com",
                "1024tera.com",
                "4funbox.com",
                "dubox.com",
                "mirrobox.com",
                "momerybox.com",
                "terabox.com",
                "terabox.app",
                "terabox.link",
                "teraboxapp.com",
                "teraboxlink.com",
                "terasharefile.com",
            ),
        ),
        ("datanodes", ("datanodes.to",)),
    )
    for kind, domains in families:
        if any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains):
            return kind
    return "generic_https"


def _title_host_kind(label: str) -> str | None:
    normalized = re.sub(r"[^A-Z0-9]+", "", label.upper())
    return _HOST_TITLE_KINDS.get(normalized)


def _is_source_redirect(raw_url: str, source_domain: str) -> bool:
    parsed = urlsplit(raw_url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    expected = source_domain.casefold().rstrip(".")
    return (
        parsed.scheme == "https"
        and hostname == expected
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/dls/")
    )


def _is_source_url(raw_url: str, source_domain: str) -> bool:
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError:
        return False
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    expected = source_domain.casefold().rstrip(".")
    return (
        parsed.scheme == "https"
        and hostname == expected
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _canonical_source_url(raw_url: str) -> str:
    parsed = urlsplit(raw_url)
    hostname = parsed.hostname
    if hostname is None:
        raise GetComicsLayoutError("GetComics returned an invalid release URL.")
    return f"https://{hostname}{parsed.path or '/'}"


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
