#!/usr/bin/env python3
"""Remove reviewed container findings before uploading actionable SARIF."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from container_vulnerability_policy import (
    BLOCKING_SEVERITIES,
    KNOWN_SEVERITIES,
    accepted,
    evaluate,
    read_object,
)


def _omission_evidence(
    report: dict[str, Any],
    baseline: dict[str, Any],
    image: str,
) -> tuple[set[str], dict[str, set[str]]]:
    actual, reviewed = evaluate(report, baseline, image)
    unreviewed_ids = {
        item.rule_id
        for item in actual
        if item.severity in BLOCKING_SEVERITIES and not accepted(item, reviewed)
    }
    nonblocking_ids = {
        item.rule_id
        for item in actual
        if item.severity not in BLOCKING_SEVERITIES and item.rule_id not in unreviewed_ids
    }
    reviewed_severities: dict[str, set[str]] = {}
    for item in actual:
        if accepted(item, reviewed) and item.rule_id not in unreviewed_ids:
            reviewed_severities.setdefault(item.rule_id, set()).add(item.severity)
    return nonblocking_ids, reviewed_severities


def _severity_from_grype_rule(rule: dict[str, Any]) -> str | None:
    short_description = rule.get("shortDescription")
    text = short_description.get("text") if isinstance(short_description, dict) else None
    if not isinstance(text, str):
        return None
    normalized = f" {text.casefold()} "
    matches = [
        severity
        for severity in KNOWN_SEVERITIES
        if f" {severity.casefold()} vulnerability " in normalized
    ]
    return matches[0] if len(matches) == 1 else None


def _sarif_rule_severities(run: dict[str, Any]) -> dict[str, str | None]:
    tool = run.get("tool")
    driver = tool.get("driver") if isinstance(tool, dict) else None
    rules = driver.get("rules") if isinstance(driver, dict) else None
    if not isinstance(rules, list):
        return {}

    severities: dict[str, str | None] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("SARIF run contains an invalid rule")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise ValueError("SARIF rule is missing its id")
        severities[rule_id] = None if rule_id in severities else _severity_from_grype_rule(rule)
    return severities


def filter_sarif(
    sarif: dict[str, Any],
    nonblocking_rule_ids: set[str],
    reviewed_severities: dict[str, set[str]],
) -> tuple[int, int]:
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
        rule_severities = _sarif_rule_severities(run)
        filtered_results: list[dict[str, Any]] = []
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("SARIF run contains an invalid result")
            rule_id = result.get("ruleId")
            if not isinstance(rule_id, str) or not rule_id:
                raise ValueError("SARIF result is missing ruleId")
            severity = rule_severities.get(rule_id)
            # Contradictory or ambiguous SARIF identity is never suppression evidence.
            index = result.get("ruleIndex")
            rules = run.get("tool", {}).get("driver", {}).get("rules", [])
            if index is not None and (
                type(index) is not int
                or index < 0
                or index >= len(rules)
                or rules[index].get("id") != rule_id
            ):
                severity = None
            omit_nonblocking = (
                severity not in BLOCKING_SEVERITIES
                and severity is not None
                and rule_id in nonblocking_rule_ids
            )
            omit_reviewed = severity in reviewed_severities.get(rule_id, set())
            if omit_nonblocking or omit_reviewed:
                removed += 1
            else:
                filtered_results.append(result)
        run["results"] = filtered_results
        remaining += len(filtered_results)
    return removed, remaining


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sarif", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        sarif = read_object(args.sarif)
        report = read_object(args.report)
        baseline = read_object(args.baseline)
        nonblocking_rule_ids, reviewed_severities = _omission_evidence(report, baseline, args.image)
        removed, remaining = filter_sarif(sarif, nonblocking_rule_ids, reviewed_severities)
        args.output.write_text(json.dumps(sarif, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Container SARIF filtering failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"Actionable SARIF for {args.image}: {remaining} unreviewed finding(s); "
        f"{removed} reviewed or nonblocking finding(s) omitted."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
