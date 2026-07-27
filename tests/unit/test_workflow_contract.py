from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "synthetic-release.yml"
PINNED_ACTION = re.compile(r"uses:\s+[^@\s]+@[0-9a-f]{40}\s+#\s+v\S+")


def _load_workflow() -> dict[str, Any]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def test_ci_is_read_only_and_never_routes_pull_requests_to_private_runners() -> None:
    workflow = _load_workflow()
    text = CI_WORKFLOW.read_text(encoding="utf-8")
    triggers = workflow.get(True, workflow.get("on"))

    assert workflow["permissions"] == {"contents": "read"}
    assert isinstance(triggers, dict)
    assert triggers["pull_request"]["branches"] == ["develop", "main"]
    assert "pull_request_target" not in text
    assert "self-hosted" not in text
    assert "packages: write" not in text
    assert "docker push" not in text


def test_ci_validates_supported_python_versions_and_private_network_container() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]

    assert jobs["test"]["strategy"]["matrix"]["python-version"] == ["3.12", "3.13", "3.14"]
    assert jobs["container-conformance"]["runs-on"] == "ubuntu-latest"
    assert any(
        step.get("run") == "make docker-conformance"
        for step in jobs["container-conformance"]["steps"]
    )
    assert jobs["required"]["name"] == "CI Required"
    assert set(jobs["required"]["needs"]) == {
        "quality",
        "test",
        "container-conformance",
        "multiarch-build",
    }


def test_ci_builds_reference_image_for_both_supported_architectures_without_publishing() -> None:
    workflow = _load_workflow()
    job = workflow["jobs"]["multiarch-build"]
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert job["runs-on"] == "ubuntu-latest"
    assert any("docker/setup-qemu-action@" in step.get("uses", "") for step in job["steps"])
    assert any("docker/setup-buildx-action@" in step.get("uses", "") for step in job["steps"])
    assert "linux/amd64,linux/arm64" in text
    assert "type=oci" in text
    assert "push: true" not in text
    assert "push=true" not in text


def test_synthetic_release_is_tag_or_manual_only_and_signs_the_published_digest() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    triggers = workflow.get(True, workflow.get("on"))

    assert triggers == {
        "push": {"tags": ["synthetic-v*"]},
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert "pull_request" not in triggers
    assert "pull_request_target" not in text
    assert "linux/amd64,linux/arm64" in text
    assert "push: true" in text
    assert "provenance: mode=max" in text
    assert "sbom: true" in text
    assert "id-token: write" in text
    assert "packages: write" in text
    assert "cosign sign --yes" in text
    assert "cosign verify" in text


def test_third_party_actions_are_pinned_to_full_commits() -> None:
    action_lines = [
        line.strip()
        for workflow in (CI_WORKFLOW, RELEASE_WORKFLOW)
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if "uses:" in line and not line.lstrip().startswith("#")
    ]

    assert action_lines
    assert all(PINNED_ACTION.search(line) for line in action_lines)
