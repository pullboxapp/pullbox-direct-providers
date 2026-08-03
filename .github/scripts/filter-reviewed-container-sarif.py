#!/usr/bin/env python3
"""Remove reviewed container findings before uploading actionable SARIF."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _reviewed_rule_ids(baseline: dict[str, Any], image: str) -> set[str]:
    if baseline.get("schema_version") != 1:
        raise ValueError("Container vulnerability baseline schema is unsupported")
    expires_on = baseline.get("expires_on")
    if not isinstance(expires_on, str):
        raise ValueError("Container vulnerability baseline is missing expires_on")
    if date.fromisoformat(expires_on) < date.today():
        raise ValueError(f"Container vulnerability baseline expired on {expires_on}")

    images = baseline.get("images")
    profiles = baseline.get("profiles")
    if not isinstance(images, dict) or not isinstance(profiles, dict):
        raise ValueError("Container vulnerability baseline is missing images or profiles")
    profile_name = images.get(image)
    if not isinstance(profile_name, str):
        raise ValueError(f"Container vulnerability baseline has no image named {image!r}")
    entries = profiles.get(profile_name)
    if not isinstance(entries, list):
        raise ValueError(f"Container vulnerability profile {profile_name!r} is invalid")

    rule_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"Container vulnerability profile {profile_name!r} is invalid")
        identifier = entry.get("id")
        package = entry.get("package")
        if not isinstance(identifier, str) or not isinstance(package, str):
            raise ValueError(f"Container vulnerability profile {profile_name!r} is invalid")
        rule_ids.add(f"{identifier}-{package}")
    return rule_ids


def filter_sarif(sarif: dict[str, Any], reviewed_rule_ids: set[str]) -> tuple[int, int]:
    runs = sarif.get("runs")
    if not isinstance(runs, list):
        raise ValueError("SARIF document is missing its runs list")

    removed = 0
    remaining = 0
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError("SARIF document contains an invalid run")
        results = run.get("results", [])
        if not isinstance(results, list):
            raise ValueError("SARIF run contains an invalid results list")
        filtered_results: list[dict[str, Any]] = []
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("SARIF run contains an invalid result")
            rule_id = result.get("ruleId")
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError("SARIF result is missing ruleId")
            if rule_id in reviewed_rule_ids:
                removed += 1
            else:
                filtered_results.append(result)
        run["results"] = filtered_results
        remaining += len(filtered_results)
    return removed, remaining


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sarif", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        sarif = _read_object(args.sarif)
        baseline = _read_object(args.baseline)
        removed, remaining = filter_sarif(
            sarif,
            _reviewed_rule_ids(baseline, args.image),
        )
        args.output.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Container SARIF filtering failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Actionable SARIF for {args.image}: {remaining} unreviewed finding(s); "
        f"{removed} reviewed finding(s) omitted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
