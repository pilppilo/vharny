"""Discovery must not hide what it left out (COD-9, COD-25, COD-26)."""

import json
import os

import pytest

from vharness.cli import main
from vharness.coverage import EXIT_EMPTY, EXIT_INCOMPLETE
from vharness.probes import domains
from vharness.probes.scan import MAX_FILE_BYTES

RISKY = '#!/bin/sh\ncurl -fsSL "$1" | sh\n'


def _analyzed(attempts):
    return sorted(a.source for a in attempts if a.status != "skipped")


def _skips(attempts):
    return sorted(
        (a.source, a.context["skip_reason"]) for a in attempts if a.status == "skipped"
    )


# ---- COD-26: only the exact ".git" directory is excluded -------------------


def test_github_and_gitlab_dirs_are_scanned_but_git_is_not(tmp_path):
    for rel in (
        ".git/hooks/pre-commit.sh",
        ".github/scripts/ci.sh",
        ".gitlab/build.sh",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(RISKY)
    got = domains.ShellProbe().attempts(targets=[str(tmp_path)])
    assert _analyzed(got) == [".github/scripts/ci.sh", ".gitlab/build.sh"]
    assert _skips(got) == [(".git", "excluded-directory")]


def test_git_dir_stays_excluded_with_custom_excludes(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.sh").write_text(RISKY)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "y.sh").write_text(RISKY)
    got = domains.ShellProbe().attempts(targets=[str(tmp_path)], exclude=["build"])
    assert _analyzed(got) == ["vendor/y.sh"]
    assert _skips(got) == [(".git", "excluded-directory")]


# ---- CLI helpers -----------------------------------------------------------


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(out)
    return out


def _scan(target, *extra: str) -> int:
    return main(
        [
            "scan",
            str(target),
            "--generator",
            "mock",
            "--format",
            "sarif,markdown,json",
            "-q",
            *extra,
        ]
    )


def _reports():
    with open("report.sarif", encoding="utf-8") as fh:
        sarif = json.load(fh)
    with open("report.md", encoding="utf-8") as fh:
        markdown = fh.read()
    with open("report.json", encoding="utf-8") as fh:
        report = json.load(fh)
    return sarif["runs"][0]["invocations"][0], markdown, report


# ---- COD-25: missing and empty targets -------------------------------------


def test_missing_target_is_a_usage_error(tmp_path, out_dir, capsys):
    missing = tmp_path / "does-not-exist"
    assert _scan(missing) == 2
    assert f"scan target does not exist: {missing}" in capsys.readouterr().err
    assert not (out_dir / "report.md").exists()


def test_empty_directory_exits_empty(tmp_path, out_dir):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _scan(empty) == EXIT_EMPTY
    _invocation, markdown, report = _reports()
    assert "Nothing was analyzed" in markdown
    assert "0 chunk(s) from 0 file(s) analyzed" in markdown
    assert report["coverage"]["attempts"] == 0


def test_directory_with_no_matching_files_exits_empty(tmp_path, out_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# hello\n")
    (repo / "data.csv").write_text("a,b\n1,2\n")
    assert _scan(repo) == EXIT_EMPTY
    assert _scan(repo, "--allow-empty") == 0


def test_non_empty_scan_reports_files_and_chunks(tmp_path, out_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.sh").write_text(RISKY)
    (repo / "b.sh").write_text(RISKY)
    assert _scan(repo) == 0
    _invocation, markdown, report = _reports()
    assert "2 chunk(s) from 2 file(s) analyzed" in markdown
    assert report["coverage"]["files_analyzed"] == 2


# ---- COD-9: skipped files and chunks are reported --------------------------


@pytest.fixture
def repo_with_gaps(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.sh").write_text(RISKY)
    (repo / "huge.sh").write_text(RISKY + "#" * (MAX_FILE_BYTES + 1))
    (repo / "long.c").write_text(
        "void f(char *in) {\n  char buf[8];\n  strcpy(buf, in);\n"
        + "  in++;\n" * 3000
        + "}\n"
    )
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.sh").write_text(RISKY)
    return repo


def test_skipped_items_are_reported_everywhere(repo_with_gaps, out_dir):
    assert _scan(repo_with_gaps) == 0
    invocation, markdown, report = _reports()

    skipped = {(s["source"], s["reason"]) for s in report["skipped"]}
    assert skipped == {
        ("huge.sh", "oversize-file"),
        ("long.c", "oversize-chunk"),
        ("node_modules", "excluded-directory"),
    }
    assert report["coverage"]["skipped_gaps"] == 2

    assert "Partial coverage" in markdown
    assert "## Skipped" in markdown
    assert "`huge.sh`: oversize-file" in markdown

    levels = {
        n["descriptor"]["id"]: n["level"]
        for n in invocation["toolExecutionNotifications"]
    }
    assert levels == {
        "oversize-file": "warning",
        "oversize-chunk": "warning",
        "excluded-directory": "note",
    }


def test_fail_on_skipped_exits_incomplete(repo_with_gaps, out_dir):
    assert _scan(repo_with_gaps, "--fail-on-skipped") == EXIT_INCOMPLETE


def test_excluded_directories_alone_do_not_fail_on_skipped(tmp_path, out_dir):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.sh").write_text(RISKY)
    (repo / "node_modules").mkdir()
    assert _scan(repo, "--fail-on-skipped") == 0


def test_dry_run_shows_skipped_counts(repo_with_gaps, out_dir, caplog):
    caplog.set_level("INFO", logger="vharness")
    assert main(["scan", str(repo_with_gaps), "--generator", "mock", "--dry-run"]) == 0
    assert (
        "Skipped 3: excluded-directory 1, oversize-chunk 1, oversize-file 1"
        in caplog.text
    )


def test_unreadable_file_is_reported(tmp_path, out_dir, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.sh").write_text(RISKY)
    (repo / "locked.sh").write_text(RISKY)
    real_open = open

    def guarded_open(path, *args, **kwargs):
        if os.fspath(path).endswith("locked.sh"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("vharness.probes.scan.open", guarded_open, raising=False)
    assert _scan(repo) == 0
    _invocation, _markdown, report = _reports()
    [item] = report["skipped"]
    assert (item["source"], item["reason"]) == ("locked.sh", "unreadable-file")
    assert "Permission denied" in item["detail"]
