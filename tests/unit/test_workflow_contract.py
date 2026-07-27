from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
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
    assert set(jobs["required"]["needs"]) == {"quality", "test", "container-conformance"}


def test_third_party_actions_are_pinned_to_full_commits() -> None:
    action_lines = [
        line.strip()
        for line in CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "uses:" in line and not line.lstrip().startswith("#")
    ]

    assert action_lines
    assert all(PINNED_ACTION.search(line) for line in action_lines)
