from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile.synthetic"
COMPOSE_FILE = ROOT / "docker" / "compose.synthetic-test.yml"


def test_runtime_uses_pinned_python_314_and_non_root_identity() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6"
        in dockerfile
    )
    assert "AS build" in dockerfile
    assert "AS runtime" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "EXPOSE 8780" in dockerfile
    assert '"--port", "8780"' in dockerfile


def test_private_network_smoke_has_no_provider_mount_or_host_port() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    provider = compose["services"]["synthetic"]

    assert "ports" not in provider
    assert "volumes" not in provider
    assert provider["read_only"] is True
    assert provider["user"] == "65532:65532"
    assert provider["cap_drop"] == ["ALL"]
    assert provider["security_opt"] == ["no-new-privileges:true"]
    assert provider["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=16m"]  # noqa: S108
    assert compose["networks"]["provider-test"]["internal"] is True


def test_conformance_runner_uses_only_the_private_provider_network() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    conformance = compose["services"]["conformance"]

    assert conformance["depends_on"]["synthetic"]["condition"] == "service_healthy"
    assert conformance["environment"]["PULLBOX_PROVIDER_BASE_URL"] == "http://synthetic:8780"
    assert conformance["networks"] == ["provider-test"]
    assert "ports" not in conformance
