"""Keyed LibGen JSON validation and provider-candidate mapping."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from pullbox_provider_contract.comic_parser import normalize_issue, parse_comic_title
from pullbox_provider_contract.models import Candidate, ParsedCandidate

from pullbox_provider_libgen.cache import BoundedTTLCache
from pullbox_provider_libgen.parser import DiscoveredRecord

_MD5 = re.compile(r"\A[0-9a-f]{32}\Z")
_ISSUE = re.compile(r"\A\d+(?:\.\d+)?[A-Za-z]?\Z")
_INTEGER = re.compile(r"\A(?:0|[1-9][0-9]*)\Z")
_SUPPORTED_EXTENSIONS = frozenset({"cb7", "cbr", "cbz", "pdf"})
_MAX_JSON_BYTES = 512 * 1024
_FILE_FIELDS = "f_id,md5,pages,filesize,extension,locator,broken,visible,comics_id"
_EDITION_FIELDS = (
    "e_id,title,series_name,title_add,publisher,year,cover_url,"
    "issue_number,issue_volume,type,visible"
)

MetadataFetcher = Callable[[str], Awaitable[str]]


class LibGenMetadataError(RuntimeError):
    """Keyed source metadata is malformed, unavailable, or contradictory."""


@dataclass(frozen=True, slots=True)
class FileMetadata:
    file_id: int
    md5: str
    pages: int | None
    size_bytes: int | None
    extension: str | None
    locator_filename: str | None
    comics_id: int | None
    edition_id: int | None


@dataclass(frozen=True, slots=True)
class EditionMetadata:
    edition_id: int
    title: str | None
    series_name: str | None
    title_add: str | None
    publisher: str | None
    year: int | None
    cover_url: str | None
    issue_number: str | None
    issue_volume: str | None
    edition_type: str | None


class LibGenMetadataEnricher:
    """Fetch and cache only metadata keyed by a retained discovery row."""

    def __init__(
        self,
        *,
        fetcher: MetadataFetcher,
        cache: BoundedTTLCache[str, FileMetadata | EditionMetadata] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._cache = cache or BoundedTTLCache(
            max_entries=2_048,
            ttl_seconds=60 * 60,
            negative_ttl_seconds=2 * 60,
        )

    async def enrich(self, discovered: DiscoveredRecord) -> Candidate | None:
        origin = _source_origin(discovered.source_reference)
        file_key = ":".join(
            (
                "file",
                origin,
                discovered.md5,
                str(discovered.file_id or ""),
                str(discovered.edition_id or ""),
                str(discovered.size_bytes or ""),
                str(discovered.size_tolerance_bytes or ""),
                discovered.extension or "",
            )
        )
        file_lookup = self._cache.get(file_key)
        if file_lookup.hit:
            if not isinstance(file_lookup.value, FileMetadata):
                return None
            file_metadata = file_lookup.value
        else:
            try:
                payload = await self._fetcher(_file_metadata_url(origin, discovered.md5))
                file_metadata = parse_file_metadata(payload, expected=discovered)
            except LibGenMetadataError:
                self._cache.set(file_key, None)
                return None
            self._cache.set(file_key, file_metadata)

        edition_metadata: EditionMetadata | None = None
        if file_metadata.edition_id is not None:
            edition_key = f"edition:{origin}:{file_metadata.edition_id}:{file_metadata.file_id}"
            edition_lookup = self._cache.get(edition_key)
            if edition_lookup.hit:
                if isinstance(edition_lookup.value, EditionMetadata):
                    edition_metadata = edition_lookup.value
            else:
                try:
                    payload = await self._fetcher(
                        _edition_metadata_url(origin, file_metadata.edition_id)
                    )
                    edition_metadata = parse_edition_metadata(
                        payload,
                        expected_edition_id=file_metadata.edition_id,
                        expected_file_id=file_metadata.file_id,
                    )
                except LibGenMetadataError:
                    self._cache.set(edition_key, None)
                else:
                    self._cache.set(edition_key, edition_metadata)
        return build_candidate(discovered, file_metadata, edition_metadata)


def parse_file_metadata(payload: str | bytes, *, expected: DiscoveredRecord) -> FileMetadata:
    return _parse_file_metadata(
        payload,
        expected_md5=expected.md5,
        expected_file_id=expected.file_id,
        expected_edition_id=expected.edition_id,
        expected_size_bytes=expected.size_bytes,
        expected_size_tolerance_bytes=expected.size_tolerance_bytes,
        expected_extension=expected.extension,
    )


def parse_file_metadata_by_md5(payload: str | bytes, *, expected_md5: str) -> FileMetadata:
    """Rebuild file metadata for stateless resolve after a provider restart."""
    md5 = _required_md5(expected_md5)
    return _parse_file_metadata(
        payload,
        expected_md5=md5,
        expected_file_id=None,
        expected_edition_id=None,
        expected_size_bytes=None,
        expected_size_tolerance_bytes=None,
        expected_extension=None,
    )


def _parse_file_metadata(
    payload: str | bytes,
    *,
    expected_md5: str,
    expected_file_id: int | None,
    expected_edition_id: int | None,
    expected_size_bytes: int | None,
    expected_size_tolerance_bytes: int | None,
    expected_extension: str | None,
) -> FileMetadata:
    records = _decode_keyed_object(payload)
    if expected_file_id is not None:
        key = str(expected_file_id)
        record = records.get(key)
        if not isinstance(record, Mapping):
            raise LibGenMetadataError("LibGen file identity conflicts with discovery metadata.")
    else:
        matches = [
            (key, value)
            for key, value in records.items()
            if isinstance(value, Mapping) and str(value.get("md5", "")).casefold() == expected_md5
        ]
        if len(matches) != 1:
            raise LibGenMetadataError("LibGen file identity is missing or ambiguous.")
        key, record = matches[0]

    file_id = _positive_identifier(key, label="file")
    if expected_file_id is not None and file_id != expected_file_id:
        raise LibGenMetadataError("LibGen file identity conflicts with discovery metadata.")
    md5 = _required_md5(record.get("md5"))
    if md5 != expected_md5:
        raise LibGenMetadataError("LibGen MD5 conflicts with discovery metadata.")
    if _source_flag(record.get("visible"), default=True) is False:
        raise LibGenMetadataError("LibGen file is not visible.")
    if _source_flag(record.get("broken"), default=False) is True:
        raise LibGenMetadataError("LibGen file is marked broken.")

    size_bytes = _optional_integer(record.get("filesize"), label="size")
    if (
        expected_size_bytes is not None
        and size_bytes is not None
        and abs(size_bytes - expected_size_bytes) > (expected_size_tolerance_bytes or 0)
    ):
        raise LibGenMetadataError("LibGen file size conflicts with discovery metadata.")
    extension = _optional_extension(record.get("extension"))
    if expected_extension is not None and extension is not None and extension != expected_extension:
        raise LibGenMetadataError("LibGen file extension conflicts with discovery metadata.")

    relations = record.get("editions", {})
    edition_ids = _relation_ids(relations, field="e_id", label="edition")
    edition_id: int | None
    if expected_edition_id is not None:
        if edition_ids and expected_edition_id not in edition_ids:
            raise LibGenMetadataError("LibGen edition identity conflicts with discovery metadata.")
        edition_id = expected_edition_id
    else:
        edition_id = min(edition_ids) if edition_ids else None

    return FileMetadata(
        file_id=file_id,
        md5=md5,
        pages=_optional_integer(record.get("pages"), label="pages"),
        size_bytes=size_bytes,
        extension=extension,
        locator_filename=_locator_filename(record.get("locator")),
        comics_id=_optional_identifier(record.get("comics_id"), label="comics"),
        edition_id=edition_id,
    )


def parse_edition_metadata(
    payload: str | bytes,
    *,
    expected_edition_id: int | None,
    expected_file_id: int,
) -> EditionMetadata | None:
    if expected_edition_id is None:
        return None
    records = _decode_keyed_object(payload)
    record = records.get(str(expected_edition_id))
    if not isinstance(record, Mapping):
        raise LibGenMetadataError("LibGen edition identity conflicts with file metadata.")
    if _source_flag(record.get("visible"), default=True) is False:
        raise LibGenMetadataError("LibGen edition is not visible.")

    related_files = _relation_ids(record.get("files", {}), field="f_id", label="file")
    if related_files and expected_file_id not in related_files:
        raise LibGenMetadataError("LibGen edition file relation conflicts with file metadata.")
    issue_number = _optional_text(record.get("issue_number"), max_length=50)
    if issue_number is not None:
        if _ISSUE.fullmatch(issue_number) is None:
            raise LibGenMetadataError("LibGen edition issue number is malformed.")
        issue_number = normalize_issue(issue_number)

    return EditionMetadata(
        edition_id=expected_edition_id,
        title=_optional_text(record.get("title"), max_length=1_000),
        series_name=_optional_text(record.get("series_name"), max_length=1_000),
        title_add=_optional_text(record.get("title_add"), max_length=1_000),
        publisher=_optional_text(record.get("publisher"), max_length=500),
        year=_optional_year(record.get("year")),
        cover_url=_optional_text(record.get("cover_url"), max_length=2_000),
        issue_number=issue_number,
        issue_volume=_optional_text(record.get("issue_volume"), max_length=100),
        edition_type=_optional_text(record.get("type"), max_length=100),
    )


def build_candidate(
    discovered: DiscoveredRecord,
    file_metadata: FileMetadata,
    edition_metadata: EditionMetadata | None,
) -> Candidate:
    raw_title = file_metadata.locator_filename or discovered.raw_title
    display_title = _candidate_display_title(discovered, edition_metadata)
    evidence = parse_comic_title(display_title)
    explicit_issue = edition_metadata.issue_number if edition_metadata is not None else None
    issue_numbers = [explicit_issue] if explicit_issue else list(evidence.issue_numbers)
    series_title = (
        edition_metadata.series_name
        if edition_metadata is not None and edition_metadata.series_name
        else evidence.series_title
    )
    publisher = (
        edition_metadata.publisher
        if edition_metadata is not None and edition_metadata.publisher
        else discovered.publisher
    )
    year = (
        edition_metadata.year
        if edition_metadata is not None and edition_metadata.year is not None
        else discovered.year or evidence.year
    )
    extension = file_metadata.extension or discovered.extension
    provenance: dict[str, str | int | float | bool | None] = {
        "layout": "libgen-search-v1",
        "source_kind": "keyed_metadata",
        "file_id": file_metadata.file_id,
        "edition_id": edition_metadata.edition_id if edition_metadata is not None else None,
        "comics_id": file_metadata.comics_id,
    }
    return Candidate(
        provider_candidate_id=f"libgen:{file_metadata.md5}",
        content_fingerprint=f"md5:{file_metadata.md5}",
        source_reference=discovered.source_reference,
        display_title=display_title,
        raw_title=raw_title,
        parsed=ParsedCandidate(
            series_title=series_title,
            issue_numbers=issue_numbers,
            volume=(
                edition_metadata.issue_volume
                if edition_metadata is not None and edition_metadata.issue_volume
                else evidence.volume
            ),
            year=year,
            publisher=publisher,
            language=_language_code(discovered.language),
            format=extension,
        ),
        provider_confidence=0.95 if edition_metadata is not None else 0.75,
        provenance=provenance,
    )


def _candidate_display_title(
    discovered: DiscoveredRecord,
    edition: EditionMetadata | None,
) -> str:
    if edition is None:
        return discovered.display_title
    title = edition.title or edition.series_name or discovered.display_title
    if edition.title_add and edition.title_add.casefold() not in title.casefold():
        title = f"{title}: {edition.title_add}"
    if edition.issue_number and edition.issue_number not in parse_comic_title(title).issue_numbers:
        title = f"{title} #{edition.issue_number}"
    return title


def _decode_keyed_object(payload: str | bytes) -> dict[str, Any]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > _MAX_JSON_BYTES:
        raise LibGenMetadataError("LibGen metadata response exceeds the supported size.")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LibGenMetadataError("LibGen metadata response is malformed.") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise LibGenMetadataError("LibGen metadata response contains no keyed records.")
    return decoded


def _required_md5(value: object) -> str:
    normalized = str(value or "").casefold()
    if _MD5.fullmatch(normalized) is None:
        raise LibGenMetadataError("LibGen MD5 is malformed.")
    return normalized


def _positive_identifier(value: object, *, label: str) -> int:
    normalized = str(value or "")
    if _INTEGER.fullmatch(normalized) is None or int(normalized) < 1:
        raise LibGenMetadataError(f"LibGen {label} identity is malformed.")
    return int(normalized)


def _optional_identifier(value: object, *, label: str) -> int | None:
    if value in {None, ""}:
        return None
    return _positive_identifier(value, label=label)


def _optional_integer(value: object, *, label: str) -> int | None:
    if value in {None, ""}:
        return None
    normalized = str(value)
    if _INTEGER.fullmatch(normalized) is None:
        raise LibGenMetadataError(f"LibGen {label} is malformed.")
    return int(normalized)


def _optional_year(value: object) -> int | None:
    year = _optional_integer(value, label="year")
    if year is not None and not 1800 <= year <= 2200:
        raise LibGenMetadataError("LibGen year is outside the supported range.")
    return year


def _optional_extension(value: object) -> str | None:
    normalized = str(value or "").casefold().lstrip(".")
    if not normalized or normalized == "unknown":
        return None
    if normalized not in _SUPPORTED_EXTENSIONS:
        raise LibGenMetadataError("LibGen file extension is unsupported.")
    return normalized


def _optional_text(value: object, *, max_length: int) -> str | None:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return None
    if len(normalized) > max_length or "\x00" in normalized:
        raise LibGenMetadataError("LibGen metadata text exceeds the supported bound.")
    return normalized


def _source_flag(value: object, *, default: bool) -> bool:
    if value in {None, ""}:
        return default
    normalized = str(value).casefold()
    if normalized in {"1", "true", "y", "yes"}:
        return True
    if normalized in {"0", "false", "n", "no"}:
        return False
    raise LibGenMetadataError("LibGen source flag is malformed.")


def _relation_ids(value: object, *, field: str, label: str) -> set[int]:
    if value is None or value == "":
        return set()
    if not isinstance(value, Mapping):
        raise LibGenMetadataError(f"LibGen {label} relations are malformed.")
    identifiers: set[int] = set()
    for relation in value.values():
        if not isinstance(relation, Mapping) or field not in relation:
            continue
        identifiers.add(_positive_identifier(relation[field], label=label))
    return identifiers


def _locator_filename(value: object) -> str | None:
    locator = _optional_text(value, max_length=2_000)
    if locator is None:
        return None
    filename = locator.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if filename in {"", ".", ".."} or len(filename) > 1_000:
        raise LibGenMetadataError("LibGen locator filename is malformed.")
    return filename


def _language_code(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.casefold()
    return {"english": "en"}.get(normalized, normalized if len(normalized) <= 5 else None)


def _source_origin(source_reference: str) -> str:
    parsed = urlsplit(source_reference)
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in {None, 443}:
        raise LibGenMetadataError("LibGen source reference has an unsafe origin.")
    return urlunsplit(("https", parsed.hostname.casefold(), "", "", ""))


def _file_metadata_url(origin: str, md5: str) -> str:
    params = {"object": "f", "md5": md5, "topic": "c", "fields": _FILE_FIELDS}
    return f"{origin}/json.php?{urlencode(params)}"


def _edition_metadata_url(origin: str, edition_id: int) -> str:
    params = {
        "object": "e",
        "ids": edition_id,
        "topic": "c",
        "fields": _EDITION_FIELDS,
    }
    return f"{origin}/json.php?{urlencode(params)}"
