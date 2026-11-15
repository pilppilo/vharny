"""Focused checks for the retained EXP-0001 supervisor-policy bundle."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "evidence" / "phase-0003-supervisor-policy"

sys.path.insert(0, str(ROOT))
from tools import exp_0001_supervisor_policy as evidence  # noqa: E402


def _results() -> dict[str, object]:
    return json.loads((BUNDLE / "results.json").read_text(encoding="utf-8"))


def test_exp_0001_bundle_replays_and_reaggregates() -> None:
    """Every retained stream replays and the table recomputes identically."""
    assert evidence.verify_bundle(BUNDLE) == []


def test_exp_0001_selection_follows_predeclared_tolerances() -> None:
    """The recorded selection matches the predeclared acceptance rules."""
    results = _results()
    assert results["selected_policy"] == "P1"
    assert results["baseline_reproducible"] is True
    assert results["censored_runs"] == []
    p1 = results["candidates"]["P1"]
    assert p1["meets_acceptance_tolerance"] is True
    assert p1["rejected"] is False
    assert p1["stagnation_pairs_improved"] == 20
    assert p1["false_triggers_productive"] == 0
    assert p1["false_triggers_exploring"] == {}
    p2 = results["candidates"]["P2"]
    assert p2["meets_acceptance_tolerance"] is True
    assert sum(p2["false_triggers_exploring"].values()) > 0


def test_exp_0001_event_stream_exports_replay() -> None:
    """A retained event-stream export rebuilds the recorded run outcome."""
    summary_path = BUNDLE / "runs" / "stalled" / "s0" / "P1" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    events = evidence._read_events(summary_path.parent / "events.jsonl.gz")
    counters = evidence.derive_counters(events)
    assert summary["accepted"] is True
    assert counters.commits == 1
    assert counters.model_calls == summary["counters"]["model_calls"]
    baseline = json.loads(
        (BUNDLE / "runs" / "stalled" / "s0" / "P0" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert baseline["accepted"] is False
