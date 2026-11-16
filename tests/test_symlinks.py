"""Planted symlinks must not leak files into prompts or redirect outputs.

COD-14 covers reading through symlinks during discovery; COD-28 covers writing
default report and log files through symlinks.
"""

import json
import os

import pytest

from vharness.cli import main
from vharness.outputs import OutputPathError, open_output
from vharness.probes import domains

if os.name != "posix":
    pytest.skip("symlink semantics are POSIX-specific here", allow_module_level=True)

RISKY = '#!/bin/sh\ncurl -fsSL "$1" | sh\n'
SECRET = '#!/bin/sh\n# SECRET-TOKEN-123\ncurl -fsSL "$1" | sh\n'


def _analyzed(attempts):
    return sorted(a.source for a in attempts if a.status != "skipped")


def _skips(attempts):
    return sorted(
        (a.source, a.context["skip_reason"]) for a in attempts if a.status == "skipped"
    )


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.sh").write_text(RISKY)
    outside = tmp_path / "home"
    outside.mkdir()
    (outside / "secret.sh").write_text(SECRET)
    return repo


# ---- COD-14: discovery stays inside the scan root ---------------------------


def test_file_symlink_outside_root_is_skipped_not_read(repo):
    (repo / "run.sh").symlink_to(repo.parent / "home" / "secret.sh")
    got = domains.ShellProbe().attempts(targets=[str(repo)])
    assert _analyzed(got) == ["ok.sh"]
    assert _skips(got) == [("run.sh", "outside-root")]
    assert not any("SECRET-TOKEN-123" in a.prompt for a in got)


def test_directory_symlink_outside_root_is_skipped(repo):
    (repo / "lib").symlink_to(repo.parent / "home", target_is_directory=True)
    got = domains.ShellProbe().attempts(targets=[str(repo)])
    assert _analyzed(got) == ["ok.sh"]
    assert _skips(got) == [("lib", "outside-root")]


def test_symlinks_inside_root_are_read_once(repo):
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.sh").write_text(RISKY + "# deep\n")
    (repo / "alias.sh").symlink_to(repo / "ok.sh")
    (repo / "alias-dir").symlink_to(repo / "sub", target_is_directory=True)
    got = domains.ShellProbe().attempts(targets=[str(repo)])
    assert _analyzed(got) == ["ok.sh", "sub/deep.sh"]
    assert _skips(got) == []


def test_symlink_loop_terminates(repo):
    (repo / "sub").mkdir()
    (repo / "sub" / "deep.sh").write_text(RISKY + "# deep\n")
    (repo / "sub" / "loop").symlink_to(repo, target_is_directory=True)
    (repo / "sub" / "self").symlink_to(repo / "sub", target_is_directory=True)
    got = domains.ShellProbe().attempts(targets=[str(repo)])
    assert _analyzed(got) == ["ok.sh", "sub/deep.sh"]


def test_follow_symlinks_reads_outside_files(repo):
    (repo / "run.sh").symlink_to(repo.parent / "home" / "secret.sh")
    got = domains.ShellProbe().attempts(targets=[str(repo)], follow_symlinks=True)
    assert _analyzed(got) == ["ok.sh", "run.sh"]


def test_scan_cli_reports_outside_symlink_and_warns_on_follow(
    repo, tmp_path, monkeypatch, caplog
):
    (repo / "run.sh").symlink_to(repo.parent / "home" / "secret.sh")
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.chdir(out)
    assert (
        main(["scan", str(repo), "--generator", "mock", "--format", "json", "-q"]) == 0
    )
    report = json.loads((out / "report.json").read_text())
    assert {"source": "run.sh", "reason": "outside-root"} == {
        k: v for k, v in report["skipped"][0].items() if k != "detail"
    }
    caplog.set_level("WARNING", logger="vharness")
    assert (
        main(
            ["scan", str(repo), "--generator", "mock", "--dry-run", "--follow-symlinks"]
        )
        == 0
    )
    assert "--follow-symlinks" in caplog.text


# ---- COD-28: default outputs are never written through symlinks ------------


@pytest.fixture
def planted(tmp_path, monkeypatch):
    """A repo used as cwd, a victim file outside it, and a scannable script."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "ok.sh").write_text(RISKY)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    monkeypatch.chdir(work)
    return work, victim


@pytest.mark.parametrize(
    "name", ["report.sarif", "report.md", "report.json", "scan_log.jsonl"]
)
def test_planted_default_output_symlink_is_refused(planted, capsys, name):
    work, victim = planted
    (work / name).symlink_to(victim)
    code = main(
        ["scan", ".", "--generator", "mock", "--format", "sarif,markdown,json", "-q"]
    )
    assert code == 2
    assert f"refusing to write {name}" in capsys.readouterr().err
    assert victim.read_text() == "precious\n"


def test_planted_eval_metrics_symlink_is_refused(planted, capsys):
    work, victim = planted
    (work / "eval_metrics.json").symlink_to(victim)
    assert main(["eval", "--generator", "mock", "--limit", "1", "-q"]) == 2
    assert "refusing to write eval_metrics.json" in capsys.readouterr().err
    assert victim.read_text() == "precious\n"


def test_explicit_output_may_be_a_symlink(planted):
    work, victim = planted
    (work / "chosen.md").symlink_to(victim)
    code = main(
        [
            "scan",
            ".",
            "--generator",
            "mock",
            "--format",
            "markdown",
            "-q",
            "--out",
            "chosen",
            "--log-file",
            "chosen.jsonl",
        ]
    )
    assert code == 0
    assert victim.read_text().startswith("# Security scan report")


def test_open_output_refuses_a_symlink_swapped_in_after_the_check(
    tmp_path, monkeypatch
):
    victim = tmp_path / "victim.txt"
    victim.write_text("precious\n")
    link = tmp_path / "report.md"
    link.symlink_to(victim)
    monkeypatch.setattr("vharness.outputs.os.path.islink", lambda _path: False)
    with pytest.raises(OutputPathError, match="refusing to write"):
        open_output(str(link), "w", follow_symlinks=False)
    assert victim.read_text() == "precious\n"
