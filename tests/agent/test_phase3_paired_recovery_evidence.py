"""Bundle regression for the PHASE-0003 item-6 paired-recovery evidence.

The retained bundle pairs the unsupervised kernel against the supervised
``monitor.p1.v1`` kernel on labeled fixtures. These checks keep the bundle
honest: the exports replay, the aggregate recomputes, the paired benefit and
bounded cost stay within the reused EXP-0001 tolerances, rejected guidance stays
visible, and no external effect is duplicated.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "evidence" / "phase-0003-paired-recovery"

sys.path.insert(0, str(ROOT))
from tools import phase3_paired_recovery as evidence  # noqa: E402

pytestmark = pytest.mark.skipif(
    not BUNDLE.exists(), reason="paired-recovery bundle is not retained"
)


def _results() -> dict[str, object]:
    return json.loads((BUNDLE / "results.json").read_text(encoding="utf-8"))


def _summaries() -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(BUNDLE.glob("runs/*/s*/*/summary.json"))
    ]


def test_paired_recovery_bundle_replays_and_reaggregates() -> None:
    """Every retained stream replays and the result table recomputes."""
    assert evidence.verify_bundle(BUNDLE) == []


def test_paired_recovery_meets_reused_tolerances() -> None:
    """The recorded outcome follows the reused EXP-0001 tolerances."""
    results = _results()
    assert results["baseline_reproducible"] is True
    assert results["censored_runs"] == []
    tolerances = results["tolerances"]
    assert tolerances["source"] == "EXP-0001 predeclared paired-sample tolerances"
    assert tolerances["meets_acceptance"] is True
    assert tolerances["rejected"] is False
    assert tolerances["inconclusive"] is False
    benefit = results["benefit"]
    assert benefit["stagnation_pairs_improved"] == benefit["stagnation_pairs_total"]
    assert benefit["classes_meeting_3_of_5"] == 4
    assert results["uncertainty"]["point"] == 1.0
    assert results["uncertainty"]["wilson_95"][0] < 1.0


def test_every_stagnation_pair_recovers_under_supervision() -> None:
    """Supervised runs recover where their paired unsupervised runs do not."""
    index = {
        (summary["fixture"], summary["seed"], summary["condition"]): summary
        for summary in _summaries()
    }
    for fixture in evidence.STAGNATION_CLASSES:
        for seed in evidence.SEEDS:
            supervised = index[(fixture, seed, "supervised")]
            unsupervised = index[(fixture, seed, "unsupervised")]
            assert supervised["accepted"] is True
            assert unsupervised["accepted"] is False
            assert unsupervised["terminal"] == "model_budget_exhausted"
            assert supervised["actions_to_recovery"] is not None


def test_supervision_is_bounded_and_never_thrashes_controls() -> None:
    """Supervisor cost stays within allocation and controls never trigger."""
    results = _results()
    assert results["false_triggers"] == {}
    assert results["cost"]["allocation_exceeded_runs"] == 0
    for summary in _summaries():
        assert summary["supervisor_allocation_exceeded"] is False
        if summary["condition"] == "supervised":
            assert summary["supervisor_calls"] <= evidence.SUPERVISOR_CALL_ALLOCATION
        else:
            assert summary["supervisor_calls"] == 0
            assert summary["triggers"] == []


def test_rejected_and_replanned_guidance_remain_visible() -> None:
    """Guidance responses are recorded, not hidden, including rejection."""
    results = _results()
    decisions = results["guidance_decisions"]
    assert decisions["reject"] >= 1
    assert decisions["accept"] >= 1
    assert decisions["replan"] >= 1
    assert results["guidance"]["rejected"] == decisions["reject"]
    for summary in _summaries():
        if summary["condition"] != "supervised" or not summary["triggers"]:
            continue
        dispositions = summary["guidance"]
        total = sum(dispositions.values())
        assert total >= 1


def test_no_duplicate_external_effect_or_lineage_leak() -> None:
    """Each proposal is submitted once and failed attempts stay outside lineage."""
    for summary_path in sorted(BUNDLE.glob("runs/*/s*/*/summary.json")):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        events = evidence._read_events(summary_path.parent / "events.jsonl.gz")
        proposal_ids = [
            event.payload["proposal"]["proposal_id"]
            for event in events
            if event.kind == "action_intended"
        ]
        assert len(proposal_ids) == len(set(proposal_ids))
        counters = evidence.derive_counters(events)
        assert counters.commits <= 1
        assert counters.commits == (1 if summary["accepted"] else 0)
        assert summary["counters"]["attempts"] == counters.attempts


def test_unsupervised_condition_is_deterministic() -> None:
    """The baseline repeats byte-for-byte on its outcome fields."""
    for fixture in evidence.FIXTURE_CLASSES:
        for seed in evidence.SEEDS:
            base = BUNDLE / "runs" / fixture / f"s{seed}" / "unsupervised"
            repeat = BUNDLE / "runs" / fixture / f"s{seed}" / "unsupervised-repeat"
            first = json.loads((base / "summary.json").read_text(encoding="utf-8"))
            second = json.loads((repeat / "summary.json").read_text(encoding="utf-8"))
            for field in ("accepted", "accepted_at_call", "steps", "terminal"):
                assert first[field] == second[field]
