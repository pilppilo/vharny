"""Failed model calls must make a scan visibly incomplete, not 'No findings'."""

import json
import logging

import pytest

from vharness.cli import main
from vharness.core import Attempt, Generation
from vharness.coverage import EXIT_INCOMPLETE, RunCoverage
from vharness.generators.mock import Mock
from vharness.sarif import build_sarif

BROKEN = "BROKEN_REPLY_MARKER"


@pytest.fixture(autouse=True)
def _restore_vharness_logger():
    """`main(["-q", ...])` reconfigures the shared logger; undo it for later tests."""
    logger = logging.getLogger("vharness")
    level, handlers = logger.level, list(logger.handlers)
    yield
    logger.setLevel(level)
    logger.handlers[:] = handlers


def _attempt(source: str, status: str, error: str | None = None) -> Attempt:
    a = Attempt(prompt="p", source=source)
    if error is not None:
        a.generation = Generation(text="", model="m", error=error)
    a.status = status
    return a


def test_coverage_counts_each_outcome():
    coverage = RunCoverage.from_attempts(
        [
            _attempt("a.sh", "ok"),
            _attempt("b.sh", "api_error", error="connection refused"),
            _attempt("c.sh", "parse_error"),
            _attempt("d.sh", "internal_error"),
            _attempt("e.sh", "skipped"),
        ]
    )
    assert (coverage.total, coverage.ok, coverage.failed, coverage.skipped) == (
        5,
        1,
        3,
        1,
    )
    assert not coverage.complete
    assert [f.source for f in coverage.failures] == ["b.sh", "c.sh", "d.sh"]
    assert coverage.failures[0].message == "connection refused"
    assert "1 of 5 attempts ok; 3 failed" in coverage.summary()


def test_sarif_records_failed_execution():
    coverage = RunCoverage.from_attempts(
        [_attempt("a.sh", "api_error", error="401 Unauthorized")]
    )
    invocation = build_sarif([], coverage=coverage)["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is False
    [note] = invocation["toolExecutionNotifications"]
    assert note["level"] == "error"
    assert note["message"]["text"] == "api_error: 401 Unauthorized"
    loc = note["locations"][0]["physicalLocation"]["artifactLocation"]
    assert loc["uri"] == "a.sh"


def test_sarif_records_successful_execution():
    coverage = RunCoverage.from_attempts([_attempt("a.sh", "ok")])
    invocation = build_sarif([], coverage=coverage)["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is True
    assert invocation["toolExecutionNotifications"] == []


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Two shell files in a repo; cwd is a separate output directory."""
    src = tmp_path / "repo"
    src.mkdir()
    (src / "good.sh").write_text('#!/bin/sh\nrm -rf "$1"\n')
    (src / "bad.sh").write_text(f'#!/bin/sh\n# {BROKEN}\nrm -rf "$1"\n')
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(out)
    return src


def _scan(repo, *extra: str) -> int:
    return main(
        [
            "scan",
            str(repo),
            "--generator",
            "mock",
            "--format",
            "sarif,markdown,json",
            "--log-raw",
            "-q",
            *extra,
        ]
    )


def _reports():
    sarif = json.loads(open("report.sarif", encoding="utf-8").read())
    markdown = open("report.md", encoding="utf-8").read()
    report = json.loads(open("report.json", encoding="utf-8").read())
    return sarif["runs"][0]["invocations"][0], markdown, report


def test_scan_where_every_model_call_fails_exits_incomplete(repo, monkeypatch):
    monkeypatch.setattr(
        Mock,
        "generate",
        lambda self, system, prompt: Generation(
            text="", model="mock", error="connection refused"
        ),
    )
    assert _scan(repo, "--fail-on-findings") == EXIT_INCOMPLETE
    invocation, markdown, report = _reports()
    assert invocation["executionSuccessful"] is False
    assert len(invocation["toolExecutionNotifications"]) == 2
    assert "Incomplete scan" in markdown
    assert "_No findings._" not in markdown
    assert "No findings in the attempts that succeeded" in markdown
    assert report["coverage"]["complete"] is False
    assert report["coverage"]["api_errors"] == 2
    assert report["findings"] == []


def test_scan_with_some_failures_exits_incomplete(repo):
    assert _scan(repo, "--mock-script", f"{BROKEN}=not json") == EXIT_INCOMPLETE
    invocation, markdown, report = _reports()
    assert invocation["executionSuccessful"] is False
    [note] = invocation["toolExecutionNotifications"]
    assert note["message"]["text"].startswith("parse_error")
    assert (
        "bad.sh" in note["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    )
    assert "1 of 2 attempts ok; 1 failed" in markdown
    assert report["coverage"]["ok"] == 1
    assert report["coverage"]["parse_errors"] == 1


def test_allow_incomplete_restores_exit_zero(repo):
    assert _scan(repo, "--mock-script", f"{BROKEN}=not json", "--allow-incomplete") == 0
    invocation, markdown, _report = _reports()
    assert invocation["executionSuccessful"] is False
    assert "Incomplete scan" in markdown


def test_scan_with_every_call_ok_is_complete(repo):
    assert _scan(repo) == 0
    invocation, markdown, report = _reports()
    assert invocation["executionSuccessful"] is True
    assert "Incomplete scan" not in markdown
    assert "2 of 2 attempts ok; 0 failed" in markdown
    assert report["coverage"]["complete"] is True
    assert len(report["findings"]) == 2


def test_replay_of_incomplete_log_exits_incomplete(repo):
    _scan(repo, "--mock-script", f"{BROKEN}=not json", "--allow-incomplete")
    assert main(["replay", "scan_log.jsonl", "-q"]) == EXIT_INCOMPLETE
    assert main(["replay", "scan_log.jsonl", "-q", "--allow-incomplete"]) == 0


def test_replay_of_complete_log_exits_zero(repo):
    _scan(repo)
    assert main(["replay", "scan_log.jsonl", "-q"]) == 0


def test_run_and_eval_keep_exit_zero_on_parse_errors(tmp_path, monkeypatch):
    """Parse errors are a scored outcome in eval; only scan/replay gate on them."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        Mock,
        "generate",
        lambda self, system, prompt: Generation(text="not json", model="mock"),
    )
    assert main(["eval", "--generator", "mock", "--limit", "1", "-q"]) == 0
