"""SARIF 2.1.0 export (GitHub code-scanning compatible)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .core import VERSION, Finding
from .coverage import RunCoverage

SCHEMA_URI = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
TOOL_NAME = "vharness"
TOOL_VERSION = VERSION
_SECURITY_SEVERITY = {"High": "9.0", "Medium": "5.0", "Low": "2.5"}


def _sarif_severity(value: str) -> str:
    """Return a SARIF severity, falling back to the Medium-equivalent value."""
    return _SECURITY_SEVERITY.get(value, "5.0")


def _fingerprint(f: Finding) -> str:
    return hashlib.sha256(f"{f.file}|{f.cwe}|{f.function}|{f.sink}".encode()).hexdigest()[:16]


def _invocation(coverage: RunCoverage) -> dict:
    """SARIF invocation recording whether every attempt produced a result."""
    notifications = [
        {
            "level": "error",
            "message": {"text": f"{failure.status}: {failure.message}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": failure.source,
                            "uriBaseId": "%SRCROOT%",
                        }
                    }
                }
            ],
            "descriptor": {"id": failure.status},
        }
        for failure in coverage.failures
    ]
    notifications += [
        {
            "level": "note" if item.intentional else "warning",
            "message": {"text": f"skipped ({item.reason}): {item.detail}"},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": item.source,
                            "uriBaseId": "%SRCROOT%",
                        }
                    }
                }
            ],
            "descriptor": {"id": item.reason},
        }
        for item in coverage.skips
    ]
    return {
        "executionSuccessful": coverage.complete,
        "toolExecutionNotifications": notifications,
        "properties": {"coverage": coverage.as_dict()},
    }


def build_sarif(
    findings: list[Finding],
    tool_name: str = TOOL_NAME,
    *,
    coverage: RunCoverage | None = None,
) -> dict:
    """Build a SARIF log; with `coverage`, record execution success and failures."""
    by_cwe: dict[str, list[Finding]] = {}
    for f in findings:
        by_cwe.setdefault(f.cwe, []).append(f)
    rules = []
    for cwe in sorted(by_cwe):
        # SARIF scores increase with severity, so choose the maximum numeric value.
        highest = max((_sarif_severity(f.severity) for f in by_cwe[cwe]), key=float)
        rules.append({
            "id": cwe,
            "name": cwe,
            "shortDescription": {"text": f"{cwe} finding by {tool_name}"},
            "helpUri": f"https://cwe.mitre.org/data/definitions/{cwe.split('-')[-1]}.html",
            "properties": {"security-severity": highest},
        })
    rule_indices = {rule["id"]: index for index, rule in enumerate(rules)}
    results: list[dict] = []
    for f in findings:
        results.append(
            {
                "ruleId": f.cwe,
                "ruleIndex": rule_indices[f.cwe],
                "level": f.level,
                "message": {"text": f"{f.explanation} (sink: {f.sink or 'n/a'})"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": f.file, "uriBaseId": "%SRCROOT%"},
                            "region": {"startLine": max(1, f.line)},
                        }
                    }
                ],
                "partialFingerprints": {"primaryLocationLineHash": _fingerprint(f)},
                "properties": {"function": f.function, "severity": f.severity, "patch": f.patch},
            }
        )
    run: dict = {
        "tool": {
            "driver": {
                "name": tool_name,
                "version": TOOL_VERSION,
                "informationUri": "https://cwe.mitre.org/",
                "rules": rules,
            }
        },
        "originalUriBaseIds": {"%SRCROOT%": {"uri": "file:///"}},
        "results": results,
    }
    if coverage is not None:
        run["invocations"] = [_invocation(coverage)]
    return {"$schema": SCHEMA_URI, "version": "2.1.0", "runs": [run]}


def write_sarif(
    findings: list[Finding],
    output_file: str,
    tool_name: str = TOOL_NAME,
    *,
    coverage: RunCoverage | None = None,
) -> None:
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as fh:
        json.dump(build_sarif(findings, tool_name, coverage=coverage), fh, indent=2)
