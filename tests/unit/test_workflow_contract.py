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
PROVIDER_RELEASE_WORKFLOW = WORKFLOW_DIR / "provider-release.yml"
GITHUB_RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
LATEST_RECONCILE_WORKFLOW = WORKFLOW_DIR / "provider-latest.yml"
RELEASE_RESOLVER = ROOT / ".github" / "scripts" / "resolve-provider-release.py"
LATEST_RELEASE_SELECTOR = ROOT / ".github" / "scripts" / "select-latest-provider-release.py"
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
    assert "pytest" in text
    assert "tests/unit/test_workflow_contract.py" in text
    assert "tests/unit/test_provider_release_metadata.py" in text


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
    assert '--report "$GRYPE_REPORT"' in text
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


def test_provider_release_is_tag_or_manual_only() -> None:
    workflow = _load_yaml(PROVIDER_RELEASE_WORKFLOW)
    text = PROVIDER_RELEASE_WORKFLOW.read_text(encoding="utf-8")
    triggers = _triggers(workflow)

    assert triggers["push"]["tags"] == [
        "getcomics-v*",
        "annas-archive-v*",
        "synthetic-v*",
    ]
    assert set(triggers["workflow_dispatch"]["inputs"]["provider"]["options"]) == {
        "getcomics",
        "annas-archive",
        "synthetic",
    }
    assert "tag_override" not in triggers["workflow_dispatch"]["inputs"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "concurrency" not in workflow
    assert "pull_request" not in triggers
    assert "pull_request_target" not in text


def test_tagged_provider_release_requires_a_commit_already_merged_to_main() -> None:
    workflow = _load_yaml(PROVIDER_RELEASE_WORKFLOW)
    prepare = workflow["jobs"]["prepare"]
    text = PROVIDER_RELEASE_WORKFLOW.read_text(encoding="utf-8")

    checkout = next(step for step in prepare["steps"] if step["name"] == "Checkout")
    assert checkout["with"]["fetch-depth"] == 0
    assert "git merge-base --is-ancestor" in text
    assert "origin/main" in text
    assert "github.ref_type == 'tag'" in text


def test_provider_release_maps_each_provider_to_both_registries() -> None:
    text = PROVIDER_RELEASE_WORKFLOW.read_text(encoding="utf-8")
    resolver_text = RELEASE_RESOLVER.read_text(encoding="utf-8")

    for provider in ("getcomics", "annas-archive", "synthetic"):
        assert f'dockerfile="docker/Dockerfile.{provider}"' in resolver_text
        assert f'image_name="pullbox-provider-{provider}"' in resolver_text

    assert '--repository-owner "${{ github.repository_owner }}"' in text
    assert 'ghcr_image=f"ghcr.io/{repository_owner}/{definition.image_name}"' in resolver_text
    assert 'dockerhub_image=f"docker.io/pullbox/{definition.image_name}"' in resolver_text
    assert "DOCKERHUB_USERNAME" in text
    assert "DOCKERHUB_TOKEN" in text


def test_provider_release_validates_before_tagging_and_preserves_supply_chain_data() -> None:
    workflow = _load_yaml(PROVIDER_RELEASE_WORKFLOW)
    jobs = workflow["jobs"]
    text = PROVIDER_RELEASE_WORKFLOW.read_text(encoding="utf-8")

    expected_jobs = {
        "prepare",
        "build-amd64",
        "build-arm64",
        "validate-amd64",
        "publish",
        "sign",
        "promote",
    }
    assert expected_jobs <= set(jobs)
    assert set(jobs["publish"]["needs"]) == {
        "prepare",
        "build-amd64",
        "build-arm64",
        "validate-amd64",
    }
    assert "verify-container-vulnerability-baseline.py" in text
    assert "linux/amd64" in text
    assert "linux/arm64" in text
    assert "provenance: mode=max" in text
    assert "sbom: true" in text
    assert "org.opencontainers.image.description" in text
    assert "same immutable digest" in text

    publish_text = yaml.safe_dump(jobs["publish"])
    sign_text = yaml.safe_dump(jobs["sign"])
    promote_text = yaml.safe_dump(jobs["promote"])
    assert "candidate-${{ github.run_id }}-${{ github.run_attempt }}" in publish_text
    assert "image-tags" not in publish_text
    assert "image-tags" not in sign_text
    assert "image-tags" in promote_text
    assert jobs["promote"]["needs"] == ["prepare", "publish", "sign"]
    assert "type=raw,value=latest" not in text
    assert (
        "type=sha,format=short,prefix=sha-,enable=${{ steps.release.outputs.is-release == 'true' }}"
    ) in text


def test_provider_release_signs_and_verifies_both_registry_digests() -> None:
    workflow = _load_yaml(PROVIDER_RELEASE_WORKFLOW)
    jobs = workflow["jobs"]
    text = PROVIDER_RELEASE_WORKFLOW.read_text(encoding="utf-8")
    verify_step = next(
        step
        for step in jobs["sign"]["steps"]
        if step.get("name") == "Verify both registry signatures"
    )
    verify_script = verify_step["run"]

    assert "id-token: write" in text
    assert "packages: write" in text
    assert text.count("cosign sign --yes") >= 2
    assert "cosign verify" in verify_script
    assert "verify_signature_with_retry()" in verify_script
    assert 'local max_attempts="12"' in verify_script
    assert 'local retry_delay_seconds="5"' in verify_script
    assert "no signatures found" in verify_script
    assert verify_script.count("verify_signature_with_retry") == 3
    assert jobs["sign"]["needs"] == ["prepare", "publish"]
    assert "provider-release-metadata" not in yaml.safe_dump(jobs["sign"])
    assert "provider-release-metadata" in yaml.safe_dump(jobs["promote"])


def test_github_release_requires_a_successful_tagged_provider_release() -> None:
    workflow = _load_yaml(GITHUB_RELEASE_WORKFLOW)
    jobs = workflow["jobs"]
    text = GITHUB_RELEASE_WORKFLOW.read_text(encoding="utf-8")
    triggers = _triggers(workflow)

    assert triggers == {
        "workflow_run": {
            "workflows": ["Provider Image Release"],
            "types": ["completed"],
        }
    }
    assert "concurrency" not in workflow
    assert jobs["create-release"]["if"] == (
        "github.event.workflow_run.conclusion == 'success' && "
        "github.event.workflow_run.event == 'push'"
    )
    assert "softprops/action-gh-release" not in text
    assert "gh release create" in text
    assert "gh release view" in text
    assert "actions/download-artifact@" in text
    assert "provider-release-metadata" in text
    assert "cosign verify" in text
    assert "(a|b|rc)[0-9]+$" in text
    assert "provider-latest-reconcile" in text
    assert "repos/${GITHUB_REPOSITORY}/dispatches" in text
    assert "select-latest-provider-release.py" not in text
    assert "Promote highest stable release to latest" not in text


def test_latest_reconciliation_is_serialized_and_idempotent_per_provider() -> None:
    workflow = _load_yaml(LATEST_RECONCILE_WORKFLOW)
    jobs = workflow["jobs"]
    text = LATEST_RECONCILE_WORKFLOW.read_text(encoding="utf-8")
    triggers = _triggers(workflow)

    assert triggers["repository_dispatch"]["types"] == ["provider-latest-reconcile"]
    assert set(triggers["workflow_dispatch"]["inputs"]["provider"]["options"]) == {
        "getcomics",
        "annas-archive",
        "synthetic",
    }
    assert workflow["concurrency"]["group"] == (
        "provider-latest-${{ github.event.client_payload.provider || inputs.provider }}"
    )
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert jobs["reconcile"]["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert "select-latest-provider-release.py" in text
    assert "gh release list" in text
    assert "cosign verify" in text
    assert "Promote highest stable release to latest" in text
    assert "docker buildx imagetools create" in text
    assert "github.event.client_payload.provider" in text
    assert LATEST_RELEASE_SELECTOR.exists()
