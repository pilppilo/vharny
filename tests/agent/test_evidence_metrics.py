"""BENCH-0001 facts stay faithful to journal and evaluator evidence."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from vharness.agent.evidence import (
    MetricAvailability,
    MetricFact,
    OPERATIONAL_MEASUREMENT_SCOPE,
    ObjectiveVector,
    _model_duration_fact,
    _external_outcomes_fact,
    metrics,
)
from tools.phase3_paired_recovery import _read_events

ROOT = Path(__file__).resolve().parents[2]
RUN = ROOT / "evidence/phase-0003-paired-recovery/runs/recovery-drill/s0/supervised"


def test_recovery_run_metrics_match_golden_durable_facts() -> None:
    """The retained stream has exact counts and explicit unsupported rows."""
    result = metrics(_read_events(RUN / "events.jsonl.gz"))
    assert result.counters.actions == 7
    assert result.counters.evaluations == 1
    assert result.facts["external_completion"].availability is MetricAvailability.KNOWN
    assert result.facts["external_completion"].value is False
    assert result.facts["lineage_successors"].value == 1
    assert (
        result.facts["attempted_directions"].availability
        is MetricAvailability.UNAVAILABLE
    )
    assert (
        result.facts["intended_actions_by_name"].availability
        is MetricAvailability.KNOWN
    )
    assert (
        result.facts["between_commit_activity"].availability is MetricAvailability.KNOWN
    )
    row = result.facts["between_commit_activity"].value[0]
    assert row["actions"] == 7
    assert row["evaluations"] == 1
    assert row["usage_source"].startswith("replay usage-ledger prefix deltas")
    assert (
        result.facts["context_selection_duration_ms"].availability
        is MetricAvailability.UNKNOWN
    )
    assert (
        result.facts["duplicate_external_effects"].availability
        is MetricAvailability.UNAVAILABLE
    )


def test_operational_measurements_distinguish_zero_repeats_and_legacy_events() -> None:
    old_events = list(_read_events(RUN / "events.jsonl.gz"))
    modern_events = list(old_events)
    creation = modern_events[0]
    modern_events[0] = replace(
        creation,
        payload={
            **creation.payload,
            "operational_measurement_scope": OPERATIONAL_MEASUREMENT_SCOPE,
        },
    )
    assembly_count = 0
    for index, event in enumerate(modern_events):
        if event.kind not in {"context_assembled", "context_overflow"}:
            continue
        assembly_count += 1
        modern_events[index] = replace(
            event,
            payload={**event.payload, "duration_ns": assembly_count * 1_500_000},
        )

    no_reads = metrics(modern_events)
    assert (
        no_reads.facts["context_selection_duration_ms"].availability
        is MetricAvailability.KNOWN
    )
    assert no_reads.facts["context_selection_duration_ms"].value == tuple(
        index * 1.5 for index in range(1, assembly_count + 1)
    )
    assert no_reads.facts["context_artifact_bytes_read"].availability is (
        MetricAvailability.KNOWN
    )
    assert no_reads.facts["context_artifact_bytes_read"].value == 0

    template = modern_events[-1]
    repeated_reads = [
        replace(
            template,
            event_id=f"measured-read-{index}",
            sequence=template.sequence + index,
            kind="artifact_read",
            payload={
                "scope": OPERATIONAL_MEASUREMENT_SCOPE,
                "purpose": "context_observation",
                "digest": "a" * 64,
                "bytes_read": 4,
            },
        )
        for index in (1, 2)
    ]
    repeated = metrics((*modern_events, *repeated_reads))
    reads = repeated.facts["session_artifact_reads"]
    assert reads.availability is MetricAvailability.KNOWN
    assert reads.value["context_observation"] == {
        "successful_reads": 2,
        "bytes_read": 8,
    }
    assert repeated.facts["context_artifact_bytes_read"].value == 8

    mixed = metrics((*old_events, repeated_reads[0]))
    assert mixed.facts["session_artifact_reads"].availability is (
        MetricAvailability.UNKNOWN
    )
    assert (
        mixed.facts["session_artifact_reads"].value["context_observation"][
            "successful_reads"
        ]
        == 1
    )


def test_known_zero_is_distinct_from_unknown_usage() -> None:
    result = metrics(_read_events(RUN / "events.jsonl.gz"))
    assert result.facts["resume_events"].availability is MetricAvailability.KNOWN
    assert result.facts["resume_events"].value == 0
    assert result.facts["actions"].availability is MetricAvailability.UNKNOWN
    assert result.facts["actions"].value == 7
    assert result.facts["measured_cost"].availability is MetricAvailability.UNKNOWN
    assert result.facts["measured_cost"].value["unknown_operations"] > 0


def test_metric_fact_recursively_freezes_caller_owned_values() -> None:
    nested: dict[str, object] = {"values": [1, {"item": 2}]}
    fact = MetricFact(nested, MetricAvailability.KNOWN, "test")
    nested["values"].append(3)  # type: ignore[union-attr]
    with pytest.raises((TypeError, AttributeError)):
        fact.value["values"][1]["item"] = 9
    assert fact.value["values"] == (1, {"item": 2})


def test_run_metrics_mapping_is_immutable() -> None:
    result = metrics(_read_events(RUN / "events.jsonl.gz"))
    with pytest.raises(TypeError):
        result.facts["new"] = MetricFact(None, MetricAvailability.UNKNOWN, "test")


def test_unresolved_main_model_intent_makes_latency_unknown() -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    intended = next(event for event in events if event.kind == "model_intended")
    unresolved = replace(
        intended,
        event_id="unresolved-intent",
        sequence=events[-1].sequence + 1,
        payload={**intended.payload, "request_id": "unresolved-request"},
    )
    fact = _model_duration_fact((*events, unresolved))
    assert fact.availability is MetricAvailability.UNKNOWN
    assert fact.value is None


def test_active_cost_reservation_is_unknown_even_when_cost_was_bounded() -> None:
    events = _read_events(RUN / "events.jsonl.gz")
    first_intended = next(
        index for index, event in enumerate(events) if event.kind == "model_intended"
    )
    result = metrics(events[: first_intended + 1])
    assert result.facts["model_duration_ms"].availability is MetricAvailability.UNKNOWN
    cost = result.facts["measured_cost"]
    assert cost.availability is MetricAvailability.UNKNOWN
    assert cost.value == {"known_partial": "0", "unknown_operations": 1}


def test_external_acceptance_is_separate_from_stale_promotion_disposition() -> None:
    vector = ObjectiveVector(
        receipt_id="receipt-stale",
        objective_version=1,
        evaluator_version="eval-v1",
        valid=True,
        completed=False,
        externally_accepted=True,
        external_comparison="unknown",
        promotion_disposition="stale",
        objectives={"stage": 1},
        hard_constraints={},
        evidence_digests=(),
    )
    outcomes = _external_outcomes_fact((vector,))
    assert outcomes.value["externally_not_accepted"] == 0
    assert outcomes.value["rejected_or_constraint_failed"] == 0
