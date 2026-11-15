"""Focused checks for the EXP-0002 signal-coverage scaffolding."""

from __future__ import annotations

import hashlib
import json
import shutil
import pytest
from dataclasses import asdict
from pathlib import Path
import sys
from typing import Mapping

from vharness.agent.artifacts import ArtifactStore
from vharness.agent.models import Event, JsonValue

ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(ROOT))
from tools import exp_0002_signal_coverage as exp2  # noqa: E402
from tools.exp_0002_signal_coverage import StepFacts  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _event(kind: str, sequence: int, payload: Mapping[str, JsonValue]) -> Event:
    return Event(
        event_id=f"event-{sequence}",
        session_id="session-test",
        sequence=sequence,
        kind=kind,
        objective_version=1,
        payload=payload,
        recorded_at="2026-10-30T00:00:00+00:00",
        causation_id=None,
        correlation_id=None,
        schema_version=1,
    )


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _objects(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _facts(
    call: int,
    *,
    carried: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    actions: int = 0,
    failed: int = 0,
    tokens: int = 0,
    progress: int = 0,
) -> StepFacts:
    return StepFacts(
        call=call,
        carried_fingerprints=carried,
        evidence_fingerprints=evidence,
        actions=actions,
        failed_receipts=failed,
        input_tokens=tokens,
        progress_signals=progress,
    )


def _observe_all(
    policy: str, facts: list[StepFacts], call_budget: int = exp2.MAIN_CALL_BUDGET
) -> list[dict[str, object]]:
    """Evaluate one policy at every boundary over the growing journal prefix."""
    monitor = exp2.CandidateMonitor(policy, call_budget)
    triggers = []
    for boundary in range(1, len(facts) + 2):
        report = monitor.observe(tuple(facts[: boundary - 1]), boundary)
        if report is not None:
            triggers.append(report.to_json())
    return triggers


def _observed(triggers: list[dict[str, object]]) -> list[tuple[int, str]]:
    observed = []
    for trigger in triggers:
        call = trigger["call"]
        reason = trigger["reason"]
        assert isinstance(call, int) and isinstance(reason, str)
        observed.append((call, reason))
    return observed


# --- Carried families (pinned through the shipped decision_facts path) -------


def test_carried_families_include_synthetic_budget_exhausted(tmp_path: Path) -> None:
    """A budget-rejected EvaluationChoice yields the synthetic fingerprint."""
    store = ArtifactStore(tmp_path / "artifacts")
    segment = (
        _event(
            "model_application_rejected",
            1,
            {"reason": "resource budget exhausted", "request_id": "req-1"},
        ),
        _event(
            "model_decided",
            2,
            {"request_id": "req-1", "result_kind": "EvaluationChoice"},
        ),
    )
    facts = exp2._step_facts(segment, 1, store, frozenset({"exec"}))
    assert "evaluation:budget exhausted" in facts.carried_fingerprints
    assert facts.evidence_fingerprints == ()


def test_carried_families_reproduce_repetition_path(tmp_path: Path) -> None:
    """Action, failed-receipt, and rejected-evaluation fingerprints carry."""
    store = ArtifactStore(tmp_path / "artifacts")
    segment = (
        _event(
            "action_intended",
            1,
            {
                "proposal": {
                    "proposal_id": "p1",
                    "action_name": "exec",
                    "arguments": {"command": "ls"},
                }
            },
        ),
        _event(
            "receipt_received",
            2,
            {
                "receipt": {
                    "proposal_id": "p1",
                    "status": "failed",
                    "error": "boom",
                }
            },
        ),
        _event("evaluation_received", 3, {"disposition": "rejected"}),
    )
    facts = exp2._step_facts(segment, 1, store, frozenset({"exec"}))
    assert facts.actions == 1
    assert facts.failed_receipts == 1
    assert any(
        fingerprint.startswith("action:exec:")
        for fingerprint in facts.carried_fingerprints
    )
    assert "receipt:failed:boom" in facts.carried_fingerprints
    assert "evaluation:rejected" in facts.carried_fingerprints


# --- R1/R2 evidence fingerprints ---------------------------------------------


def test_r1_r2_extract_stdout_from_run2_envelope(tmp_path: Path) -> None:
    """The envelope's stdout stream entries produce the R1/R2 digests."""
    store = ArtifactStore(tmp_path / "artifacts")
    stdout_bytes = b"HTTP/1.1 200 OK\n\n Timing"
    stdout_ref = store.put(
        stdout_bytes, media_type="text/plain", provenance="process-output-stdout"
    )
    stderr_ref = store.put(
        b"", media_type="text/plain", provenance="process-output-stderr"
    )
    envelope = {
        "artifact_total": 2,
        "exit_code": 0,
        "output_artifacts": [
            {"artifact": asdict(stdout_ref), "offset": 0, "stream": "stdout"},
            {"artifact": asdict(stderr_ref), "offset": 1, "stream": "stderr"},
        ],
    }
    envelope_bytes = json.dumps(envelope).encode("utf-8")
    envelope_ref = store.put(
        envelope_bytes, media_type="application/json", provenance="receipt-envelope"
    )
    receipt = {
        "status": "succeeded",
        "raw_artifact": {
            "digest": envelope_ref.digest,
            "size": envelope_ref.size,
            "media_type": envelope_ref.media_type,
            "provenance": envelope_ref.provenance,
        },
    }
    fingerprints = exp2._stdout_fingerprints(receipt, store)
    expected_normalized = _sha(b"timing")
    assert fingerprints == (
        f"stdout-strict:{_sha(stdout_bytes)}",
        f"stdout-normalized:{expected_normalized}",
    )


def test_r1_r2_direct_payload_is_stdout(tmp_path: Path) -> None:
    """Without an envelope, the artifact content itself is the stdout."""
    store = ArtifactStore(tmp_path / "artifacts")
    empty_ref = store.put(
        b"", media_type="text/plain", provenance="fixture-exec-stdout"
    )
    receipt = {
        "status": "succeeded",
        "raw_artifact": {
            "digest": empty_ref.digest,
            "size": empty_ref.size,
            "media_type": empty_ref.media_type,
            "provenance": empty_ref.provenance,
        },
    }
    fingerprints = exp2._stdout_fingerprints(receipt, store)
    empty_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert fingerprints == (
        f"stdout-strict:{empty_digest}",
        f"stdout-normalized:{empty_digest}",
    )


def test_normalize_stdout_drops_headers_and_markers() -> None:
    """The frozen normalization rule strips headers, markers, and case."""
    text = "HTTP/1.1 200 OK\nServer: x\n\n---\nResult: OK\n---\n"
    assert exp2._normalize_stdout(text) == "result: ok"
    assert exp2._normalize_stdout("") == ""
    assert exp2._normalize_stdout("---\n") == ""


# --- Candidate monitors over synthetic facts ---------------------------------


def test_p4_fires_on_run2_shape() -> None:
    """Run2's shape fires P4 exactly at the pre-registered boundaries.

    All-distinct commands, empty stdout on run2's sparse credential-loop
    calls (7, 9, 11, 16), zero supported progress, and run2's 40-call budget
    reproduce the pre-registered table: repetition at boundaries 12 and 17,
    claim-based burn with consumed calls 24/28/32/36 (boundaries 25-37).
    """
    empty_calls = {7, 9, 11, 16}
    facts = []
    for call in range(1, 39):
        empty = call in empty_calls
        facts.append(
            _facts(
                call,
                carried=(f"action:exec:command-{call}",),
                evidence=(
                    (f"stdout-strict:{'0' * 64}", f"stdout-normalized:{'0' * 64}")
                    if empty
                    else (f"stdout-strict:{_sha(str(call).encode())}",)
                ),
                actions=1,
                tokens=10,
            )
        )
    observed = _observed(_observe_all("P4", facts, call_budget=40))
    assert observed == [
        (12, "repetition"),
        (17, "repetition"),
        (25, "budget-burn-claim"),
        (29, "budget-burn-claim"),
        (33, "budget-burn-claim"),
        (37, "budget-burn-claim"),
    ]


def test_b2_stays_silent_when_fresh_evidence_keeps_arriving() -> None:
    """Churn counts as progress for B2 while B1's claim counter still burns."""
    facts = [
        _facts(
            call,
            carried=(f"action:exec:command-{call}",),
            evidence=(f"stdout-strict:{_sha(str(call).encode())}",),
            actions=1,
            tokens=10,
        )
        for call in range(1, 13)
    ]
    p4 = _observed(_observe_all("P4", facts))
    p5 = _observed(_observe_all("P5", facts))
    assert any(reason == "budget-burn-claim" for _, reason in p4)
    assert all(reason != "budget-burn-evidence" for _, reason in p5)


def test_b2_fires_when_evidence_repeats_without_repetition() -> None:
    """Sparse repeated evidence leaves repetition silent and burns B2.

    The same digest appears on calls 1, 5, 9, and 13, so no window-8 holds
    three repeats, but no call after the first shows unseen evidence: the
    streak reaches the threshold at boundary 10.
    """
    empty_calls = {1, 5, 9, 13}
    facts = []
    for call in range(1, 15):
        facts.append(
            _facts(
                call,
                carried=(f"action:exec:command-{call}",),
                evidence=(f"stdout-strict:{'0' * 64}",) if call in empty_calls else (),
                actions=1,
                tokens=10,
            )
        )
    observed = _observed(_observe_all("P5", facts))
    assert observed[0] == (10, "budget-burn-evidence")
    assert all(reason == "budget-burn-evidence" for _, reason in observed)
    fire_calls = [call for call, _ in observed]
    for earlier, later in zip(fire_calls, fire_calls[1:]):
        assert later - earlier >= exp2.BURN_COOLDOWN_CALLS


def test_progress_resets_the_burn_clock() -> None:
    """A supported claim re-arms the burn threshold from that call."""

    def build(progress_calls: set[int]) -> list[StepFacts]:
        return [
            _facts(
                call,
                carried=(f"action:exec:command-{call}",),
                evidence=(f"stdout-strict:{_sha(str(call).encode())}",),
                actions=1,
                tokens=10,
                progress=1 if call in progress_calls else 0,
            )
            for call in range(1, 13)
        ]

    one_claim = _observed(_observe_all("P4", build({3})))
    assert (12, "budget-burn-claim") in one_claim
    two_claims = _observed(_observe_all("P4", build({3, 8})))
    assert all(reason != "budget-burn-claim" for _, reason in two_claims)


def test_productive_shape_never_triggers() -> None:
    """Distinct evidence with claims filed throughout the run stays silent."""
    facts = [
        _facts(
            call,
            carried=(f"action:focus:step-{call}",),
            evidence=(f"stdout-strict:{_sha(str(call).encode())}",),
            actions=1,
            tokens=10,
            progress=1 if call % 2 == 0 else 0,
        )
        for call in range(1, 13)
    ]
    for policy in ("B0", "P4", "P5"):
        assert _observe_all(policy, facts) == []


def test_b0_counts_carried_fingerprints_only() -> None:
    """B0 is monitor.p1.v1: evidence repeats alone never fire it."""
    facts = [
        _facts(
            call,
            carried=(f"action:exec:command-{call}",),
            evidence=(f"stdout-strict:{'0' * 64}",),
            actions=1,
            tokens=10,
        )
        for call in range(1, 13)
    ]
    assert _observe_all("B0", facts) == []
    repeating = [
        _facts(call, carried=("action:exec:same",), actions=1, tokens=10)
        for call in range(1, 6)
    ]
    assert (4, "repetition") in _observed(_observe_all("B0", repeating))


def test_shared_cooldown_across_triggers() -> None:
    """One re-arm timer covers a policy's repetition and burn triggers."""
    facts = []
    for call in range(1, 15):
        facts.append(
            _facts(
                call,
                carried=("action:exec:same",),
                evidence=(f"stdout-strict:{'0' * 64}",),
                actions=1,
                tokens=10,
            )
        )
    calls = [call for call, _ in _observed(_observe_all("P4", facts))]
    assert calls[0] == 4
    for earlier, later in zip(calls, calls[1:]):
        assert later - earlier >= exp2.COOLDOWN_CALLS


# --- Pre-registration and replay comparison ----------------------------------


def test_pre_registration_matches_exp_0002() -> None:
    """The frozen run2 expectations match the approved document."""
    assert exp2.PRE_REGISTRATION["B0"] == {"repetition": [], "burn_consumed": []}
    assert exp2.PRE_REGISTRATION["P4"]["repetition"] == [12, 17]
    assert exp2.PRE_REGISTRATION["P4"]["burn_consumed"] == [24, 28, 32, 36]
    assert exp2.PRE_REGISTRATION["P5"]["repetition"] == [12, 17]
    assert exp2.PRE_REGISTRATION["P5"]["burn_consumed"] == []
    assert exp2.RUN2_JOURNAL_SHA256 == (
        "ca9adf8f2efdca1e5dec48de9e9d87a09081df681c82a609c83112512f2719ec"
    )


def test_replay_summary_compares_pre_registration() -> None:
    """Missing and extra fires are computed against the frozen boundaries."""
    rows = {
        "B0": [],
        "P4": [
            {"call": 12, "reason": "repetition", "source": "local"},
            {"call": 25, "reason": "budget-burn-claim", "source": "local"},
        ],
        "P5": [
            {"call": 12, "reason": "repetition", "source": "local"},
            {"call": 30, "reason": "budget-burn-evidence", "source": "local"},
        ],
    }
    summary = exp2._replay_summary(rows, [], 40, 39)
    policies = _mapping(summary["policies"])
    p4 = _mapping(policies["P4"])
    assert p4["missing_pre_registered_fires"] == [17, 29, 33, 37]
    assert p4["extra_fires"] == []
    p5 = _mapping(policies["P5"])
    assert p5["missing_pre_registered_fires"] == [17]
    assert p5["extra_fires"] == [30]
    assert p5["b2_pre_registration_falsified"] is True
    b0 = _mapping(policies["B0"])
    assert b0["missing_pre_registered_fires"] == []
    assert b0["extra_fires"] == []


# --- Fixture run integration --------------------------------------------------


def test_fixture_run_triggers_and_retains_artifacts(tmp_path: Path) -> None:
    """A stalled P4 run fires, accepts, and retains recomputable evidence."""
    from tools.exp_0001_supervisor_policy import FIXTURES

    run_dir = tmp_path / "stalled" / "s0" / "P4"
    summary = exp2._run_once(FIXTURES["stalled"], 0, "P4", run_dir)
    assert summary["accepted"] is True
    supervisor_calls = summary["supervisor_calls"]
    assert isinstance(supervisor_calls, int)
    assert supervisor_calls <= exp2.SUPERVISOR_CALL_ALLOCATION
    triggers = _objects(summary["triggers"])
    reasons = [str(_mapping(trigger)["reason"]) for trigger in triggers]
    assert "repetition" in reasons
    empty_digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    first_facts = _mapping(_objects(summary["facts"])[0])
    assert f"stdout-strict:{empty_digest}" in _objects(
        first_facts["evidence_fingerprints"]
    )
    store = run_dir / "artifacts"
    assert store.exists() and any(store.rglob("*"))
    assert exp2.verify_artifact_store_copy(store) == []


def test_manifest_records_b2_semantics_and_fixture_outputs() -> None:
    """The manifest freezes the burn semantics, outputs, and normalization."""
    manifest = exp2.build_manifest()
    signals = _mapping(manifest["signal_definitions"])
    burn = _mapping(signals["burn_semantics"])
    assert "since the last progress-bearing call" in str(burn["B2_progress"])
    assert "streak" in str(burn["B2_progress"])
    assert "supported" in str(burn["B1_progress"])
    replay = _mapping(manifest["run2_replay"])
    assert replay["journal_sha256"] == exp2.RUN2_JOURNAL_SHA256
    fixtures = _mapping(manifest["fixtures"])
    stalled = _mapping(fixtures["stalled"])
    outputs = {
        str(_mapping(_mapping(entry)["decision"])["name"]): str(
            _mapping(entry)["stdout"]
        )
        for entry in _objects(stalled["outputs"])
    }
    assert outputs["probe"] == ""
    exploring = _mapping(fixtures["plateaued-exploring"])
    exploring_outputs = [
        str(_mapping(entry)["stdout"]) for entry in _objects(exploring["outputs"])
    ]
    assert len(set(exploring_outputs)) == len(exploring_outputs)


# --- Bundle path handling -----------------------------------------------------


def _write_minimal_bundle(bundle: Path) -> None:
    """Write a minimal bundle (manifest and aggregate, no retained runs)."""
    bundle.mkdir(parents=True, exist_ok=True)
    manifest = exp2.build_manifest()
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    exp2.write_aggregate(bundle)


def _fake_checkout(tmp_path: Path) -> Path:
    """Create a repository stand-in whose tool copy has identical bytes."""
    checkout = tmp_path / "checkout"
    (checkout / "tools").mkdir(parents=True)
    shutil.copyfile(exp2.SOURCE_ROOT / exp2.TOOL_RELPATH, checkout / exp2.TOOL_RELPATH)
    return checkout


def test_verify_and_aggregate_accept_bundle_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle outside the repository tree aggregates and verifies in full."""
    from tools.exp_0001_supervisor_policy import FIXTURES

    bundle = tmp_path / "bundle"
    run_dir = bundle / "runs" / "stalled" / "s0" / "P4"
    exp2._run_once(FIXTURES["stalled"], 0, "P4", run_dir)
    monkeypatch.setattr(exp2, "SOURCE_ROOT", _fake_checkout(tmp_path))
    monkeypatch.setattr(exp2, "_git_revision", lambda: ("0" * 40, False))
    _write_minimal_bundle(bundle)
    resolved = bundle.resolve()
    assert not resolved.is_relative_to(exp2.SOURCE_ROOT)
    results = exp2.write_aggregate(bundle)
    assert results["bundle"] == resolved.as_posix()
    assert exp2.verify_bundle(bundle) == []


def test_verify_and_aggregate_accept_relative_bundle_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative bundle path resolves from the caller's working directory."""
    bundle = tmp_path / "bundle"
    _write_minimal_bundle(bundle)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(exp2, "SOURCE_ROOT", _fake_checkout(tmp_path))
    results = exp2.write_aggregate(Path("bundle"))
    assert results["bundle"] == bundle.resolve().as_posix()
    assert exp2.verify_bundle(Path("bundle")) == []


def test_outside_repo_bundle_remains_digest_gated(tmp_path: Path) -> None:
    """The tool digest gate still governs a bundle outside the repository."""
    bundle = tmp_path / "bundle"
    _write_minimal_bundle(bundle)
    assert exp2.verify_bundle(bundle) == []
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    _mapping(manifest["source"])["tool_sha256"] = "0" * 64
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    assert exp2.verify_bundle(bundle) == ["tool sha256 does not match the manifest"]
