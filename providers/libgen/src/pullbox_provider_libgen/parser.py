"""Fail-closed normalization for the LibGen comics search layout."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

_MD5 = re.compile(r"\A[0-9a-f]{32}\Z")
_POSITIVE_INTEGER = re.compile(r"\A[1-9][0-9]*\Z")
_SIZE = re.compile(r"\A(\d+(?:\.\d+)?)\s*(KB|MB|GB)\Z", re.IGNORECASE)
_SUPPORTED_EXTENSIONS = frozenset({"cb7", "cbr", "cbz", "pdf"})
_MAX_HTML_CHARS = 2 * 1024 * 1024
_MAX_TITLE_CHARS = 2_000


class LibGenLayoutError(RuntimeError):
    """The source no longer matches the supported comics search layout."""


@dataclass(frozen=True, slots=True)
class DiscoveredRecord:
    """Uninterpreted source evidence retained for keyed API enrichment."""

    md5: str
    source_reference: str
    display_title: str
    raw_title: str
    file_id: int | None
    edition_id: int | None
    source_series_id: int | None
    author: str | None
    publisher: str | None
    year: int | None
    language: str | None
    pages: int | None
    size_bytes: int | None
    extension: str | None


@dataclass(frozen=True, slots=True)
class _Link:
    href: str
    text: str


@dataclass(slots=True)
class _Cell:
    tag: str
    parts: list[str] = field(default_factory=list)
    secondary_parts: list[str] = field(default_factory=list)
    links: list[_Link] = field(default_factory=list)

    @property
    def text(self) -> str:
        return _normalize_text(" ".join(self.parts))

    @property
    def secondary_text(self) -> str:
        return _normalize_text(" ".join(self.secondary_parts))


@dataclass(slots=True)
class _Row:
    cells: list[_Cell] = field(default_factory=list)


class _CatalogParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.has_catalog_table = False
        self.rows: list[_Row] = []
        self._catalog_depth = 0
        self._row: _Row | None = None
        self._cell: _Cell | None = None
        self._link: tuple[str, list[str]] | None = None
        self._secondary_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "table":
            if self._catalog_depth:
                self._catalog_depth += 1
            elif values.get("id") == "tablelibgen":
                self.has_catalog_table = True
                self._catalog_depth = 1
            return
        if not self._catalog_depth:
            return
        if tag == "tr" and self._row is None:
            self._row = _Row()
        elif tag in {"td", "th"} and self._row is not None and self._cell is None:
            self._cell = _Cell(tag=tag)
        elif tag == "a" and self._cell is not None and self._link is None:
            self._link = (values.get("href", "").strip(), [])
        elif tag == "font" and self._cell is not None:
            self._secondary_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._catalog_depth:
            return
        if tag == "a" and self._link is not None and self._cell is not None:
            href, parts = self._link
            self._cell.links.append(_Link(href=href, text=_normalize_text(" ".join(parts))))
            self._link = None
        elif tag == "font" and self._secondary_depth:
            self._secondary_depth -= 1
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.cells.append(self._cell)
            self._cell = None
            self._link = None
            self._secondary_depth = 0
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None
            self._link = None
            self._secondary_depth = 0
        elif tag == "table":
            self._catalog_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell is None:
            return
        normalized = _normalize_text(data)
        if not normalized:
            return
        self._cell.parts.append(normalized)
        if self._secondary_depth:
            self._cell.secondary_parts.append(normalized)
        if self._link is not None:
            self._link[1].append(normalized)


def parse_search_html(html: str, *, source_origin: str) -> list[DiscoveredRecord]:
    """Parse recognized LibGen file rows without applying Pullbox match policy."""
    origin = _normalize_source_origin(source_origin)
    if len(html) > _MAX_HTML_CHARS:
        raise LibGenLayoutError("LibGen search response exceeds the supported size.")

    parser = _CatalogParser()
    parser.feed(html)
    parser.close()
    if not parser.has_catalog_table or not parser.rows or not _is_header_row(parser.rows[0]):
        raise LibGenLayoutError("LibGen search layout is no longer recognized.")

    records: list[DiscoveredRecord] = []
    seen: set[str] = set()
    for row in parser.rows[1:]:
        try:
            record = _parse_record(row, origin=origin)
        except (ValueError, OverflowError):
            continue
        if record is None or record.md5 in seen:
            continue
        seen.add(record.md5)
        records.append(record)
    return records


def _parse_record(row: _Row, *, origin: str) -> DiscoveredRecord | None:
    if len(row.cells) != 9 or any(cell.tag != "td" for cell in row.cells):
        return None
    identity, author, publisher, year, language, pages, size, extension, mirrors = row.cells
    get_url, md5 = _get_reference(mirrors.links, origin=origin)
    if get_url is None or md5 is None:
        return None

    file_url, file_id = _id_reference(size.links, origin=origin, path="/file.php")
    source_reference = file_url or get_url
    display_title = _display_title(identity.links)
    raw_title = identity.secondary_text or display_title
    if not display_title or not raw_title:
        return None
    if len(display_title) > _MAX_TITLE_CHARS or len(raw_title) > _MAX_TITLE_CHARS:
        return None

    return DiscoveredRecord(
        md5=md5,
        source_reference=source_reference,
        display_title=display_title,
        raw_title=raw_title,
        file_id=file_id,
        edition_id=_linked_id(identity.links, path="edition.php"),
        source_series_id=_linked_id(identity.links, path="series.php"),
        author=_optional_text(author.text),
        publisher=_optional_text(publisher.text),
        year=_optional_year(year.text),
        language=_optional_text(language.text),
        pages=_optional_integer(pages.text),
        size_bytes=_optional_size(size.text),
        extension=_optional_extension(extension.text),
    )


def _is_header_row(row: _Row) -> bool:
    if not row.cells or any(cell.tag != "th" for cell in row.cells):
        return False
    text = " ".join(cell.text.casefold() for cell in row.cells)
    return all(label in text for label in ("title", "year", "language", "size", "ext", "mirrors"))


def _display_title(links: list[_Link]) -> str:
    series = next((link.text for link in links if _link_path(link.href) == "series.php"), "")
    edition = next((link.text for link in links if _link_path(link.href) == "edition.php"), "")
    if series and edition and not edition.casefold().startswith(series.casefold()):
        return _normalize_text(f"{series} {edition}")
    return edition or series


def _get_reference(links: list[_Link], *, origin: str) -> tuple[str | None, str | None]:
    for link in links:
        reference = _same_origin_reference(link.href, origin=origin, path="/get.php", key="md5")
        if reference is None:
            continue
        url, value = reference
        md5 = value.casefold()
        if _MD5.fullmatch(md5):
            return urlunsplit((*urlsplit(url)[:3], f"md5={md5}", "")), md5
    return None, None


def _id_reference(
    links: list[_Link],
    *,
    origin: str,
    path: str,
) -> tuple[str | None, int | None]:
    for link in links:
        reference = _same_origin_reference(link.href, origin=origin, path=path, key="id")
        if reference is None:
            continue
        _, value = reference
        if _POSITIVE_INTEGER.fullmatch(value):
            identifier = int(value)
            return f"{origin}{path}?id={identifier}", identifier
    return None, None


def _same_origin_reference(
    raw_href: str,
    *,
    origin: str,
    path: str,
    key: str,
) -> tuple[str, str] | None:
    try:
        parsed_origin = urlsplit(origin)
        parsed = urlsplit(urljoin(f"{origin}/", raw_href))
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != parsed_origin.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path != path
        or parsed.fragment
    ):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if set(query) != {key} or len(query[key]) != 1:
        return None
    return urlunsplit(("https", parsed.hostname, path, parsed.query, "")), query[key][0]


def _linked_id(links: list[_Link], *, path: str) -> int | None:
    for link in links:
        parsed = urlsplit(link.href)
        if parsed.scheme or parsed.netloc or parsed.path.lstrip("/") != path:
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        values = query.get("id", [])
        if len(values) == 1 and _POSITIVE_INTEGER.fullmatch(values[0]):
            return int(values[0])
    return None


def _link_path(raw_href: str) -> str:
    return urlsplit(raw_href).path.lstrip("/")


def _optional_text(value: str) -> str | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    if len(normalized) > 500:
        raise ValueError("source text exceeds the supported bound")
    return normalized


def _optional_integer(value: str) -> int | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    if not _POSITIVE_INTEGER.fullmatch(normalized):
        raise ValueError("source integer is malformed")
    return int(normalized)


def _optional_year(value: str) -> int | None:
    parsed = _optional_integer(value)
    if parsed is not None and not 1800 <= parsed <= 2200:
        raise ValueError("source year is outside the supported bound")
    return parsed


def _optional_size(value: str) -> int | None:
    normalized = _normalize_text(value)
    if not normalized:
        return None
    match = _SIZE.fullmatch(normalized)
    if match is None:
        raise ValueError("source size is malformed")
    multiplier = {"kb": 1024, "mb": 1024**2, "gb": 1024**3}[match.group(2).casefold()]
    return round(float(match.group(1)) * multiplier)


def _optional_extension(value: str) -> str | None:
    normalized = _normalize_text(value).casefold().lstrip(".")
    if not normalized or normalized == "unknown":
        return None
    if normalized not in _SUPPORTED_EXTENSIONS:
        raise ValueError("source extension is unsupported")
    return normalized


def _normalize_source_origin(raw_origin: str) -> str:
    try:
        parsed = urlsplit(raw_origin.strip())
        port = parsed.port
    except ValueError as exc:
        raise LibGenLayoutError("LibGen source origin is malformed.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise LibGenLayoutError("LibGen source origin is unsafe.")
    return urlunsplit(("https", parsed.hostname.casefold().rstrip("."), "", "", ""))


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
