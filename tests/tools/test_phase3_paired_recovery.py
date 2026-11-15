"""Focused checks for the phase 3 paired-recovery tooling."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT))
from tools import phase3_paired_recovery as p3  # noqa: E402


def test_aggregate_accepts_bundle_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle outside the repository aggregates with an absolute label."""
    monkeypatch.setattr(p3, "SOURCE_ROOT", tmp_path / "checkout")
    bundle = tmp_path / "bundle"
    (bundle / "runs").mkdir(parents=True)
    resolved = bundle.resolve()
    assert not resolved.is_relative_to(p3.SOURCE_ROOT)
    results = p3.write_aggregate(bundle)
    assert results["bundle"] == resolved.as_posix()
    assert (bundle / "results.json").is_file()


def test_aggregate_accepts_relative_bundle_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative bundle path resolves from the caller's working directory."""
    monkeypatch.setattr(p3, "SOURCE_ROOT", tmp_path / "checkout")
    bundle = tmp_path / "bundle"
    (bundle / "runs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    results = p3.write_aggregate(Path("bundle"))
    assert results["bundle"] == bundle.resolve().as_posix()
