from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pullbox_provider_libgen.metadata import (
    LibGenMetadataEnricher,
    LibGenMetadataError,
    build_candidate,
    parse_edition_metadata,
    parse_file_metadata,
    parse_file_metadata_by_md5,
)
from pullbox_provider_libgen.parser import parse_search_html

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "libgen"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _records():
    return parse_search_html(
        _fixture("search-results-v1.html"),
        source_origin="https://libgen.gl",
    )


def test_parse_keyed_file_and_edition_metadata_then_build_candidate() -> None:
    discovered = _records()[0]
    file_metadata = parse_file_metadata(_fixture("file-v1.json"), expected=discovered)
    edition_metadata = parse_edition_metadata(
        _fixture("edition-v1.json"),
        expected_edition_id=file_metadata.edition_id,
        expected_file_id=file_metadata.file_id,
    )

    assert file_metadata.file_id == 1201
    assert file_metadata.edition_id == 910
    assert file_metadata.comics_id == 410
    assert file_metadata.locator_filename == "Clockwork Harbor 003 (2024) (Digital).cbz"
    assert edition_metadata is not None
    assert edition_metadata.edition_id == 910
    assert edition_metadata.issue_number == "3"

    candidate = build_candidate(discovered, file_metadata, edition_metadata)
    assert candidate.provider_candidate_id == "libgen:0123456789abcdef0123456789abcdef"
    assert candidate.content_fingerprint == "md5:0123456789abcdef0123456789abcdef"
    assert candidate.source_reference == "https://libgen.gl/file.php?id=1201"
    assert candidate.display_title == "Clockwork Harbor: Signal Fires #3"
    assert candidate.raw_title == "Clockwork Harbor 003 (2024) (Digital).cbz"
    assert candidate.parsed.series_title == "Clockwork Harbor"
    assert candidate.parsed.issue_numbers == ["3"]
    assert candidate.parsed.year == 2024
    assert candidate.parsed.publisher == "Example Press"
    assert candidate.parsed.language == "en"
    assert candidate.parsed.format == "cbz"
    assert candidate.provider_confidence == 0.95
    assert candidate.provenance == {
        "layout": "libgen-search-v1",
        "source_kind": "keyed_metadata",
        "file_id": 1201,
        "edition_id": 910,
        "comics_id": 410,
    }


def test_sparse_file_metadata_remains_a_lower_confidence_candidate() -> None:
    discovered = _records()[1]
    file_metadata = parse_file_metadata(_fixture("file-sparse-v1.json"), expected=discovered)

    assert file_metadata.edition_id == 911
    assert build_candidate(discovered, file_metadata, None).provider_confidence == 0.75


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("0123456789abcdef0123456789abcdef", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "MD5"),
        ('"18874368"', '"19922944"', "size"),
        ('"cbz"', '"pdf"', "extension"),
        ('"1201"', '"9999"', "file"),
    ],
)
def test_file_metadata_rejects_html_api_identity_conflicts(
    old: str,
    new: str,
    message: str,
) -> None:
    payload = _fixture("file-v1.json").replace(old, new, 1)

    with pytest.raises(LibGenMetadataError, match=message):
        parse_file_metadata(payload, expected=_records()[0])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.replace('"visible": "1"', '"visible": "0"', 1),
        lambda payload: payload.replace('"broken": "0"', '"broken": "1"', 1),
        lambda payload: payload.replace('"e_id": "910"', '"e_id": "999"', 1),
    ],
)
def test_file_metadata_rejects_unavailable_or_conflicting_records(mutation) -> None:
    with pytest.raises(LibGenMetadataError):
        parse_file_metadata(mutation(_fixture("file-v1.json")), expected=_records()[0])


def test_edition_metadata_uses_keyed_identity_and_validates_file_relation() -> None:
    with pytest.raises(LibGenMetadataError, match="edition"):
        parse_edition_metadata(
            _fixture("edition-v1.json").replace('"910":', '"999":', 1),
            expected_edition_id=910,
            expected_file_id=1201,
        )

    with pytest.raises(LibGenMetadataError, match="file"):
        parse_edition_metadata(
            _fixture("edition-v1.json").replace('"f_id": "1201"', '"f_id": "9999"'),
            expected_edition_id=910,
            expected_file_id=1201,
        )


@pytest.mark.parametrize("payload", ["[]", "null", "{", "{}"])
def test_metadata_rejects_missing_or_malformed_top_level_shapes(payload: str) -> None:
    with pytest.raises(LibGenMetadataError):
        parse_file_metadata(payload, expected=_records()[0])


def test_resolve_can_rebuild_file_metadata_from_md5_without_search_cache() -> None:
    metadata = parse_file_metadata_by_md5(
        _fixture("file-v1.json"),
        expected_md5="0123456789abcdef0123456789abcdef",
    )

    assert metadata.file_id == 1201
    assert metadata.edition_id == 910
    assert metadata.extension == "cbz"


