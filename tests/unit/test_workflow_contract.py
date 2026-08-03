from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
SECURITY_WORKFLOW = WORKFLOW_DIR / "security.yml"
HYGIENE_WORKFLOW = WORKFLOW_DIR / "workflow-hygiene.yml"
CONTAINER_WORKFLOW = WORKFLOW_DIR / "container-security.yml"
RELEASE_WORKFLOW = WORKFLOW_DIR / "synthetic-release.yml"
DEPENDABOT_CONFIG = ROOT / ".github" / "dependabot.yml"
CODEQL_CONFIG = ROOT / ".github" / "codeql" / "codeql-config.yml"
PINNED_ACTION = re.compile(r"uses:\s+[^@\s]+@[0-9a-f]{40}\s+#\s+v\S+")


def _load_yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get(True, workflow.get("on"))
    assert isinstance(value, dict)
    return value


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOW_DIR.glob("*.yml"))


def test_pr_workflows_are_read_only_and_never_use_private_runners() -> None:
    for path in (CI_WORKFLOW, SECURITY_WORKFLOW, HYGIENE_WORKFLOW, CONTAINER_WORKFLOW):
        workflow = _load_yaml(path)
        text = path.read_text(encoding="utf-8")
        triggers = _triggers(workflow)

        assert workflow["permissions"] == {"contents": "read"}
        assert triggers["pull_request"]["branches"] == ["develop", "main"]
        assert "pull_request_target" not in text
        assert "self-hosted" not in text
        assert "packages: write" not in text
        assert "docker push" not in text


def test_every_workflow_action_is_pinned_to_a_full_commit() -> None:
    action_lines = [
        line.strip()
        for workflow in _workflow_files()
        for line in workflow.read_text(encoding="utf-8").splitlines()
        if "uses:" in line and not line.lstrip().startswith("#")
    ]

    assert action_lines
    assert all(PINNED_ACTION.search(line) for line in action_lines)


def test_every_workflow_and_job_declares_least_privilege_permissions() -> None:
    for path in _workflow_files():
        workflow = _load_yaml(path)
        assert workflow.get("permissions") == {"contents": "read"}
        for job in workflow["jobs"].values():
            assert isinstance(job, dict)
            assert "permissions" in job


def test_ci_validates_supported_python_versions_with_stable_aggregate() -> None:
    jobs = _load_yaml(CI_WORKFLOW)["jobs"]

    assert jobs["test"]["strategy"]["matrix"]["python-version"] == [
        "3.12",
        "3.13",
        "3.14",
    ]
    assert jobs["required"]["name"] == "CI Required"
    assert set(jobs["required"]["needs"]) == {"quality", "test"}
    assert "container-conformance" not in jobs
    assert "multiarch-build" not in jobs


def test_security_workflow_runs_blocking_scanners_and_scoped_codeql() -> None:
    workflow = _load_yaml(SECURITY_WORKFLOW)
    jobs = workflow["jobs"]
    text = SECURITY_WORKFLOW.read_text(encoding="utf-8")
    config = _load_yaml(CODEQL_CONFIG)

    assert {
        "gitleaks",
        "dependency-audit",
        "bandit",
        "dependency-review",
        "codeql",
        "required",
    } <= set(jobs)
    assert jobs["required"]["name"] == "Security Required"
    assert jobs["dependency-review"]["if"] == "github.event_name == 'pull_request'"
    assert "queries: +security-extended" in text
    assert "pip-audit --strict ." in text
    assert "bandit -r packages providers -ll -ii" in text
    assert "gitleaks/gitleaks@sha256:" in text
    assert config["paths"] == ["packages/**/src", "providers/**/src"]
    assert config["paths-ignore"] == ["tests/**"]


def test_workflow_hygiene_runs_actionlint_and_contract_tests() -> None:
    workflow = _load_yaml(HYGIENE_WORKFLOW)
    jobs = workflow["jobs"]
    text = HYGIENE_WORKFLOW.read_text(encoding="utf-8")

    assert {"actionlint", "contract-tests", "required"} <= set(jobs)
    assert jobs["required"]["name"] == "Workflow Hygiene Required"
    assert "rhysd/actionlint@sha256:" in text
    assert "pytest tests/unit/test_workflow_contract.py" in text


def test_container_security_builds_tests_and_scans_every_runtime_image() -> None:
    workflow = _load_yaml(CONTAINER_WORKFLOW)
    jobs = workflow["jobs"]
    text = CONTAINER_WORKFLOW.read_text(encoding="utf-8")
    expected_providers = {"synthetic", "getcomics", "annas-archive"}

    assert {"runtime-smoke", "image-scan", "multiarch-build", "required"} <= set(jobs)
    assert jobs["required"]["name"] == "Container Security Required"
    assert set(jobs["image-scan"]["strategy"]["matrix"]["provider"]) == expected_providers
    assert set(jobs["multiarch-build"]["strategy"]["matrix"]["provider"]) == expected_providers
    assert "make docker-conformance" in text
    assert "make docker-source-smoke" in text
    assert "anchore/scan-action@" in text
    assert "verify-container-vulnerability-baseline.py" in text
    assert "filter-reviewed-container-sarif.py" in text
    assert "linux/amd64,linux/arm64" in text
    assert "PYTHON_BASE" not in text
    assert "if: always()" in text
    assert "if-no-files-found: ignore" in text
    assert "push: true" not in text
    assert "push=true" not in text

    steps = jobs["image-scan"]["steps"]
    json_scan = next(step for step in steps if step.get("id") == "scan-json")
    sarif_scan = next(step for step in steps if step.get("id") == "scan-sarif")
    sarif_upload = next(step for step in steps if step.get("name") == "Upload actionable SARIF")
    assert json_scan["with"]["severity-cutoff"] == "negligible"
    assert sarif_scan["with"]["severity-cutoff"] == "high"
    assert sarif_upload["with"]["sarif_file"].endswith("-actionable.sarif")


def test_dependabot_covers_python_actions_and_each_dockerfile() -> None:
    updates = _load_yaml(DEPENDABOT_CONFIG)["updates"]
    ecosystems = [entry["package-ecosystem"] for entry in updates]
    docker_directories = {
        entry["directory"] for entry in updates if entry["package-ecosystem"] == "docker"
    }

    assert "pip" in ecosystems
    assert "github-actions" in ecosystems
    assert docker_directories == {"/docker"}
    assert all(entry["target-branch"] == "develop" for entry in updates)


def test_synthetic_release_is_tag_or_manual_only_and_signs_the_published_digest() -> None:
    workflow = _load_yaml(RELEASE_WORKFLOW)
    text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    triggers = _triggers(workflow)

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
