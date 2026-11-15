"""Tests for the EXP-0003 burn-progress experiment apparatus."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from vharness.agent import ContractError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import exp_0003_burn_progress as exp3  # noqa: E402
from tools.exp_0001_supervisor_policy import FIXTURES  # noqa: E402


def _fact(
    call: int,
    *,
    consumed: int = 1,
    digests: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    carried: tuple[str, ...] = (),
    pending: int = 0,
) -> exp3.ExpFacts:
    return exp3.ExpFacts(
        call=call,
        consumed_calls=consumed,
        carried_fingerprints=carried,
        evidence_fingerprints=evidence,
        progress_digests=digests,
        pending_claims=pending,
        actions=0,
        failed_receipts=0,
        input_tokens=0,
        progress_signals=len(digests),
    )


def test_checkpoint_is_due_after_24_consumed_calls_at_a_40_budget():
    monitor = exp3.CheckpointMonitor(40)
    assert monitor.threshold() == 24
    # Boundary c is evaluated with decisions 1..c-1 settled: at boundary 24
    # only 23 calls are consumed, and the checkpoint is due at boundary 25.
    before = [_fact(call) for call in range(1, 24)]
    assert monitor.due(tuple(before)) is None
    facts = [_fact(call) for call in range(1, 25)]
    assert monitor.due(tuple(facts)) == (24, 0)
    report = monitor.observe(tuple(facts), 25)
    assert isinstance(report, exp3.CheckpointReport)
    assert report.consumed_calls == 24
    assert report.threshold == 24
    assert report.progress_anchor == 0
    assert report.reason == exp3.CHECKPOINT_CAUSE


def test_novel_stdout_every_call_does_not_delay_the_checkpoint():
    monitor = exp3.CheckpointMonitor(40)
    facts = [
        _fact(call, evidence=(f"stdout-strict:{call:064d}",)) for call in range(1, 25)
    ]
    report = monitor.observe(tuple(facts), 25)
    assert isinstance(report, exp3.CheckpointReport)
    assert report.consumed_calls == 24
    assert report.progress_anchor == 0


def test_novel_supported_progress_resets_the_anchor_exactly_once():
    monitor = exp3.CheckpointMonitor(40)
    facts = [
        _fact(1, digests=("progress:a",)),
        _fact(2),
        _fact(3),
        _fact(4),
        _fact(5, digests=("progress:b",)),
        *[_fact(call) for call in range(6, 30)],
    ]
    # Anchor sits at consumed 5 after the second novel claim: the checkpoint
    # is due at consumed 5 + 24 = 29, boundary 30.
    assert monitor.due(tuple(facts)) == (29, 5)


def test_duplicate_support_and_pending_claims_never_reset_the_anchor():
    monitor = exp3.CheckpointMonitor(40)
    facts = [
        _fact(1, digests=("progress:a",)),
        *[_fact(call, digests=("progress:a",)) for call in range(2, 20)],
        *[_fact(call, digests=("progress:a",), pending=1) for call in range(20, 26)],
    ]
    # The anchor stays at consumed 1: duplicates and pending noise never move it.
    assert monitor.due(tuple(facts)) == (25, 1)


def test_retry_overlay_consumes_budget_in_the_checkpoint_counting():
    monitor = exp3.CheckpointMonitor(12)
    facts = [_fact(call, consumed=3) for call in range(1, 3)]
    assert monitor.due(tuple(facts)) is None
    facts.append(_fact(3, consumed=3))
    assert monitor.due(tuple(facts)) == (9, 0)


def test_checkpoint_cooldown_and_repetition_precedence():
    monitor = exp3.CheckpointMonitor(40)
    facts = [_fact(call) for call in range(1, 26)]
    first = monitor.observe(tuple(facts), 25)
    assert isinstance(first, exp3.CheckpointReport)
    for call in (26, 27, 28):
        assert monitor.observe(tuple(facts), call) is None
    second = monitor.observe(tuple(facts), 29)
    assert isinstance(second, exp3.CheckpointReport)
    assert second.consumed_calls == 25

    repeated = exp3.CheckpointMonitor(40)
    repeat_facts = [_fact(call, carried=("action:probe",)) for call in range(1, 26)]
    report = repeated.observe(tuple(repeat_facts), 25)
    assert report.reason == "repetition"
    assert report.repeat_count == 8


def test_paired_pre_registrations_are_frozen_constants():
    assert exp3.PAIRED_PRE_REGISTRATION["novel-stdout-no-progress"]["P6"] == {
        "repetition": [],
        "checkpoint_consumed": [8],
    }
    assert exp3.PAIRED_PRE_REGISTRATION["retry-heavy-burn"]["P6"] == {
        "repetition": [],
        "checkpoint_consumed": [9],
    }
    assert exp3.PAIRED_PRE_REGISTRATION["novel-stdout-with-progress"]["P6"] == {
        "repetition": [],
        "checkpoint_consumed": [],
    }
    assert exp3.RUN2_PRE_REGISTRATION["P6"] == {
        "repetition": [12, 17],
        "checkpoint_consumed": [24, 28, 32, 36],
    }
    assert exp3.RUN2_JOURNAL_SHA256 == (
        "ca9adf8f2efdca1e5dec48de9e9d87a09081df681c82a609c83112512f2719ec"
    )
    assert exp3.THRESHOLD_CHECK["expected_first_checkpoint_consumed"] == 24
    assert exp3.THRESHOLD_CHECK["expected_first_checkpoint_boundary"] == 25


def test_paired_fixtures_are_registered_and_distinct_from_carried():
    for name in exp3.PAIRED_CLASSES:
        assert name not in FIXTURES
        assert name in exp3.PAIRED_FIXTURES
    assert exp3.ALL_CLASSES == (*exp3.FIXTURE_CLASSES, *exp3.PAIRED_CLASSES)
    retry_spec = exp3.PAIRED_FIXTURES["retry-heavy-burn"]
    assert isinstance(retry_spec, exp3.RetryHeavySpec)
    assert retry_spec.retries_per_call == 2
    assert exp3._retries_for(retry_spec) == 2
    assert exp3._retries_for(FIXTURES["stalled"]) == 0
    novel_plateau = exp3.PAIRED_FIXTURES["novel-stdout-no-progress"].plateau
    targets = [decision.resolved_arguments()["target"] for decision in novel_plateau]
    assert len(set(targets)) == len(targets) == 12


def test_exp_facts_extracts_supported_identities_and_pending_claims(
    tmp_path: Path,
) -> None:
    from vharness.agent.artifacts import ArtifactStore
    from vharness.agent.models import Event

    store = ArtifactStore(tmp_path / "artifacts")
    events = (
        Event(
            event_id="e1",
            session_id="s",
            sequence=1,
            kind="progress_claim_recorded",
            objective_version=1,
            payload={
                "claim": {
                    "claim_id": "claim-1",
                    "category": "knowledge",
                    "before": "before",
                    "after": "after",
                    "scope": "scope",
                    "status": "supported",
                }
            },
            recorded_at="2026-11-06T00:00:00Z",
            causation_id=None,
        ),
        Event(
            event_id="e2",
            session_id="s",
            sequence=2,
            kind="progress_claim_recorded",
            objective_version=1,
            payload={
                "claim": {
                    "claim_id": "claim-2",
                    "category": "knowledge",
                    "before": "before",
                    "after": "guess",
                    "scope": "scope",
                    "status": "pending",
                }
            },
            recorded_at="2026-11-06T00:00:01Z",
            causation_id=None,
        ),
    )
    facts = exp3._exp_facts(events, 1, store)
    assert len(facts.progress_digests) == 1
    assert facts.pending_claims == 1
    duplicate = exp3._exp_facts(events, 2, store)
    assert duplicate.progress_digests == facts.progress_digests


def test_run_once_novel_stdout_p6_checkpoints_while_p5_stays_silent(
    tmp_path: Path,
) -> None:
    spec = exp3.PAIRED_FIXTURES["novel-stdout-no-progress"]
    p6 = exp3._run_once(spec, 0, "P6", tmp_path / "p6")
    checkpoints = [
        row for row in p6["triggers"] if row["reason"] == exp3.CHECKPOINT_CAUSE
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0]["call"] == 9
    assert checkpoints[0]["consumed_calls"] == 8
    assert checkpoints[0]["threshold"] == 8
    assert checkpoints[0]["progress_anchor"] == 0
    p5 = exp3._run_once(spec, 0, "P5", tmp_path / "p5")
    assert p5["triggers"] == []
    assert p5["terminal"] == "model_budget_exhausted"


def test_run_once_retry_heavy_p6_checkpoints_at_consumed_nine(tmp_path: Path) -> None:
    spec = exp3.PAIRED_FIXTURES["retry-heavy-burn"]
    p6 = exp3._run_once(spec, 0, "P6", tmp_path / "p6")
    checkpoints = [
        row for row in p6["triggers"] if row["reason"] == exp3.CHECKPOINT_CAUSE
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0]["call"] == 4
    assert checkpoints[0]["consumed_calls"] == 9
    assert p6["steps"] == 4
    assert p6["consumed_calls"] == 12
    for policy in ("P4", "P5"):
        run = exp3._run_once(spec, 0, policy, tmp_path / policy.lower())
        assert [
            row for row in run["triggers"] if row["reason"].startswith("budget-")
        ] == []


def test_run_once_duplicate_support_resets_p4_but_not_p6(tmp_path: Path) -> None:
    spec = exp3.PAIRED_FIXTURES["duplicate-progress"]
    p6 = exp3._run_once(spec, 0, "P6", tmp_path / "p6")
    checkpoints = [
        row for row in p6["triggers"] if row["reason"] == exp3.CHECKPOINT_CAUSE
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0]["call"] == 10
    assert checkpoints[0]["consumed_calls"] == 9
    assert checkpoints[0]["progress_anchor"] == 1
    p4 = exp3._run_once(spec, 0, "P4", tmp_path / "p4")
    assert [row for row in p4["triggers"] if row["reason"].startswith("budget-")] == []


def test_run_once_escalates_when_supervisor_capacity_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exp3, "SUPERVISOR_CALL_ALLOCATION", 0)
    spec = exp3.PAIRED_FIXTURES["novel-stdout-no-progress"]
    run = exp3._run_once(spec, 0, "P6", tmp_path / "run")
    assert len(run["triggers"]) == 1
    assert run["supervisor_calls"] == 0
    assert len(run["operator_escalations"]) == 1
    assert run["operator_escalations"][0]["call"] == 9


def test_run_once_truthful_progress_keeps_every_policy_silent(
    tmp_path: Path,
) -> None:
    spec = exp3.PAIRED_FIXTURES["novel-stdout-with-progress"]
    for policy in exp3.POLICIES:
        run = exp3._run_once(spec, 0, policy, tmp_path / policy.lower())
        assert run["triggers"] == []
        assert run["checkpoint_suppressions"] == []


def test_manifest_freezes_tool_identity_and_pre_registration():
    manifest = exp3.build_manifest()
    assert manifest["experiment"] == "EXP-0003"
    assert manifest["source"]["tool"] == "tools/exp_0003_burn_progress.py"
    assert manifest["source"]["tool_sha256"]
    assert manifest["executions"]["total"] == 220
    assert manifest["run4_replay"]["status"] == "input-missing"
    assert manifest["pre_registration"]["threshold_check"] == exp3.THRESHOLD_CHECK
    assert (
        manifest["pre_registration"]["paired_classes"] == exp3.PAIRED_PRE_REGISTRATION
    )


def test_verify_bundle_reports_a_tampered_tool_digest(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = exp3.build_manifest()
    manifest["source"]["tool_sha256"] = "0" * 64
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    problems = exp3.verify_bundle(bundle)
    assert "tool sha256 does not match the manifest" in problems


def test_replay_run4_records_missing_input_without_touching_the_bundle(
    tmp_path: Path,
) -> None:
    record = exp3.replay_run4(None, tmp_path)
    assert record["status"] == "missing-input"
    assert record["executed"] is False
    assert (tmp_path / "replay" / "run4-replay.json").is_file()
    supplied = tmp_path / "copy"
    supplied.mkdir()
    (supplied / "session.sqlite3").write_bytes(b"not a journal")
    unpinned = exp3.replay_run4(supplied, tmp_path)
    assert unpinned["status"] == "supplied-but-unpinned"
    assert "observed_sha256" in unpinned


def test_aggregate_refuses_a_bundle_without_runs(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    results = exp3.aggregate_bundle(bundle)
    assert results["runs"] == 0
    assert results["p6_determinism_reproducible"] is False
    assert results["admissible"] is False
    assert any(
        "run2 replay summary missing" in item
        for item in results["pre_registration_deviations"]
    )


def test_main_verify_reports_problems(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    manifest = exp3.build_manifest()
    manifest["source"]["tool_sha256"] = "0" * 64
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert exp3.main(["verify", "--bundle", str(bundle)]) == 1


def test_carried_candidate_monitor_rejects_the_p6_policy() -> None:
    from tools.exp_0002_signal_coverage import CandidateMonitor as Exp2Monitor

    with pytest.raises(ContractError):
        Exp2Monitor("P6", 12)
