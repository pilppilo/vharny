"""Report evaluators: SARIF, Markdown, JSON. Each is independently selectable."""

from __future__ import annotations

import hashlib
import json
import os

from ..core import Attempt
from ..coverage import RunCoverage
from ..log import log
from ..sarif import build_sarif
from .base import Evaluator, register_builtin

_SEV_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def _all_findings(attempts: list[Attempt]):
    for a in attempts:
        yield from a.findings


def _out_path(run_info: dict, default: str, requested: str | None, ext: str) -> str:
    """Resolve output path: explicit per-format arg > base 'out' + ext > default."""
    if requested:
        return requested
    base = run_info.get("out")
    if base:
        return base if base.endswith(f".{ext}") else f"{base}.{ext}"
    return default


def _write_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


@register_builtin
class SarifReport(Evaluator):
    name = "sarif"
    help = "write a SARIF 2.1.0 report (GitHub code-scanning compatible)"

    def evaluate(self, attempts: list[Attempt], run_info: dict) -> None:
        path = _out_path(run_info, "report.sarif", run_info.get("sarif_out"), "sarif")
        sarif = build_sarif(
            list(_all_findings(attempts)), coverage=RunCoverage.from_attempts(attempts)
        )
        _write_parent(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sarif, fh, indent=2)
        log.info("wrote SARIF: %s", path)


@register_builtin
class MarkdownReport(Evaluator):
    name = "markdown"
    help = "write a human-readable Markdown issue report"

    def evaluate(self, attempts: list[Attempt], run_info: dict) -> None:
        path = _out_path(run_info, "report.md", run_info.get("markdown_out"), "md")
        findings = sorted(_all_findings(attempts), key=lambda f: (_SEV_ORDER.get(f.severity, 3), f.file, f.line))
        coverage = RunCoverage.from_attempts(attempts)
        out: list[str] = [
            "# Security scan report",
            "",
            f"**{len(findings)} findings** across {len({f.file for f in findings})} file(s).",
            "",
            f"**Coverage:** {coverage.summary()}.",
            "",
        ]
        if not coverage.complete:
            out += [
                f"> **Incomplete scan.** {coverage.failed} of {coverage.total} attempts "
                "failed, so the code they covered was not analyzed. The findings "
                "below come only from the attempts that succeeded.",
                "",
            ]
        by_file: dict[str, list] = {}
        for f in findings:
            by_file.setdefault(f.file, []).append(f)
        for file in sorted(by_file):
            out += [f"## `{file}`", ""]
            for f in by_file[file]:
                out += [
                    f"### {f.severity} · {f.cwe} — `{f.function or 'n/a'}` (line {f.line})",
                    "",
                    f"**Sink:** `{f.sink or 'n/a'}`",
                    "",
                    f.explanation,
                ]
                if f.patch:
                    out += ["", "**Suggested patch** (advisory — review before applying):", "", "```", f.patch, "```"]
                out.append("")
        if not findings:
            out.append(
                "_No findings._"
                if coverage.complete
                else "_No findings in the attempts that succeeded._"
            )
        if coverage.failures:
            out += ["## Failed attempts", ""]
            out += [
                f"- `{failure.source}`: {failure.status} — {failure.message}"
                for failure in coverage.failures
            ]
            out.append("")
        _write_parent(path)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        log.info("wrote markdown: %s", path)


@register_builtin
class JsonReport(Evaluator):
    name = "json"
    help = "write findings and attempt coverage as JSON"

    def evaluate(self, attempts: list[Attempt], run_info: dict) -> None:
        path = _out_path(run_info, "report.json", run_info.get("json_out"), "json")
        data = [
            {
                "file": f.file, "line": f.line, "function": f.function, "cwe": f.cwe,
                "severity": f.severity, "sink": f.sink, "explanation": f.explanation, "patch": f.patch,
            }
            for f in _all_findings(attempts)
        ]
        report = {
            "coverage": RunCoverage.from_attempts(attempts).as_dict(),
            "findings": data,
        }
        _write_parent(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log.info("wrote JSON: %s", path)


@register_builtin
class Summary(Evaluator):
    name = "summary"
    help = "print the run summary to stdout"

    def evaluate(self, attempts: list[Attempt], run_info: dict) -> None:
        info = run_info.get("run_info")
        if info is not None:
            log.info(
                "run %s: probes=%s attempts=%s ok=%s parse_errors=%s api_errors=%s internal_errors=%s "
                "findings=%s wall=%.1fs",
                info.run_id, info.probes, info.attempts_total, info.ok,
                info.parse_errors, info.api_errors, info.internal_errors, info.findings, info.wall_seconds,
            )
        gen = run_info.get("generator_summary")
        if gen:
            log.info("generator: %s", gen)
        errs = [a for a in attempts if a.status == "api_error"]
        if errs:
            log.warning("%s attempt(s) failed at the endpoint (first 3):", len(errs))
            for a in errs[:3]:
                log.warning("  %s: %s", a.source, a.generation.error if a.generation else "?")
