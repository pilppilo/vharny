"""Focused tests for retained PHASE-0002 evidence verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "evidence" / "phase-0002-operator-steering-resume"
sys.path.insert(0, str(ROOT))

from tools import phase2_operator_evidence as evidence  # noqa: E402
from vharness.agent.artifacts import ArtifactStore  # noqa: E402
from vharness.agent.environments import SoftwareFixture  # noqa: E402


def _skip_unless_pinned_revision_is_present(bundle: Path) -> None:
    """This copy has no Harny history, so a missing pin cannot be verified."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    source = manifest.get("source")
    revision = source.get("git_revision") if isinstance(source, dict) else None
    if not isinstance(revision, str):
        return
    present = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if present.returncode != 0:
        pytest.skip(f"pinned source revision {revision} is not in this repository")


def _copy_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLE, bundle)
    return bundle


def _file_hashes(bundle: Path) -> dict[str, str]:
    return {
        path.relative_to(bundle)
        .as_posix(): hashlib.sha256(path.read_bytes())
        .hexdigest()
        for path in bundle.rglob("*")
        if path.is_file()
    }


def test_verify_rejects_file_tampering_without_mutating_bundle(
    tmp_path: Path,
) -> None:
    """A changed retained file fails verification and stays unchanged."""
    bundle = _copy_bundle(tmp_path)
    (bundle / "report.json").write_text("tampered\n", encoding="utf-8")
    before = _file_hashes(bundle)

    with pytest.raises(RuntimeError, match="manifest hash or size mismatch"):
        evidence.verify_bundle(bundle, ROOT)

    assert _file_hashes(bundle) == before


def test_verify_rejects_tampered_stored_fresh_projection(tmp_path: Path) -> None:
    """Updating the manifest hash cannot make a changed fresh record valid."""
    _skip_unless_pinned_revision_is_present(BUNDLE)
    bundle = _copy_bundle(tmp_path)
    fresh_path = bundle / "fresh-process-reopen.json"
    fresh = json.loads(fresh_path.read_text(encoding="utf-8"))
    fresh["runs"][0]["replay_status"] = "tampered"
    fresh_path.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["fresh-process-reopen.json"] = {
        "sha256": hashlib.sha256(fresh_path.read_bytes()).hexdigest(),
        "size": fresh_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="stored fresh-process verification mismatch"
    ):
        evidence.verify_bundle(bundle, ROOT)


def test_verify_recomputes_report_after_manifest_hash_update(tmp_path: Path) -> None:
    """A changed report cannot be made valid by changing its manifest hash."""
    _skip_unless_pinned_revision_is_present(BUNDLE)
    bundle = _copy_bundle(tmp_path)
    report_path = bundle / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["primary_run"]["status"] = "running"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["report.json"] = {
        "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "size": report_path.stat().st_size,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="primary report status mismatch"):
        evidence.verify_bundle(bundle, ROOT)


def test_safe_bundle_path_rejects_reparse_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Junction-like components are rejected before they can escape containment."""
    bundle = tmp_path / "bundle"
    component = bundle / "nested"
    component.mkdir(parents=True)
    monkeypatch.setattr(evidence, "_is_reparse_point", lambda path: path == component)

    with pytest.raises(RuntimeError, match="reparse point"):
        evidence._safe_bundle_path(bundle, "nested/file", "test path")


def test_fresh_process_environment_is_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh evidence subprocesses do not inherit unrelated operator variables."""
    monkeypatch.setenv("UNRELATED_EVIDENCE_SECRET", "must-not-cross-boundary")
    environment = evidence._python_environment(ROOT)

    assert "UNRELATED_EVIDENCE_SECRET" not in environment
    assert set(environment) <= {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "PYTHONPATH",
        "PYTHONIOENCODING",
    }
    assert environment["PYTHONPATH"] == str((ROOT / "src").resolve())
    assert environment["PYTHONIOENCODING"] == "utf-8"


def test_artifact_inventory_allows_only_validated_fixture_checkpoint(
    tmp_path: Path,
) -> None:
    """Allow adapter checkpoint metadata while continuing to reject hidden files."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    journal_path = bundle / "session.sqlite3"
    journal_path.touch()
    artifacts_path = bundle / "artifacts"
    store = ArtifactStore(artifacts_path)
    store.register_journal(journal_path)
    fixture = SoftwareFixture(store)
    fixture.workspace_record(
        {"main.py": "value = 1\n"}, revision="1", tested=True, check_passed=True
    )

    restored = SoftwareFixture(ArtifactStore(artifacts_path))
    assert restored.describe().initial_state == fixture.describe().initial_state
    facts = {"journal_pointer": "../session.sqlite3", "reachable_artifacts": []}
    evidence._validate_artifact_facts(bundle, journal_path, artifacts_path, facts)

    manifest = {
        "files": {
            path.relative_to(bundle).as_posix(): {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
            for path in bundle.rglob("*")
            if path.is_file()
        }
    }
    evidence._validate_bundle_manifest(bundle, manifest)
    state_path = artifacts_path / ".software-fixture-state"
    state_path.write_bytes(state_path.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="manifest hash or size mismatch"):
        evidence._validate_bundle_manifest(bundle, manifest)

    (artifacts_path / ".unrecognized-metadata").write_text("unexpected")
    with pytest.raises(RuntimeError, match="unexpected artifact-store file"):
        evidence._validate_artifact_facts(bundle, journal_path, artifacts_path, facts)


def test_verify_survives_fresh_autocrlf_clone(tmp_path: Path) -> None:
    """A fresh Windows-style clone preserves the raw bytes hashed by evidence."""
    _skip_unless_pinned_revision_is_present(BUNDLE)
    checkout = tmp_path / "checkout"
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=true",
            "clone",
            "--local",
            str(ROOT),
            str(checkout),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "core.autocrlf=true",
            "checkout",
            "--detach",
            revision,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    exported_bundle = checkout / "evidence" / "phase-0002-operator-steering-resume"
    evidence.verify_bundle(exported_bundle, checkout)


def test_cli_assertion_rejects_missing_required_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A CLI regression in required output fails generation assertions."""

    def fake_run(
        _arguments: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, env, capture_output, text, check
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="STATUS running\n", stderr=""
        )

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="omitted stdout proof"):
        evidence._run_cli(
            tmp_path,
            ROOT,
            "assertion-test",
            "status",
            expected_returncode=0,
            stdout_contains=("PLAN ",),
        )
