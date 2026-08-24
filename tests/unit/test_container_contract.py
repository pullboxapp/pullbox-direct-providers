from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
DOCKERFILE = ROOT / "docker" / "Dockerfile.synthetic"
COMPOSE_FILE = ROOT / "docker" / "compose.synthetic-test.yml"
SOURCE_DOCKERFILES = {
    "getcomics": (ROOT / "docker" / "Dockerfile.getcomics", "providers/getcomics"),
    "annas-archive": (
        ROOT / "docker" / "Dockerfile.annas-archive",
        "providers/annas_archive",
    ),
    "libgen": (ROOT / "docker" / "Dockerfile.libgen", "providers/libgen"),
}
SOURCE_COMPOSE_FILE = ROOT / "docker" / "compose.providers-test.yml"
SOURCE_SMOKE_SCRIPT = ROOT / "docker" / "provider_smoke.py"


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


def test_source_provider_images_are_independent_hardened_python_314_services() -> None:
    for provider, (path, source_path) in SOURCE_DOCKERFILES.items():
        dockerfile = path.read_text(encoding="utf-8")

        assert (
            "python:3.14-slim@sha256:"
            "cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6" in dockerfile
        )
        assert "USER 65532:65532" in dockerfile
        assert "EXPOSE 8780" in dockerfile
        assert '"--port", "8780"' in dockerfile
        assert source_path in dockerfile
        other_source_paths = {
            source for other, (_path, source) in SOURCE_DOCKERFILES.items() if other != provider
        }
        assert all(source not in dockerfile for source in other_source_paths)


def test_source_provider_healthchecks_test_process_liveness_without_upstream_work() -> None:
    for path, _source_path in SOURCE_DOCKERFILES.values():
        dockerfile = path.read_text(encoding="utf-8")
        healthcheck = dockerfile.split("HEALTHCHECK", maxsplit=1)[1].split(
            "ENTRYPOINT", maxsplit=1
        )[0]

        assert "socket.create_connection" in healthcheck
        assert "/v1/health" not in healthcheck
        assert "PULLBOX_PROVIDER_TOKEN" not in healthcheck


def test_source_provider_smoke_has_no_ports_mounts_or_browser_privileges() -> None:
    compose = yaml.safe_load(SOURCE_COMPOSE_FILE.read_text(encoding="utf-8"))

    for service_name in ("getcomics", "annas-archive", "libgen"):
        provider = compose["services"][service_name]
        assert "ports" not in provider
        assert "volumes" not in provider
        assert provider["read_only"] is True
        assert provider["user"] == "65532:65532"
        assert provider["cap_drop"] == ["ALL"]
        assert provider["security_opt"] == ["no-new-privileges:true"]
        assert provider["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=16m"]  # noqa: S108
        assert "shm_size" not in provider

    smoke = compose["services"]["smoke"]
    assert smoke["depends_on"]["getcomics"]["condition"] == "service_healthy"
    assert smoke["depends_on"]["annas-archive"]["condition"] == "service_healthy"
    assert smoke["depends_on"]["libgen"]["condition"] == "service_healthy"
    assert "ports" not in smoke
    assert compose["networks"]["provider-test"]["internal"] is True


def test_source_provider_smoke_never_probes_external_source_health() -> None:
    smoke_script = SOURCE_SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert '"/v1/manifest"' in smoke_script
    assert '"/v1/health"' not in smoke_script
