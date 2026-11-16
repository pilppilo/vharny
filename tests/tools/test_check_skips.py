"""The CI skip gate (tools/check_skips.py) for COD-15."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import check_skips  # noqa: E402

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.agent.test_a" name="test_ran" />
  <testcase classname="tests.agent.test_a" name="test_known_skip">
    <skipped message="tracked" />
  </testcase>
  <testcase classname="tests.agent.test_b.TestGroup" name="test_case[x]">
    <skipped message="tracked" />
  </testcase>
</testsuite></testsuites>
"""


@pytest.fixture
def junit(tmp_path: Path) -> Path:
    path = tmp_path / "results.xml"
    path.write_text(JUNIT, encoding="utf-8")
    return path


def _allowlist(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "allow.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_normalize_nodeid_matches_junit_names():
    assert check_skips.normalize_nodeid("tests/agent/test_a.py::test_x") == (
        "tests.agent.test_a::test_x"
    )
    assert check_skips.normalize_nodeid("tests/b.py::TestGroup::test_case[x]") == (
        "tests.b.TestGroup::test_case[x]"
    )


def test_all_skips_allow_listed_passes(tmp_path, junit, capsys):
    allow = _allowlist(
        tmp_path,
        "# comment\n"
        "tests/agent/test_a.py::test_known_skip  # COD-1\n"
        "tests/agent/test_b.py::TestGroup::test_case[x]\n",
    )
    assert check_skips.main([str(junit), str(allow)]) == 0
    assert "all skipped tests are allow-listed" in capsys.readouterr().out


def test_unlisted_skip_fails(tmp_path, junit, capsys):
    allow = _allowlist(tmp_path, "tests/agent/test_a.py::test_known_skip\n")
    assert check_skips.main([str(junit), str(allow)]) == 1
    assert (
        "skipped but not allow-listed: tests.agent.test_b.TestGroup::test_case[x]"
        in capsys.readouterr().err
    )


def test_stale_entry_fails(tmp_path, junit, capsys):
    allow = _allowlist(
        tmp_path,
        "tests/agent/test_a.py::test_known_skip\n"
        "tests/agent/test_b.py::TestGroup::test_case[x]\n"
        "tests/agent/test_a.py::test_ran\n",
    )
    assert check_skips.main([str(junit), str(allow)]) == 1
    assert "no longer skipped (remove it): tests.agent.test_a::test_ran" in (
        capsys.readouterr().err
    )


def test_entry_without_test_name_is_rejected(tmp_path, junit):
    allow = _allowlist(tmp_path, "tests/agent/test_a.py\n")
    with pytest.raises(ValueError, match="not a pytest node id"):
        check_skips.main([str(junit), str(allow)])


def test_python_conditions_select_entries(tmp_path):
    allow = _allowlist(
        tmp_path,
        "tests/a.py::test_old  [python<3.12]  # only on old Pythons\n"
        "tests/a.py::test_new [python>=3.12]\n"
        "tests/a.py::test_always\n",
    )
    assert check_skips.read_allowlist(allow, (3, 10)) == {
        "tests.a::test_old",
        "tests.a::test_always",
    }
    assert check_skips.read_allowlist(allow, (3, 13)) == {
        "tests.a::test_new",
        "tests.a::test_always",
    }


def test_malformed_condition_is_rejected(tmp_path):
    allow = _allowlist(tmp_path, "tests/a.py::test_x [py3]\n")
    with pytest.raises(ValueError, match="not a pytest node id"):
        check_skips.read_allowlist(allow, (3, 13))


def test_repository_allowlist_is_well_formed():
    entries = check_skips.read_allowlist(ROOT / ".github" / "ci-skip-allowlist.txt")
    assert entries
