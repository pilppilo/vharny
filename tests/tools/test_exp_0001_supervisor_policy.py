"""Focused checks for the EXP-0001 supervisor-policy evidence tool."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT))
from tools import exp_0001_supervisor_policy as exp1  # noqa: E402


def _fake_checkout(tmp_path: Path) -> Path:
    """Create a repository stand-in whose tool copy has identical bytes."""
    checkout = tmp_path / "checkout"
    (checkout / "tools").mkdir(parents=True)
    shutil.copyfile(exp1.SOURCE_ROOT / exp1.TOOL_RELPATH, checkout / exp1.TOOL_RELPATH)
    return checkout


def _write_minimal_bundle(bundle: Path) -> None:
    """Write a minimal bundle (manifest and aggregate, no retained runs)."""
    bundle.mkdir(parents=True, exist_ok=True)
    manifest = {
        "experiment": "EXP-0001",
        "document": "plans/experiments/exp-0001-supervisor-policy.md",
        "source": {
            "git_revision": "0" * 40,
            "git_dirty": False,
            "tool": exp1.TOOL_RELPATH,
            "tool_sha256": exp1._sha256_file(Path(exp1.__file__)),
        },
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    exp1.write_aggregate(bundle)


def test_bundle_label_records_repo_relative_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-repo bundles record repo-relative; anything else records absolute."""
    inside = exp1.SOURCE_ROOT / "evidence" / "some-bundle"
    assert exp1._bundle_label(inside) == "evidence/some-bundle"
    monkeypatch.setattr(exp1, "SOURCE_ROOT", tmp_path / "checkout")
    outside = tmp_path / "bundle"
    assert exp1._bundle_label(outside) == outside.resolve().as_posix()


def test_verify_and_aggregate_accept_bundle_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle outside the repository tree aggregates and verifies in full."""
    bundle = tmp_path / "bundle"
    run_dir = bundle / "runs" / "stalled" / "s0" / "P1"
    exp1._run_once(exp1.FIXTURES["stalled"], 0, "P1", run_dir)
    monkeypatch.setattr(exp1, "SOURCE_ROOT", _fake_checkout(tmp_path))
    _write_minimal_bundle(bundle)
    results = exp1.write_aggregate(bundle)
    assert results["bundle"] == bundle.resolve().as_posix()
    assert exp1.verify_bundle(bundle) == []


def test_verify_and_aggregate_accept_relative_bundle_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative bundle path resolves from the caller's working directory."""
    bundle = tmp_path / "bundle"
    _write_minimal_bundle(bundle)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(exp1, "SOURCE_ROOT", _fake_checkout(tmp_path))
    results = exp1.write_aggregate(Path("bundle"))
    assert results["bundle"] == bundle.resolve().as_posix()
    assert exp1.verify_bundle(Path("bundle")) == []


def test_bundle_remains_digest_gated(tmp_path: Path) -> None:
    """The tool digest gate governs a bundle at any location."""
    bundle = tmp_path / "bundle"
    _write_minimal_bundle(bundle)
    assert exp1.verify_bundle(bundle) == []
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    source = manifest["source"]
    assert isinstance(source, dict)
    source["tool_sha256"] = "0" * 64
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert exp1.verify_bundle(bundle) == ["tool sha256 does not match the manifest"]