def test_file_metadata_accepts_live_empty_visible_and_yes_no_broken_flags() -> None:
    payload = _fixture("file-v1.json").replace('"visible": "1"', '"visible": ""', 1)
    payload = payload.replace('"broken": "0"', '"broken": "N"', 1)

    metadata = parse_file_metadata(payload, expected=_records()[0])

    assert metadata.file_id == 1201


def test_file_metadata_accepts_exact_size_within_html_rounding_precision() -> None:
    payload = _fixture("file-v1.json").replace('"18874368"', '"19074368"', 1)

    metadata = parse_file_metadata(payload, expected=_records()[0])

    assert metadata.size_bytes == 19_074_368


async def test_metadata_enricher_fetches_keyed_records_once_then_uses_cache() -> None:
    calls: list[str] = []

    async def fetcher(url: str) -> str:
        calls.append(url)
        return _fixture("edition-v1.json") if "object=e" in url else _fixture("file-v1.json")

    enricher = LibGenMetadataEnricher(fetcher=fetcher)
    discovered = _records()[0]

    first = await enricher.enrich(discovered)
    second = await enricher.enrich(discovered)

    assert first == second
    assert first is not None
    assert len(calls) == 2
    assert "object=f" in calls[0]
    assert "md5=0123456789abcdef0123456789abcdef" in calls[0]
    assert "object=e" in calls[1]
    assert "ids=910" in calls[1]


async def test_metadata_enricher_batches_multiple_keyed_records() -> None:
    calls: list[str] = []
    file_records = json.loads(_fixture("file-v1.json")) | json.loads(
        _fixture("file-sparse-v1.json")
    )
    file_records["1202"]["editions"] = {"50002": {"e_id": "911"}}
    edition_records = json.loads(_fixture("edition-v1.json"))
    edition_records["911"] = {
        "title": "Clockwork Harbor Deluxe Collection",
        "series_name": "Clockwork Harbor",
        "publisher": "Example Press",
        "year": "2025",
        "issue_number": "2",
        "issue_volume": "2",
        "visible": "1",
        "files": {"70002": {"f_id": "1202"}},
    }

    async def fetcher(url: str) -> str:
        calls.append(url)
        return json.dumps(edition_records if "object=e" in url else file_records)

    enricher = LibGenMetadataEnricher(fetcher=fetcher)
    candidates = await enricher.enrich_many(_records())
    cached = await enricher.enrich_many(_records())

    assert cached == candidates
    assert [candidate.provider_candidate_id for candidate in candidates] == [
        "libgen:0123456789abcdef0123456789abcdef",
        "libgen:fedcba9876543210fedcba9876543210",
    ]
    assert len(calls) == 2
    assert "object=f" in calls[0]
    assert "ids=1201%2C1202" in calls[0]
    assert "object=e" in calls[1]
    assert "ids=910%2C911" in calls[1]


async def test_metadata_enricher_rejects_only_the_invalid_record_in_a_batch() -> None:
    calls: list[str] = []
    file_records = json.loads(_fixture("file-v1.json"))

    async def fetcher(url: str) -> str:
        calls.append(url)
        return _fixture("edition-v1.json") if "object=e" in url else json.dumps(file_records)

    enricher = LibGenMetadataEnricher(fetcher=fetcher)
    candidates = await enricher.enrich_many(_records())
    cached = await enricher.enrich_many(_records())

    assert cached == candidates
    assert [candidate.provider_candidate_id for candidate in candidates] == [
        "libgen:0123456789abcdef0123456789abcdef"
    ]
    assert len(calls) == 2


async def test_metadata_enricher_validates_shared_edition_for_each_file_once() -> None:
    calls: list[str] = []
    file_records = json.loads(_fixture("file-v1.json")) | json.loads(
        _fixture("file-sparse-v1.json")
    )
    file_records["1202"]["editions"] = {"50002": {"e_id": "910"}}
    edition_records = json.loads(_fixture("edition-v1.json"))
    edition_records["910"]["files"]["70002"] = {"f_id": "1202"}

    async def fetcher(url: str) -> str:
        calls.append(url)
        return json.dumps(edition_records if "object=e" in url else file_records)

    enricher = LibGenMetadataEnricher(fetcher=fetcher)
    records = _records()
    records[1] = replace(records[1], edition_id=910)
    candidates = await enricher.enrich_many(records)

    assert len(candidates) == 2
    assert await enricher.enrich_many(records) == candidates
    assert len(calls) == 2
    assert "ids=910" in calls[1]


async def test_metadata_enricher_negatively_caches_invalid_source_records() -> None:
    calls = 0

    async def fetcher(_url: str) -> str:
        nonlocal calls
        calls += 1
        return "{}"

    enricher = LibGenMetadataEnricher(fetcher=fetcher)
    discovered = _records()[0]

    assert await enricher.enrich(discovered) is None
    assert await enricher.enrich(discovered) is None
    assert calls == 1
