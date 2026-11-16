"""Fail CI when a test is skipped without being on the explicit allow-list.

Silent skips (a missing optional dependency, a pinned revision that no longer
exists) let coverage disappear unnoticed. Every skip must be listed, with the
issue that tracks it, in the allow-list file; a listed test that no longer
skips is reported too, so the list shrinks as issues are fixed.

An entry may end with a Python condition, ``[python<3.12]`` or
``[python>=3.12]``; it then applies only on matching interpreters (for tests
whose dependency exists only on some Python versions).

Usage: python tools/check_skips.py JUNIT_XML ALLOWLIST
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkipReport:
    """Skipped tests compared with the allow-list."""

    unexpected: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when every skip is allowed and every allowed test still skips."""
        return not self.unexpected and not self.stale


def normalize_nodeid(nodeid: str) -> str:
    """Turn ``tests/a/test_b.py::Cls::test`` into ``tests.a.test_b.Cls::test``."""
    path, *parts = nodeid.strip().split("::")
    if not parts:
        raise ValueError(f"allow-list entry {nodeid!r} is not a pytest node id")
    module = path.removesuffix(".py").replace("/", ".")
    classname = ".".join([module, *parts[:-1]])
    return f"{classname}::{parts[-1]}"


_CONDITION = re.compile(
    r"^(?P<nodeid>\S+)\s*(?:\[python(?P<op><|>=)(?P<ver>\d+\.\d+)\])?$"
)


def condition_holds(op: str, version: str, current: tuple[int, int]) -> bool:
    """Evaluate ``python<X.Y`` or ``python>=X.Y`` against ``current``."""
    major, minor = (int(part) for part in version.split("."))
    return current < (major, minor) if op == "<" else current >= (major, minor)


def read_allowlist(path: Path, current: tuple[int, int] | None = None) -> set[str]:
    """Node ids that apply to ``current`` Python; ``#`` starts a comment."""
    running = current or (sys.version_info.major, sys.version_info.minor)
    entries: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        match = _CONDITION.match(text)
        if match is None:
            raise ValueError(f"allow-list entry {text!r} is not a pytest node id")
        if match["op"] and not condition_holds(match["op"], match["ver"], running):
            continue
        entries.add(normalize_nodeid(match["nodeid"]))
    return entries


def read_skipped(junit_xml: Path) -> set[str]:
    """Skipped tests in a pytest JUnit XML report, as ``classname::name``."""
    skipped: set[str] = set()
    for case in ET.parse(junit_xml).iter("testcase"):
        if case.find("skipped") is not None:
            skipped.add(f"{case.get('classname')}::{case.get('name')}")
    return skipped


def compare(skipped: set[str], allowed: set[str]) -> SkipReport:
    """Report skips missing from the allow-list and allowed tests that ran."""
    return SkipReport(
        unexpected=tuple(sorted(skipped - allowed)),
        stale=tuple(sorted(allowed - skipped)),
    )


def main(argv: list[str]) -> int:
    """Entry point; returns the process exit code."""
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    junit_xml, allowlist = Path(argv[0]), Path(argv[1])
    report = compare(read_skipped(junit_xml), read_allowlist(allowlist))
    for test in report.unexpected:
        print(f"skipped but not allow-listed: {test}", file=sys.stderr)
    for test in report.stale:
        print(
            f"allow-listed but no longer skipped (remove it): {test}", file=sys.stderr
        )
    if report.ok:
        print("all skipped tests are allow-listed")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
