from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / ".github" / "scripts" / "resolve-provider-release.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("resolve_provider_release", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tag_release_maps_getcomics_to_both_registry_names() -> None:
    release = _load_module().resolve_release(
        repository_owner="pullboxapp",
        tag="getcomics-v1.0.0",
    )

    assert release.provider == "getcomics"
    assert release.version == "1.0.0"
    assert release.release_tag == "getcomics-v1.0.0"
    assert release.is_release is True
    assert release.is_prerelease is False
    assert release.dockerfile == "docker/Dockerfile.getcomics"
    assert release.ghcr_image == "ghcr.io/pullboxapp/pullbox-provider-getcomics"
    assert release.dockerhub_image == "docker.io/pullbox/pullbox-provider-getcomics"


def test_tag_release_preserves_provider_specific_prerelease(tmp_path: Path) -> None:
    pyproject = tmp_path / "providers" / "annas_archive" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text('[project]\nversion = "1.2.3-rc1"\n', encoding="utf-8")

    release = _load_module().resolve_release(
        repository_owner="pullboxapp",
        tag="annas-archive-v1.2.3-rc1",
        repository_root=tmp_path,
    )

    assert release.provider == "annas-archive"
    assert release.version == "1.2.3-rc1"
    assert release.is_prerelease is True


def test_manual_release_uses_edge_without_creating_a_github_release() -> None:
    release = _load_module().resolve_release(
        repository_owner="pullboxapp",
        provider="synthetic",
        version="edge",
    )

    assert release.provider == "synthetic"
    assert release.version == "edge"
    assert release.release_tag == ""
    assert release.is_release is False
    assert release.is_prerelease is False


def test_manual_release_rejects_numbered_tags() -> None:
    with pytest.raises(ValueError, match="Manual image releases must use edge"):
        _load_module().resolve_release(
            repository_owner="pullboxapp",
            provider="getcomics",
            version="1.0.0",
        )


@pytest.mark.parametrize(
    "tag",
    [
        "getcomics-v1.0",
        "getcomics-vlatest",
        "unknown-v1.0.0",
        "v1.0.0",
        "getcomics-v1.0.0+metadata",
    ],
)
def test_invalid_release_tags_are_rejected(tag: str) -> None:
    with pytest.raises(ValueError, match="provider release tag"):
        _load_module().resolve_release(repository_owner="pullboxapp", tag=tag)


def test_unknown_manual_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported provider"):
        _load_module().resolve_release(
            repository_owner="pullboxapp",
            provider="unknown",
            version="edge",
        )


def test_release_tag_must_match_the_selected_provider_package_version(tmp_path: Path) -> None:
    pyproject = tmp_path / "providers" / "getcomics" / "pyproject.toml"
    pyproject.parent.mkdir(parents=True)
    pyproject.write_text('[project]\nversion = "1.0.1"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="does not match package version"):
        _load_module().resolve_release(
            repository_owner="pullboxapp",
            tag="getcomics-v1.0.0",
            repository_root=tmp_path,
        )
