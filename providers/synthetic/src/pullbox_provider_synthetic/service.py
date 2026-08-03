"""Deterministic synthetic provider behavior."""

from __future__ import annotations

from pullbox_provider_contract.models import (
    Artifact,
    ArtifactCoverage,
    ArtifactRoute,
    Candidate,
    Mirror,
    ParsedCandidate,
    SearchIntent,
)


class SyntheticProviderService:
    """Return generated, non-copyrighted fixtures for conformance tests."""

    def search(self, intent: SearchIntent) -> list[Candidate]:
        issue_number = intent.issue_number or "1"
        return [
            Candidate(
                provider_candidate_id=f"synthetic-issue-{number}",
                source_reference=f"synthetic://candidate/{number}",
                display_title=f"Synthetic Adventures #{number}",
                raw_title=f"Synthetic Adventures {number} (2026) (Digital)",
                parsed=ParsedCandidate(
                    series_title=intent.series_title,
                    issue_numbers=[number],
                    year=intent.year,
                    publisher=intent.publisher,
                    language=intent.language,
                    format="cbz",
                    quality="digital",
                ),
                provider_confidence=1.0 if number == issue_number else 0.8,
                provenance={"fixture": "deterministic"},
            )
            for number in [issue_number, "2", "3"]
        ]

    def resolve(self, candidate_id: str) -> list[Artifact] | None:
        prefix = "synthetic-issue-"
        if not candidate_id.startswith(prefix):
            return None
        issue_number = candidate_id.removeprefix(prefix)
        if not issue_number:
            return None
        return [
            Artifact(
                artifact_id=f"artifact-{issue_number}",
                coverage=ArtifactCoverage(issue_numbers=[issue_number]),
                route=ArtifactRoute.DIRECT_ARTIFACT,
                format="cbz",
                quality="digital",
                language="en",
                size_bytes=1024,
                mirrors=[
                    Mirror(
                        mirror_id=f"mirror-{issue_number}",
                        host_kind="generic_https",
                        final_url=f"https://provider.test/files/{issue_number}.cbz",
                        size_bytes=1024,
                        etag=f'"synthetic-{issue_number}"',
                    )
                ],
            )
        ]
