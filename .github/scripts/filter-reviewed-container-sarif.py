#!/usr/bin/env python3
"""Remove reviewed container findings before uploading actionable SARIF."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

BLOCKING_SEVERITIES = {"High", "Critical"}
KNOWN_SEVERITIES = {"Unknown", "Negligible", "Low", "Medium", *BLOCKING_SEVERITIES}


@dataclass(frozen=True)
class Finding:
    identifier: str
    package: str
    severity: str

    @property
    def rule_id(self) -> str:
        return f"{self.identifier}-{self.package}"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _report_findings(report: dict[str, Any]) -> set[Finding]:
    matches = report.get("matches")
    if not isinstance(matches, list):
        raise ValueError("Grype report is missing its matches list")
    findings: set[Finding] = set()
    for match in matches:
        if not isinstance(match, dict):
            raise ValueError("Grype report contains an invalid match")
        vulnerability = match.get("vulnerability")
        artifact = match.get("artifact")
        if not isinstance(vulnerability, dict) or not isinstance(artifact, dict):
            raise ValueError("Grype match is missing vulnerability or artifact data")
        severity = vulnerability.get("severity")
        if severity not in KNOWN_SEVERITIES:
            raise ValueError("Grype finding has an invalid severity")
        identifier = vulnerability.get("id")
        package = artifact.get("name")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Grype finding is missing its vulnerability identifier")
        if not isinstance(package, str) or not package:
            raise ValueError("Grype finding is missing its package name")
        findings.add(Finding(identifier, package, severity))
    return findings


def _reviewed_findings(baseline: dict[str, Any], image: str) -> set[Finding]:
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

    findings: set[Finding] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"Container vulnerability profile {profile_name!r} is invalid")
        identifier = entry.get("id")
        package = entry.get("package")
        severity = entry.get("severity")
        if (
            not isinstance(identifier, str)
            or not isinstance(package, str)
            or severity not in BLOCKING_SEVERITIES
        ):
            raise ValueError(f"Container vulnerability profile {profile_name!r} is invalid")
        findings.add(Finding(identifier, package, severity))
    return findings


def _omission_evidence(
    report: dict[str, Any],
    baseline: dict[str, Any],
    image: str,
) -> tuple[set[str], dict[str, set[str]]]:
    actual = _report_findings(report)
    reviewed = _reviewed_findings(baseline, image)
    blocking = {finding for finding in actual if finding.severity in BLOCKING_SEVERITIES}
    unreviewed_rule_ids = {finding.rule_id for finding in blocking - reviewed}
    nonblocking_rule_ids = {
        finding.rule_id
        for finding in actual
        if finding.severity not in BLOCKING_SEVERITIES
        and finding.rule_id not in unreviewed_rule_ids
    }
    reviewed_severities: dict[str, set[str]] = {}
    for finding in blocking & reviewed:
        if finding.rule_id not in unreviewed_rule_ids:
            reviewed_severities.setdefault(finding.rule_id, set()).add(finding.severity)
    return nonblocking_rule_ids, reviewed_severities


def _severity_from_security_score(value: object) -> str | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        score = float(value)
    except ValueError:
        return None
    if not 0 <= score <= 10:
        return None
    if score >= 9:
        return "Critical"
    if score >= 7:
        return "High"
    if score >= 4:
        return "Medium"
    if score > 0:
        return "Low"
    return "Negligible"


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
        properties = rule.get("properties")
        security_score = (
            properties.get("security-severity") if isinstance(properties, dict) else None
        )
        severities[rule_id] = _severity_from_security_score(security_score)
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
        sarif = _read_object(args.sarif)
        report = _read_object(args.report)
        baseline = _read_object(args.baseline)
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
