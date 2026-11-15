"""Manifest pairing and aggregation rules for BENCH-0001 campaigns."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import pytest

from vharness.agent.evidence import (
    OPERATIONAL_MEASUREMENT_SCOPE,
    ObjectiveVector,
    metrics,
)
from vharness.agent.errors import IntegrityError
from tools.bench_metrics_report import (
    Campaign,
    LoadedRun,
    RunInput,
    _campaign_disposition,
    _compare_pair,
    _objective_summaries,
    _per_run_rows,
    _pair_runs,
    _validate_run_set,
    load_campaign,
    write_report,
)
from vharness.agent.codec import canonical_json
from tools.phase3_paired_recovery import _read_events

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evidence/phase-0003-paired-recovery/runs/recovery-drill/s0/supervised"
REVISION = "0" * 40
DIGEST = "a" * 64


def _campaign(**updates: object) -> Campaign:
    fields: dict[str, object] = {
        "campaign_id": "test",
        "provenance": "scripted demonstration",
        "source_revision": REVISION,
        "configuration_digest": DIGEST,
        "evidence_index_digest": DIGEST,
        "repository_root": ROOT,
        "baseline": "base",
        "candidate": "candidate",
        "minimum_repetitions": 5,
        "missing_policy": "exclude",
        "censored_policy": "exclude",
        "objective_tolerances": {
            "score": {
                "direction": "maximize",
                "improvement": 1.0,
                "equivalence": 0.1,
                "regression": 1.0,
                "uncertainty": 2.0,
            }
        },
        "assistance_command_types": (),
        "pair_roster": (("semantic-task", 0, "r0"),),
        "runs": (),
    }
    fields.update(updates)
    return Campaign(**fields)  # type: ignore[arg-type]


def _record(condition: str, seed: int, replicate: str = "r0") -> RunInput:
    return RunInput(
        run_id=f"{condition}-{seed}-{replicate}",
        condition=condition,
        task_id=f"source-task-{seed}",
        task_group_id="semantic-task",
        environment_id="env",
        environment_version="v1",
        evaluator_version="eval-v1",
        objective_version=1,
        seed=seed,
        replicate_id=replicate,
        source_revision=REVISION,
        configuration_digest=DIGEST,
        event_path="events.jsonl",
        event_sha256=DIGEST,
        journal_path=None,
        journal_sha256=None,
        censored=False,
    )


def _vector(
    value: float, *, accepted: bool = True, passed: bool = True
) -> ObjectiveVector:
    return ObjectiveVector(
        receipt_id=f"receipt-{value}",
        objective_version=1,
        evaluator_version="eval-v1",
        valid=True,
        completed=True,
        externally_accepted=accepted,
        external_comparison="better",
        promotion_disposition="accepted" if accepted else "rejected",
        objectives={"score": value},
        hard_constraints={"safe": passed},
        evidence_digests=(),
    )


def test_latest_vector_is_one_sample_per_run() -> None:
    base = metrics(_read_events(SOURCE / "events.jsonl.gz"))
    run_metrics = replace(base, objective_vectors=(_vector(1), _vector(9)))
    loaded = LoadedRun(_record("candidate", 0), run_metrics, {})
    rows = _objective_summaries((loaded,))
    score = next(row for row in rows if row["objective"] == "score")
    assert score["count"] == 1
    assert score["median"] == 9
    assert score["range"] == (9.0, 9.0)


def test_historical_objective_vector_is_not_a_current_version_sample() -> None:
    base = metrics(_read_events(SOURCE / "events.jsonl.gz"))
    historical = replace(base, objective_vectors=(_vector(9),))
    current_record = replace(_record("base", 0), objective_version=2)
    loaded = LoadedRun(current_record, historical, {})
    assert _objective_summaries((loaded,)) == []
    baseline = LoadedRun(current_record, historical, {})
    candidate = LoadedRun(
        replace(_record("candidate", 0), objective_version=2), historical, {}
    )
    pair = _compare_pair(baseline, candidate, _campaign())
    assert pair["classification"] == "uncertainty"


def test_per_run_report_preserves_scoped_artifact_read_metrics() -> None:
    events = list(_read_events(SOURCE / "events.jsonl.gz"))
    creation = events[0]
    events[0] = replace(
        creation,
        payload={
            **creation.payload,
            "operational_measurement_scope": OPERATIONAL_MEASUREMENT_SCOPE,
        },
    )
    template = events[-1]
    events.append(
        replace(
            template,
            event_id="report-artifact-read",
            sequence=template.sequence + 1,
            kind="artifact_read",
            payload={
                "scope": OPERATIONAL_MEASUREMENT_SCOPE,
                "purpose": "context_observation",
                "digest": "a" * 64,
                "bytes_read": 9,
            },
        )
    )
    row = _per_run_rows((LoadedRun(_record("candidate", 0), metrics(events), {}),))[0]
    legacy_row = _per_run_rows(
        (
            LoadedRun(
                _record("base", 0),
                metrics(_read_events(SOURCE / "events.jsonl.gz")),
                {},
            ),
        )
    )[0]

    facts = row["metrics"]["facts"]
    assert facts["context_artifact_bytes_read"]["availability"] == "known"
    assert facts["context_artifact_bytes_read"]["value"] == 9
    assert facts["session_artifact_reads"]["value"]["context_observation"] == {
        "successful_reads": 1,
        "bytes_read": 9,
    }
    legacy_facts = legacy_row["metrics"]["facts"]
    assert legacy_facts["context_artifact_bytes_read"]["availability"] == "unknown"
    assert legacy_facts["session_artifact_reads"]["availability"] == "unknown"


def test_missing_excluded_pair_does_not_force_inconclusive() -> None:
    disposition = _campaign_disposition(
        ({"status": "missing", "classification": "uncertainty"},),
        ({"requirement_met": True},),
        _campaign(),
    )
    assert disposition == "descriptive_only"


def test_censored_inconclusive_and_paired_uncertainty_are_inconclusive() -> None:
    campaign = _campaign(censored_policy="inconclusive")
    assert (
        _campaign_disposition(
            ({"status": "censored", "classification": "uncertainty"},),
            ({"requirement_met": True},),
            campaign,
        )
        == "inconclusive"
    )
    assert (
        _campaign_disposition(
            ({"status": "paired", "classification": "uncertainty"},),
            ({"requirement_met": True},),
            _campaign(),
        )
        == "inconclusive"
    )


def test_minimum_repetition_failure_is_inconclusive() -> None:
    assert (
        _campaign_disposition(
            (),
            ({"requirement_met": False},),
            _campaign(),
        )
        == "inconclusive"
    )


def test_duplicate_semantic_pair_is_rejected_even_if_source_task_ids_differ() -> None:
    first = _record("base", 1)
    second = replace(first, run_id="second", task_id="different-source-task")
    with pytest.raises(IntegrityError, match="duplicate paired run identity"):
        _validate_run_set((first, second), "base", "candidate")


def test_hard_constraint_failure_forces_regression() -> None:
    base = metrics(_read_events(SOURCE / "events.jsonl.gz"))
    baseline = LoadedRun(
        _record("base", 0), replace(base, objective_vectors=(_vector(5),)), {}
    )
    candidate = LoadedRun(
        _record("candidate", 0),
        replace(base, objective_vectors=(_vector(9, passed=False),)),
        {},
    )
    pair = _compare_pair(baseline, candidate, _campaign())
    assert pair["classification"] == "regression"
    assert pair["candidate_hard_constraint_failures"] == ("safe",)


def test_censored_run_retains_partial_hard_constraint_evidence() -> None:
    base = metrics(_read_events(SOURCE / "events.jsonl.gz"))
    baseline = LoadedRun(
        replace(_record("base", 0), run_id="base-0"),
        replace(base, objective_vectors=(_vector(5),)),
        {},
    )
    candidate_record = replace(
        _record("candidate", 0), run_id="candidate-0", censored=True
    )
    candidate = LoadedRun(
        candidate_record,
        replace(base, objective_vectors=(_vector(9, passed=False),)),
        {},
    )
    pair = _pair_runs((baseline, candidate), _campaign())[0]
    assert pair["status"] == "censored"
    assert pair["candidate_partial_evidence"]["hard_constraint_failures"] == ("safe",)


def test_source_revision_is_run_specific() -> None:
    first = _record("base", 0)
    second = replace(_record("candidate", 0), source_revision="1" * 40)
    _validate_run_set((first, second), "base", "candidate")


def test_tolerance_mapping_is_detached_and_immutable() -> None:
    values = {"score": {"direction": "maximize", "improvement": 1}}
    campaign = _campaign(objective_tolerances=values)
    values["score"]["improvement"] = 99
    assert campaign.objective_tolerances["score"]["improvement"] == 1
    with pytest.raises(TypeError):
        campaign.objective_tolerances["score"]["improvement"] = 2


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_e2e_campaign(
    root: Path, *, missing_candidate: bool = False, censored_candidate: bool = False
) -> Path:
    source_rows = (
        ("productive", 0, "unsupervised"),
        ("productive", 0, "supervised"),
        ("plateaued-exploring", 0, "unsupervised"),
        ("plateaued-exploring", 0, "supervised"),
    )
    run_rows: list[dict[str, object]] = []
    for task_group, seed, condition in source_rows:
        if (
            missing_candidate
            and task_group == "productive"
            and condition == "supervised"
        ):
            continue
        source_path = (
            ROOT
            / "evidence/phase-0003-paired-recovery/runs"
            / task_group
            / f"s{seed}"
            / condition
            / "events.jsonl.gz"
        )
        output_path = root / f"{task_group}-{condition}.events.jsonl.gz"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, output_path)
        run_metrics = metrics(_read_events(source_path))
        identity = run_metrics.run_manifest
        run_rows.append(
            {
                "run_id": identity.session_id,
                "condition": "base" if condition == "unsupervised" else "candidate",
                "task_id": identity.task_id,
                "task_group_id": task_group,
                "environment_id": identity.environment_id,
                "environment_version": identity.environment_version,
                "evaluator_version": identity.evaluator_version,
                "objective_version": 1,
                "seed": seed,
                "replicate_id": "r0",
                "source_revision": (
                    "1" * 40 if condition == "unsupervised" else "2" * 40
                ),
                "configuration_digest": identity.configuration_digest,
                "event_path": output_path.relative_to(root).as_posix(),
                "event_sha256": _sha(output_path.read_bytes()),
                "censored": (
                    censored_candidate
                    and task_group == "productive"
                    and condition == "supervised"
                ),
            }
        )
    runs_path = root / "runs.json"
    runs_path.write_text(canonical_json(run_rows) + "\n", encoding="utf-8")
    manifest: dict[str, object] = {
        "schema_version": 1,
        "campaign_id": "e2e-demo",
        "provenance": "scripted demonstration",
        "source_revision": REVISION,
        "repository_root": str(root),
        "conditions": {"baseline": "base", "candidate": "candidate"},
        "pairing": {
            "minimum_repetitions": 1,
            "missing_policy": "exclude",
            "censored_policy": "exclude",
        },
        "objective_tolerances": {
            "stage": {
                "direction": "maximize",
                "improvement": 0.5,
                "equivalence": 0.0,
                "regression": 0.5,
                "uncertainty": 1.0,
            }
        },
        "assistance_command_types": [],
        "pair_roster": [
            {"task_group_id": group, "seed": seed, "replicate_id": "r0"}
            for group, seed, _ in source_rows[::2]
        ]
        + [{"task_group_id": "predeclared-no-run", "seed": 7, "replicate_id": "r0"}],
        "runs_path": "runs.json",
        "runs_sha256": _sha(runs_path.read_bytes()),
    }
    manifest["configuration_digest"] = _sha(
        canonical_json(
            {
                key: manifest[key]
                for key in (
                    "campaign_id",
                    "provenance",
                    "conditions",
                    "pairing",
                    "objective_tolerances",
                    "assistance_command_types",
                    "pair_roster",
                )
            }
        ).encode("utf-8")
    )
    manifest_path = root / "manifest.json"
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return manifest_path


def test_manifest_report_recomputes_with_task_groups_and_frozen_identities(
    tmp_path: Path,
) -> None:
    manifest_path = _write_e2e_campaign(tmp_path)
    campaign = load_campaign(manifest_path)
    assert {row.source_revision for row in campaign.runs} == {"1" * 40, "2" * 40}
    report_path = tmp_path / "report.json"
    first = write_report(manifest_path, report_path)
    second = write_report(manifest_path, report_path)
    assert first == second
    assert report_path.read_text(encoding="utf-8") == canonical_json(second) + "\n"
    groups = first["pairing"]["comparable_groups"]
    assert {row["task_group_id"] for row in groups} == {
        "productive",
        "plateaued-exploring",
    }
    assert first["provenance"] == "scripted demonstration"
    assert (
        first["operator_assistance"]["classification"]
        == "no recorded assisting intervention"
    )


def test_excluded_missing_pair_is_reported_without_forcing_inconclusive(
    tmp_path: Path,
) -> None:
    manifest_path = _write_e2e_campaign(tmp_path, missing_candidate=True)
    report = write_report(manifest_path, tmp_path / "report.json")
    missing = [
        pair
        for pair in report["pairing"]["pairs"]
        if pair["status"] == "missing" and pair["task_group_id"] == "productive"
    ]
    assert len(missing) == 1
    assert missing[0]["policy"] == "exclude"
    assert report["campaign_disposition"] == "descriptive_only"


def test_roster_pair_missing_both_conditions_remains_visible(tmp_path: Path) -> None:
    manifest_path = _write_e2e_campaign(tmp_path)
    report = write_report(manifest_path, tmp_path / "report.json")
    missing = next(
        pair
        for pair in report["pairing"]["pairs"]
        if pair["task_group_id"] == "predeclared-no-run"
    )
    assert missing["status"] == "missing"
    assert missing["baseline_run_id"] is None
    assert missing["candidate_run_id"] is None


def test_censored_manifest_run_keeps_partial_event_facts(tmp_path: Path) -> None:
    manifest_path = _write_e2e_campaign(tmp_path, censored_candidate=True)
    report = write_report(manifest_path, tmp_path / "report.json")
    censored = next(
        pair
        for pair in report["pairing"]["pairs"]
        if pair["task_group_id"] == "productive"
    )
    assert censored["status"] == "censored"
    assert censored["candidate_partial_evidence"]["available"] is True
    assert censored["candidate_partial_evidence"]["valid_external_evaluations"] == 1
    assert any(
        row["condition"] == "candidate"
        and row["task_group_id"] == "productive"
        and row["censored"] is True
        and row["metrics"] is not None
        for row in report["per_run_metrics"]
    )

    objective = next(
        row
        for row in report["native_objective_summaries"]
        if row["condition"] == "candidate" and row["task_group_id"] == "productive"
    )
    assert objective["count"] == 0
    assert objective["excluded_censored_count"] == 1
    assert objective["median"] is None

    run_metrics = next(
        row
        for row in report["run_metric_summaries"]
        if row["condition"] == "candidate"
        and row["task_group_id"] == "productive"
        and row["metric"] == "model_calls"
    )
    assert run_metrics["sample_count"] == 0
    assert run_metrics["excluded_censored_count"] == 1

    efficiency = next(
        row
        for row in report["efficiency"]["candidate"]["comparable_groups"]
        if row["task_group_id"] == "productive"
    )
    assert efficiency["uncensored_run_count"] == 0
    assert efficiency["excluded_censored_count"] == 1
    assert efficiency["completion_denominator"] == 0
    assert efficiency["tokens_per_external_completion"] is None

    completion = report["completion"]["candidate"]
    assert completion["denominator"] == 1
    assert completion["censored_count"] == 1
    assert (
        completion["censored_observations_excluded_from_denominator"]["denominator"]
        == 1
    )
    assert (
        completion["censored_observations_excluded_from_denominator"][
            "evaluated_incomplete"
        ]
        == 1
    )


def test_wrong_frozen_configuration_digest_is_rejected(tmp_path: Path) -> None:
    manifest_path = _write_e2e_campaign(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["configuration_digest"] = "0" * 64
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="configuration digest"):
        load_campaign(manifest_path)


def test_wrong_event_hash_is_rejected(tmp_path: Path) -> None:
    manifest_path = _write_e2e_campaign(tmp_path)
    runs_path = tmp_path / "runs.json"
    runs = json.loads(runs_path.read_text(encoding="utf-8"))
    runs[0]["event_sha256"] = "0" * 64
    raw_runs = canonical_json(runs) + "\n"
    runs_path.write_text(raw_runs, encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runs_sha256"] = _sha(raw_runs.encode("utf-8"))
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match="SHA-256"):
        write_report(manifest_path, tmp_path / "report.json")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("run_id", "wrong-session", "session identity"),
        ("task_id", "wrong-task", "task identity"),
        ("environment_id", "wrong-environment", "environment identity"),
        ("environment_version", "wrong-version", "environment version"),
        ("evaluator_version", "wrong-evaluator", "evaluator version"),
        ("objective_version", 7, "objective version"),
    ),
)
def test_event_identity_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    manifest_path = _write_e2e_campaign(tmp_path)
    runs_path = tmp_path / "runs.json"
    runs = json.loads(runs_path.read_text(encoding="utf-8"))
    runs[0][field] = value
    raw_runs = canonical_json(runs) + "\n"
    runs_path.write_text(raw_runs, encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runs_sha256"] = _sha(raw_runs.encode("utf-8"))
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    with pytest.raises(IntegrityError, match=message):
        write_report(manifest_path, tmp_path / "report.json")
