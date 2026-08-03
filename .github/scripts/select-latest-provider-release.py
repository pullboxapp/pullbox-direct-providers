#!/usr/bin/env python3
"""Select the highest published stable release for one provider."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PROVIDERS = {"getcomics", "annas-archive", "synthetic"}
STABLE_VERSION = r"(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"


def select_latest_release(
    releases: list[dict[str, object]],
    *,
    provider: str,
) -> tuple[str, str] | None:
    """Return the tag and version for the provider's highest stable release."""
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported provider: {provider}")

    tag_pattern = re.compile(rf"{re.escape(provider)}-v(?P<version>{STABLE_VERSION})")
    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    for release in releases:
        if release.get("isDraft") is True or release.get("isPrerelease") is True:
            continue
        tag = release.get("tagName")
        if not isinstance(tag, str):
            continue
        match = tag_pattern.fullmatch(tag)
        if match is None:
            continue
        version_key = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
        candidates.append((version_key, tag, match.group("version")))

    if not candidates:
        return None
    _, tag, version = max(candidates)
    return tag, version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=sorted(PROVIDERS), required=True)
    parser.add_argument("--releases", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    releases = json.loads(args.releases.read_text(encoding="utf-8"))
    if not isinstance(releases, list):
        raise ValueError("GitHub release data must be a list")
    selected = select_latest_release(releases, provider=args.provider)

    with args.github_output.open("a", encoding="utf-8") as output:
        if selected is None:
            output.write("found=false\n")
            return
        tag, version = selected
        output.write("found=true\n")
        output.write(f"tag={tag}\n")
        output.write(f"version={version}\n")


if __name__ == "__main__":
    main()
