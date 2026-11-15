"""Focused checks for the retained PHASE-0003 failure-trace bundle."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "evidence" / "phase-0003-failure-traces"

sys.path.insert(0, str(ROOT))
from tools import phase3_failure_traces as evidence  # noqa: E402


def _report() -> dict[str, object]:
    return json.loads((BUNDLE / "report.json").read_text(encoding="utf-8"))


def test_failure_trace_bundle_replays_and_verifies() -> None:
    """The retained journal, hashes, and failure taxonomy all agree."""
    assert evidence.verify_bundle(BUNDLE) == []


def test_failure_trace_bundle_records_failure_then_repair() -> None:
    """A live failing test receipt precedes a passing one in the journal."""
    events = evidence._read_events(BUNDLE / "session.sqlite3")
    statuses = [
        evidence._mapping(event.payload.get("receipt")).get("status")
        for event in events
        if event.kind == "receipt_received"
    ]
    assert "failed" in statuses
    first_failed = statuses.index("failed")
    assert "succeeded" in statuses[first_failed + 1 :]
    report = _report()
    traces = report["failure_traces"]
    assert traces["present"] is True
    assert "VALUE is 0" in traces["failed_receipts"][0]["error"]
    assert report["acceptance_claims"] == {
        "scripted_action_sequence_supplied": False,
        "hidden_validator_data_supplied": False,
        "provider_rejected": False,
    }


def test_failure_trace_bundle_records_formal_stagnation() -> None:
    """The retained run exhibits the amended entrance criterion's stagnation."""
    sidecar = json.loads((BUNDLE / "stagnation.json").read_text(encoding="utf-8"))
    assert sidecar["stagnation_detected"] is True
    assert sidecar["decisions"] == 9
    assert sidecar["supported_progress"] == 0
    policy = sidecar["policy"]
    assert policy["version"] == "monitor.p1.v1"
    assert policy["window_calls"] == 8
    assert policy["repeat_threshold"] == 3
    trigger = sidecar["first_trigger"]
    assert trigger["repeat_count"] >= policy["repeat_threshold"]
    assert trigger["window_calls"] <= policy["window_calls"]
    assert trigger["window_supported_progress"] == 0
    assert trigger["fingerprint"].startswith("action:")
    events = evidence._read_events(BUNDLE / "session.sqlite3")
    event_ids = {event.event_id for event in events}
    assert set(trigger["input_event_ids"]) <= event_ids
    receipts = sidecar["failure_receipts"]
    assert receipts and receipts[0]["status"] == "failed"


def test_stagnation_sidecar_rebuilds_byte_identically(tmp_path: Path) -> None:
    """A lost stagnation.json rebuilds byte-identically from the journal."""
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLE, bundle)
    original = (bundle / "stagnation.json").read_bytes()
    (bundle / "stagnation.json").unlink()
    assert evidence.write_stagnation(bundle) == []
    assert (bundle / "stagnation.json").read_bytes() == original


def test_rebuild_report_from_the_journal_is_deterministic(tmp_path: Path) -> None:
    """A lost report.json rebuilds byte-identically from the retained journal."""
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLE, bundle)
    (bundle / "report.json").unlink()
    assert evidence.rebuild_report(bundle) == []
    first = (bundle / "report.json").read_bytes()
    assert evidence.rebuild_report(bundle) == []
    assert (bundle / "report.json").read_bytes() == first
