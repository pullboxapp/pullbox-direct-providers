#!/usr/bin/env python3
"""Resolve and validate provider-specific container release metadata."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path
from typing import NamedTuple

SEMVER = re.compile(
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)\."
    r"(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
TAG = re.compile(rf"(?P<provider>getcomics|annas-archive|synthetic)-v(?P<version>{SEMVER.pattern})")
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


class ProviderDefinition(NamedTuple):
    dockerfile: str
    version_file: str
    image_name: str
    title: str
    description: str


class ProviderRelease(NamedTuple):
    provider: str
    dockerfile: str
    image_name: str
    title: str
    description: str
    ghcr_image: str
    dockerhub_image: str
    version: str
    release_tag: str
    is_release: bool
    is_prerelease: bool


PROVIDERS = {
    "getcomics": ProviderDefinition(
        dockerfile="docker/Dockerfile.getcomics",
        version_file="providers/getcomics/pyproject.toml",
        image_name="pullbox-provider-getcomics",
        title="Pullbox GetComics Direct Download Provider",
        description="Optional GetComics discovery provider for Pullbox direct downloads",
    ),
    "annas-archive": ProviderDefinition(
        dockerfile="docker/Dockerfile.annas-archive",
        version_file="providers/annas_archive/pyproject.toml",
        image_name="pullbox-provider-annas-archive",
        title="Pullbox Anna's Archive Direct Download Provider",
        description="Optional Anna's Archive discovery provider for Pullbox direct downloads",
    ),
    "synthetic": ProviderDefinition(
        dockerfile="docker/Dockerfile.synthetic",
        version_file="pyproject.toml",
        image_name="pullbox-provider-synthetic",
        title="Pullbox Synthetic Direct Download Provider",
        description="Reference and conformance provider for the Pullbox direct-download protocol",
    ),
}


def resolve_release(
    *,
    repository_owner: str,
    tag: str = "",
    provider: str = "",
    version: str = "edge",
    repository_root: Path | None = None,
) -> ProviderRelease:
    """Return validated release metadata for a tag or manual dispatch."""
    if not OWNER.fullmatch(repository_owner):
        raise ValueError("Invalid GitHub repository owner")

    release_tag = ""
    is_release = bool(tag)
    if is_release:
        match = TAG.fullmatch(tag)
        if match is None:
            raise ValueError(
                "Invalid provider release tag; expected <provider>-v<major>.<minor>.<patch>"
            )
        provider = match.group("provider")
        version = match.group("version")
        release_tag = tag
    else:
        if provider not in PROVIDERS:
            raise ValueError(f"Unsupported provider: {provider}")
        if version != "edge" and SEMVER.fullmatch(version) is None:
            raise ValueError("Manual image tag must be edge or a semantic version")

    definition = PROVIDERS[provider]
    root = repository_root or Path(__file__).parents[2]
    with (root / definition.version_file).open("rb") as project_file:
        package_version = tomllib.load(project_file)["project"]["version"]
    if version != "edge" and version != package_version:
        raise ValueError(
            f"Release version {version} does not match package version {package_version}"
        )
    return ProviderRelease(
        provider=provider,
        dockerfile=definition.dockerfile,
        image_name=definition.image_name,
        title=definition.title,
        description=definition.description,
        ghcr_image=f"ghcr.io/{repository_owner}/{definition.image_name}",
        dockerhub_image=f"docker.io/pullbox/{definition.image_name}",
        version=version,
        release_tag=release_tag,
        is_release=is_release,
        is_prerelease=is_release and "-" in version,
    )


def _write_github_output(path: Path, release: ProviderRelease) -> None:
    values = release._asdict()
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            rendered_key = key.replace("_", "-")
            rendered_value = str(value).lower() if isinstance(value, bool) else value
            output.write(f"{rendered_key}={rendered_value}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-owner", required=True)
    parser.add_argument("--tag", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--version", default="edge")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    release = resolve_release(
        repository_owner=args.repository_owner,
        tag=args.tag,
        provider=args.provider,
        version=args.version,
    )
    _write_github_output(args.github_output, release)


if __name__ == "__main__":
    main()
