"""Derive and aggregate BENCH-0001 metrics from frozen run evidence."""

# This single tool owns manifest validation, evidence loading, pairing, and
# report composition because the task grants this one entry-point file only.
# pylint: disable=too-many-lines

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import math
from pathlib import Path
import re
import statistics
from types import MappingProxyType
from typing import Mapping, Sequence, TypeGuard

from vharness.agent.codec import canonical_json, parse_json, parse_json_object
from vharness.agent.codec import to_json_value
from vharness.agent.evidence import (
    MetricAvailability,
    ObjectiveVector,
    RunMetrics,
    events_from_jsonl,
    metrics,
)
from vharness.agent.errors import ContractError, IntegrityError
from vharness.agent.models import JsonValue

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_POLICIES = {"inconclusive", "exclude"}


@dataclass(frozen=True, slots=True)
# This row is the frozen operator-facing per-run evidence identity.
# pylint: disable=too-many-instance-attributes
class RunInput:
    """One run identity frozen into the campaign run table."""

    run_id: str
    condition: str
    task_id: str
    task_group_id: str
    environment_id: str
    environment_version: str
    evaluator_version: str
    objective_version: int
    seed: int
    replicate_id: str
    source_revision: str
    configuration_digest: str
    event_path: str | None
    event_sha256: str | None
    journal_path: str | None
    journal_sha256: str | None
    censored: bool


@dataclass(frozen=True, slots=True)
# These are the immutable comparison rules and evidence-index identities.
# pylint: disable=too-many-instance-attributes
class Campaign:
    """Validated immutable campaign settings and run records."""

    campaign_id: str
    provenance: str
    source_revision: str
    configuration_digest: str
    evidence_index_digest: str
    repository_root: Path
    baseline: str
    candidate: str
    minimum_repetitions: int
    missing_policy: str
    censored_policy: str
    objective_tolerances: Mapping[str, Mapping[str, JsonValue]]
    assistance_command_types: tuple[str, ...]
    pair_roster: tuple[tuple[str, int, str], ...]
    runs: tuple[RunInput, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "objective_tolerances",
            MappingProxyType(
                {
                    key: MappingProxyType(dict(value))
                    for key, value in self.objective_tolerances.items()
                }
            ),
        )


@dataclass(frozen=True, slots=True)
class LoadedRun:
    """One frozen run row plus its replay-derived metrics."""

    record: RunInput
    metrics: RunMetrics | None
    storage_facts: Mapping[str, JsonValue]


# Manifest validation keeps each frozen and post-run identity explicit.
# pylint: disable=too-many-locals
def load_campaign(manifest_path: Path) -> Campaign:
    """Load and validate a manifest, its run table, and their content digests."""
    try:
        manifest = parse_json_object(manifest_path.read_bytes())
    except (OSError, ContractError) as exc:
        raise IntegrityError(f"cannot read campaign manifest: {exc}") from exc
    _require_int(manifest, "schema_version", 1)
    campaign_id = _require_string(manifest, "campaign_id")
    provenance = _require_string(manifest, "provenance")
    source_revision = _require_string(manifest, "source_revision")
    if not _GIT_OID.fullmatch(source_revision):
        raise IntegrityError("source_revision must be a full Git object ID")
    configuration_digest = _require_sha256(manifest, "configuration_digest")
    digest_input = _frozen_rules(manifest)
    actual_configuration_digest = _sha256(canonical_json(digest_input).encode("utf-8"))
    if actual_configuration_digest != configuration_digest:
        raise IntegrityError("campaign configuration digest does not match manifest")
    repository_root = Path(_require_string(manifest, "repository_root")).resolve()
    if not repository_root.is_dir():
        raise IntegrityError("repository_root must be an existing directory")
    conditions = manifest.get("conditions")
    if not isinstance(conditions, Mapping):
        raise IntegrityError("conditions must be an object")
    baseline = _mapping_string(conditions, "baseline")
    candidate = _mapping_string(conditions, "candidate")
    if not baseline or not candidate or baseline == candidate:
        raise IntegrityError("baseline and candidate condition IDs must differ")
    pairing = manifest.get("pairing")
    if not isinstance(pairing, Mapping):
        raise IntegrityError("pairing must be an object")
    minimum_repetitions = _positive_int(pairing, "minimum_repetitions")
    missing_policy = _policy(pairing, "missing_policy")
    censored_policy = _policy(pairing, "censored_policy")
    tolerance_values = manifest.get("objective_tolerances")
    if not isinstance(tolerance_values, Mapping) or not tolerance_values:
        raise IntegrityError("objective_tolerances must be a non-empty object")
    objective_tolerances = _validate_tolerances(tolerance_values)
    assistance_types = _string_sequence(manifest.get("assistance_command_types"))
    pair_roster = _pair_roster(manifest.get("pair_roster"))
    runs_path = _repository_file(
        repository_root, _require_string(manifest, "runs_path")
    )
    expected_runs_digest = _require_sha256(manifest, "runs_sha256")
    raw_runs = _read_verified(runs_path, expected_runs_digest, "run table")
    parsed_runs = parse_json(raw_runs)
    if not isinstance(parsed_runs, (tuple, list)):
        raise IntegrityError("run table must be a JSON array")
    runs = tuple(_run_input(value) for value in parsed_runs)
    if not pair_roster:
        raise IntegrityError("pair_roster must contain at least one predeclared pair")
    _validate_run_set(runs, baseline, candidate, pair_roster)
    return Campaign(
        campaign_id,
        provenance,
        source_revision,
        configuration_digest,
        expected_runs_digest,
        repository_root,
        baseline,
        candidate,
        minimum_repetitions,
        missing_policy,
        censored_policy,
        objective_tolerances,
        tuple(assistance_types),
        pair_roster,
        runs,
    )


def aggregate_campaign(campaign: Campaign) -> dict[str, JsonValue]:
    """Replay evidence, pair runs, and summarize native metrics without pooling."""
    loaded = tuple(_load_run(campaign, record) for record in campaign.runs)
    pairs = _pair_runs(loaded, campaign)
    summaries = _objective_summaries(loaded)
    completion = _completion_summary(loaded, campaign)
    assistance = _assistance_summary(loaded, campaign.assistance_command_types)
    per_run_metrics = _per_run_rows(loaded)
    repetition_groups = _repetition_groups(pairs, campaign.minimum_repetitions)
    full_pairs = tuple(pair for pair in pairs if pair["status"] == "paired")
    classification_counts = {
        name: sum(pair.get("classification") == name for pair in full_pairs)
        for name in ("improvement", "equivalence", "regression", "uncertainty")
    }
    return _json_object(
        {
            "schema_version": 1,
            "campaign_id": campaign.campaign_id,
            "provenance": campaign.provenance,
            "source_revision": campaign.source_revision,
            "configuration_digest": campaign.configuration_digest,
            "evidence_index_digest": campaign.evidence_index_digest,
            "conditions": {
                "baseline": campaign.baseline,
                "candidate": campaign.candidate,
            },
            "pairing": {
                "key": ["task_group_id", "seed", "replicate_id"],
                "minimum_repetitions": campaign.minimum_repetitions,
                "observed_paired_repetitions": len(full_pairs),
                "repetition_requirement_met": bool(repetition_groups)
                and all(
                    group["requirement_met"] is True for group in repetition_groups
                ),
                "comparable_groups": repetition_groups,
                "missing_policy": campaign.missing_policy,
                "censored_policy": campaign.censored_policy,
                "classification_counts": classification_counts,
                "pairs": pairs,
            },
            "completion": completion,
            "native_objective_summaries": summaries,
            "run_metric_summaries": _run_metric_summaries(loaded),
            "operator_assistance": assistance,
            "per_run_metrics": per_run_metrics,
            "efficiency": _efficiency_summary(loaded, campaign),
            "campaign_disposition": _campaign_disposition(
                pairs, repetition_groups, campaign
            ),
            "limitations": [
                "Scripted fixture provenance is not evidence of autonomous reasoning.",
                (
                    "Only objective dimensions with matching task, environment, "
                    "evaluator, and objective-version identities are compared."
                ),
                (
                    "Objective vectors are never scalarized or pooled across version "
                    "boundaries."
                ),
                (
                    "Replay validation does not establish duplicate-effect, recovery, "
                    "lost-action, replay-agreement, or trigger-precision reliability."
                ),
                (
                    "Context selection time is recorded for streams declaring "
                    "session_context_and_evaluation_successful_reads_v1; artifact "
                    "bytes cover successful context-observation and evaluator-evidence "
                    "reads only, excluding creation validation, replay, and external "
                    "runtime reads. These are persisted successful read observations, "
                    "not physical disk/cache I/O; interruption between a store read "
                    "and its journal append can lose an observation. Legacy or "
                    "incomplete streams remain unknown."
                ),
            ],
        }
    )


def write_report(manifest_path: Path, output_path: Path) -> dict[str, JsonValue]:
    """Run the report composition and write canonical JSON output."""
    report = aggregate_campaign(load_campaign(manifest_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    return report


# This boundary verifies all present partial/full event and journal evidence.
# pylint: disable=too-many-branches
def _load_run(campaign: Campaign, record: RunInput) -> LoadedRun:
    if record.censored and record.event_path is None:
        storage: dict[str, JsonValue] = {
            "status": "censored",
            "event_file_physical_bytes": None,
            "event_size_availability": "unavailable",
            "event_size_reason": "no partial event export was retained",
        }
        if record.journal_path is not None and record.journal_sha256 is not None:
            journal_path = _repository_file(
                campaign.repository_root, record.journal_path
            )
            journal = _read_verified(
                journal_path, record.journal_sha256, f"journal for {record.run_id}"
            )
            storage.update(
                {
                    "journal_physical_bytes": len(journal),
                    "journal_size_availability": "known",
                    "journal_path": record.journal_path,
                    "journal_sha256": record.journal_sha256,
                }
            )
        else:
            storage.update(
                {
                    "journal_physical_bytes": None,
                    "journal_size_availability": "unavailable",
                    "journal_size_reason": "no partial journal was retained",
                }
            )
        return LoadedRun(record, None, storage)
    if record.event_path is None or record.event_sha256 is None:
        raise IntegrityError(f"run {record.run_id} lacks event evidence identity")
    path = _repository_file(campaign.repository_root, record.event_path)
    stored = _read_verified(path, record.event_sha256, f"events for {record.run_id}")
    physical_size = len(stored)
    raw = stored
    if path.suffix == ".gz":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise IntegrityError(
                f"run {record.run_id} has invalid gzip evidence"
            ) from exc
    run_metrics = metrics(events_from_jsonl(raw))
    identity = run_metrics.run_manifest
    if identity.session_id != record.run_id:
        raise IntegrityError(f"run {record.run_id} event session identity differs")
    if identity.task_id != record.task_id:
        raise IntegrityError(f"run {record.run_id} task identity differs")
    if identity.configuration_digest != record.configuration_digest:
        raise IntegrityError(f"run {record.run_id} configuration digest differs")
    if identity.environment_id != record.environment_id:
        raise IntegrityError(f"run {record.run_id} environment identity differs")
    if identity.environment_version != record.environment_version:
        raise IntegrityError(f"run {record.run_id} environment version differs")
    if identity.evaluator_version != record.evaluator_version:
        raise IntegrityError(f"run {record.run_id} evaluator version differs")
    terminal = run_metrics.facts["terminal_references"].value
    if (
        not isinstance(terminal, Mapping)
        or terminal.get("objective_version") != record.objective_version
    ):
        raise IntegrityError(f"run {record.run_id} objective version differs")
    storage_facts: dict[str, JsonValue] = {
        "status": "measured",
        "event_file_physical_bytes": physical_size,
        "event_export_uncompressed_bytes": len(raw),
        "event_jsonl_encoded_bytes": len(raw),
        "event_path": record.event_path,
        "event_sha256": record.event_sha256,
        "journal_physical_bytes": None,
        "journal_size_availability": "deferred",
        "journal_size_reason": "no SQLite journal path is recorded for this run",
    }
    if record.journal_path is not None and record.journal_sha256 is not None:
        journal_path = _repository_file(campaign.repository_root, record.journal_path)
        journal = _read_verified(
            journal_path, record.journal_sha256, f"journal for {record.run_id}"
        )
        storage_facts.update(
            {
                "journal_physical_bytes": len(journal),
                "journal_size_availability": "known",
                "journal_path": record.journal_path,
                "journal_sha256": record.journal_sha256,
            }
        )
    return LoadedRun(record, run_metrics, storage_facts)


def _pair_runs(
    loaded: Sequence[LoadedRun], campaign: Campaign
) -> list[dict[str, JsonValue]]:
    indexed: dict[tuple[str, int, str], dict[str, LoadedRun]] = {}
    for item in loaded:
        key = (
            item.record.task_group_id,
            item.record.seed,
            item.record.replicate_id,
        )
        indexed.setdefault(key, {})[item.record.condition] = item
    pairs: list[dict[str, JsonValue]] = []
    for key in campaign.pair_roster:
        conditions = indexed.get(key, {})
        baseline = conditions.get(campaign.baseline)
        candidate = conditions.get(campaign.candidate)
        row: dict[str, JsonValue] = {
            "task_id": key[0],
            "task_group_id": key[0],
            "seed": key[1],
            "replicate_id": key[2],
        }
        if baseline is None or candidate is None:
            policy = campaign.missing_policy
            row.update(
                {
                    "status": "missing",
                    "policy": policy,
                    "classification": "uncertainty",
                    "baseline_run_id": (
                        None if baseline is None else baseline.record.run_id
                    ),
                    "candidate_run_id": (
                        None if candidate is None else candidate.record.run_id
                    ),
                }
            )
        elif (
            baseline.record.censored
            or candidate.record.censored
            or baseline.metrics is None
            or candidate.metrics is None
        ):
            row.update(
                {
                    "status": "censored",
                    "policy": campaign.censored_policy,
                    "classification": "uncertainty",
                    "baseline_partial_evidence": _partial_failure_flags(baseline),
                    "candidate_partial_evidence": _partial_failure_flags(candidate),
                }
            )
        else:
            row.update(_compare_pair(baseline, candidate, campaign))
        pairs.append(row)
    return pairs


# Pair comparison carries independent native dimensions and constraint outcomes.
# pylint: disable=too-many-locals
def _compare_pair(
    baseline: LoadedRun, candidate: LoadedRun, campaign: Campaign
) -> dict[str, JsonValue]:
    assert baseline.metrics is not None and candidate.metrics is not None
    base_vectors = _latest_valid_vectors(
        baseline.metrics.objective_vectors,
        baseline.record.objective_version,
        baseline.record.evaluator_version,
    )
    candidate_vectors = _latest_valid_vectors(
        candidate.metrics.objective_vectors,
        candidate.record.objective_version,
        candidate.record.evaluator_version,
    )
    if not _comparable_identity(baseline, candidate, base_vectors, candidate_vectors):
        return {
            "status": "paired",
            "classification": "uncertainty",
            "reason": "native score identities are missing or incomparable",
        }
    assert base_vectors and candidate_vectors
    base_vector = base_vectors[-1]
    candidate_vector = candidate_vectors[-1]
    deltas: dict[str, JsonValue] = {}
    outcomes: list[str] = []
    for name, tolerance in campaign.objective_tolerances.items():
        before = base_vector.objectives.get(name)
        after = candidate_vector.objectives.get(name)
        if not _number(before) or not _number(after):
            outcomes.append("uncertainty")
            deltas[name] = None
            continue
        direction = tolerance["direction"]
        delta = (after - before) * (1 if direction == "maximize" else -1)
        deltas[name] = delta
        outcomes.append(_classify_delta(delta, tolerance))
    if (
        not outcomes
        or "uncertainty" in outcomes
        or ("improvement" in outcomes and "regression" in outcomes)
    ):
        classification = "uncertainty"
    elif "regression" in outcomes:
        classification = "regression"
    elif "improvement" in outcomes:
        classification = "improvement"
    else:
        classification = "equivalence"
    base_failures = {
        name for name, result in base_vector.hard_constraints.items() if not result
    }
    candidate_failures = {
        name for name, result in candidate_vector.hard_constraints.items() if not result
    }
    if candidate_failures - base_failures:
        classification = "regression"
    elif (
        base_failures
        and not candidate_failures
        and classification
        not in {
            "regression",
            "uncertainty",
        }
    ):
        classification = "improvement"
    return _json_object(
        {
            "status": "paired",
            "classification": classification,
            "baseline_run_id": baseline.record.run_id,
            "candidate_run_id": candidate.record.run_id,
            "baseline_source_task_id": baseline.record.task_id,
            "candidate_source_task_id": candidate.record.task_id,
            "task_group_id": baseline.record.task_group_id,
            "objective_version": base_vector.objective_version,
            "evaluator_version": base_vector.evaluator_version,
            "environment_id": baseline.metrics.run_manifest.environment_id,
            "environment_version": baseline.metrics.run_manifest.environment_version,
            "baseline_hard_constraint_failures": tuple(sorted(base_failures)),
            "candidate_hard_constraint_failures": tuple(sorted(candidate_failures)),
            "native_objective_deltas": deltas,
        }
    )


def _partial_failure_flags(run: LoadedRun) -> dict[str, JsonValue]:
    if run.metrics is None:
        return {"available": False, "reason": "no partial event evidence recorded"}
    vectors = _latest_valid_vectors(
        run.metrics.objective_vectors,
        run.record.objective_version,
        run.record.evaluator_version,
    )
    failures = (
        sorted(
            name for name, passed in vectors[-1].hard_constraints.items() if not passed
        )
        if vectors
        else ()
    )
    return _json_object(
        {
            "available": True,
            "hard_constraint_failures": tuple(failures),
            "valid_external_evaluations": sum(
                vector.valid for vector in run.metrics.objective_vectors
            ),
            "accepted_lineage_successors": run.metrics.facts[
                "lineage_successors"
            ].value,
            "duplicate_external_effects": run.metrics.facts[
                "duplicate_external_effects"
            ].availability.value,
        }
    )


def _objective_summaries(loaded: Sequence[LoadedRun]) -> list[dict[str, JsonValue]]:
    groups: dict[
        tuple[str, str, str, str, int, str, str],
        dict[str, dict[str, float] | set[str]],
    ] = {}
    for item in loaded:
        if item.metrics is None:
            continue
        vectors = _latest_valid_vectors(
            item.metrics.objective_vectors,
            item.record.objective_version,
            item.record.evaluator_version,
        )
        for vector in vectors[-1:]:
            for name, value in vector.objectives.items():
                if _number(value):
                    key = (
                        item.record.condition,
                        item.record.task_group_id,
                        item.metrics.run_manifest.environment_id,
                        item.metrics.run_manifest.environment_version,
                        vector.objective_version,
                        vector.evaluator_version,
                        name,
                    )
                    group = groups.setdefault(
                        key, {"values": {}, "excluded_censored": set()}
                    )
                    if item.record.censored:
                        excluded = group["excluded_censored"]
                        assert isinstance(excluded, set)
                        excluded.add(item.record.run_id)
                    else:
                        values = group["values"]
                        assert isinstance(values, dict)
                        values[item.record.run_id] = float(value)
    rows = []
    for key, group in sorted(groups.items()):
        run_values = group["values"]
        excluded = group["excluded_censored"]
        assert isinstance(run_values, dict)
        assert isinstance(excluded, set)
        values = list(run_values.values())
        ordered = sorted(values)
        rows.append(
            {
                "condition": key[0],
                "task_id": key[1],
                "task_group_id": key[1],
                "environment_id": key[2],
                "environment_version": key[3],
                "objective_version": key[4],
                "evaluator_version": key[5],
                "objective": key[6],
                "count": len(values),
                "excluded_censored_count": len(excluded),
                "median": statistics.median(ordered) if ordered else None,
                "iqr": (
                    _quantile(ordered, 0.75) - _quantile(ordered, 0.25)
                    if ordered
                    else None
                ),
                "range": (ordered[0], ordered[-1]) if ordered else None,
            }
        )
    return rows


# Each declared metric is tracked across samples and missingness by identity.
# pylint: disable=too-many-locals
def _run_metric_summaries(
    loaded: Sequence[LoadedRun],
) -> list[dict[str, JsonValue]]:
    samples: dict[
        tuple[str, str, str, str, int, str, str],
        list[tuple[str, float]],
    ] = {}
    availability: dict[tuple[str, str, str, str, int, str, str], dict[str, int]] = {}
    definitions = (
        "actions",
        "evaluations",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "model_duration_ms",
        "wall_interval_ms",
        "attempt_to_commit_ratio",
        "malformed_model_rate",
        "repeated_action_proposals",
        "resume_events",
        "lineage_successors",
        "external_evaluation_outcomes",
    )
    for item in loaded:
        if item.metrics is None or item.record.censored:
            for name in definitions:
                key = (
                    item.record.condition,
                    item.record.task_group_id,
                    item.record.environment_id,
                    item.record.environment_version,
                    item.record.objective_version,
                    item.record.evaluator_version,
                    name,
                )
                counts = availability.setdefault(
                    key, {"known": 0, "unknown": 0, "excluded": 0}
                )
                counts["excluded"] += 1
            continue
        vectors = _latest_valid_vectors(
            item.metrics.objective_vectors,
            item.record.objective_version,
            item.record.evaluator_version,
        )
        version = (
            vectors[-1].objective_version if vectors else item.record.objective_version
        )
        evaluator = (
            vectors[-1].evaluator_version if vectors else item.record.evaluator_version
        )
        identity_prefix = (
            item.record.condition,
            item.record.task_group_id,
            item.record.environment_id,
            item.record.environment_version,
            version,
            evaluator,
        )
        per_run = _declared_scalar_metrics(item)
        for name in definitions:
            scalar = per_run.get(name)
            key = (*identity_prefix, name)
            counts = availability.setdefault(
                key, {"known": 0, "unknown": 0, "excluded": 0}
            )
            if scalar is None:
                counts["unknown"] += 1
                continue
            counts["known"] += 1
            samples.setdefault(key, []).append((item.record.run_id, scalar))
    rows: list[dict[str, JsonValue]] = []
    for key, counts in sorted(availability.items()):
        values = sorted(value for _, value in samples.get(key, ()))
        rows.append(
            {
                "condition": key[0],
                "task_group_id": key[1],
                "environment_id": key[2],
                "environment_version": key[3],
                "objective_version": key[4],
                "evaluator_version": key[5],
                "metric": key[6],
                "sample_count": len(values),
                "unknown_count": counts["unknown"],
                "excluded_censored_count": counts["excluded"],
                "median": statistics.median(values) if values else None,
                "iqr": (
                    _quantile(values, 0.75) - _quantile(values, 0.25)
                    if values
                    else None
                ),
                "range": (values[0], values[-1]) if values else None,
                "sample_unit": "one declared final fact per run",
                "source": "the named automatically derived per-run RunMetrics fact",
                "denominator": "retained in the per-run fact where defined",
            }
        )
    return rows


def _declared_scalar_metrics(item: LoadedRun) -> dict[str, float]:
    assert item.metrics is not None
    result: dict[str, float] = {}
    facts = item.metrics.facts
    for name in (
        "model_duration_ms",
        "wall_interval_ms",
        "attempt_to_commit_ratio",
        "resume_events",
        "lineage_successors",
    ):
        fact = facts[name]
        if fact.availability is MetricAvailability.KNOWN and _number(fact.value):
            result[name] = float(fact.value)
    for name in (
        "actions",
        "evaluations",
        "model_calls",
        "input_tokens",
        "output_tokens",
    ):
        fact = facts[name]
        if fact.availability is MetricAvailability.KNOWN and _number(fact.value):
            result[name] = float(fact.value)
    malformed = facts["malformed_model_rate"]
    malformed_rate = (
        malformed.value.get("rate") if isinstance(malformed.value, Mapping) else None
    )
    if malformed.availability is MetricAvailability.KNOWN and _number(malformed_rate):
        result["malformed_model_rate"] = float(malformed_rate)
    repeated = facts["repeated_action_proposals"]
    repeated_count = (
        repeated.value.get("repeated_proposals")
        if isinstance(repeated.value, Mapping)
        else None
    )
    if repeated.availability is MetricAvailability.KNOWN and _number(repeated_count):
        result["repeated_action_proposals"] = float(repeated_count)
    outcomes = facts["external_evaluation_outcomes"]
    rejection_rate = (
        outcomes.value.get("rejected_or_constraint_failed_rate")
        if isinstance(outcomes.value, Mapping)
        else None
    )
    if outcomes.availability is MetricAvailability.KNOWN and _number(rejection_rate):
        result["external_evaluation_outcomes"] = float(rejection_rate)
    return result


def _completion_summary(
    loaded: Sequence[LoadedRun], campaign: Campaign
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for condition in (campaign.baseline, campaign.candidate):
        rows = [item for item in loaded if item.record.condition == condition]
        uncensored = [item for item in rows if not item.record.censored]
        censored = [item for item in rows if item.record.censored]
        result[condition] = {
            **_completion_counts(uncensored),
            "censored_count": len(censored),
            "censored_policy": campaign.censored_policy,
            "censored_observations_excluded_from_denominator": _completion_counts(
                censored
            ),
        }
    return result


def _completion_counts(rows: Sequence[LoadedRun]) -> dict[str, int]:
    known = [
        item.metrics.facts["external_completion"].value
        for item in rows
        if item.metrics is not None
        and item.metrics.facts["external_completion"].availability
        is MetricAvailability.KNOWN
    ]
    return {
        "completed": sum(value is True for value in known),
        "evaluated_incomplete": sum(value is False for value in known),
        "unknown_or_unavailable": len(rows) - len(known),
        "denominator": len(rows),
    }


def _assistance_summary(
    loaded: Sequence[LoadedRun], command_types: Sequence[str]
) -> dict[str, JsonValue]:
    summary: dict[str, JsonValue] = {
        "counted_command_types": tuple(command_types),
        "admitted": 0,
        "applied": 0,
        "rejected": 0,
        "classification": (
            "no recorded assisting intervention"
            if not command_types
            else "manifest-defined command types"
        ),
    }
    admitted = applied = rejected = 0
    for item in loaded:
        if item.metrics is None:
            continue
        for command_type, transition, disposition in item.metrics.operator_commands:
            if command_type not in command_types:
                continue
            admitted += 1
            if transition:
                if disposition == "rejected":
                    rejected += 1
                else:
                    applied += 1
    summary.update({"admitted": admitted, "applied": applied, "rejected": rejected})
    return summary


def _latest_valid_vectors(
    vectors: Sequence[ObjectiveVector],
    objective_version: int | None = None,
    evaluator_version: str | None = None,
) -> tuple[ObjectiveVector, ...]:
    valid = tuple(
        vector
        for vector in vectors
        if vector.valid
        and (objective_version is None or vector.objective_version == objective_version)
        and (evaluator_version is None or vector.evaluator_version == evaluator_version)
    )
    if not valid:
        return ()
    current_version = valid[-1].objective_version
    current_evaluator = valid[-1].evaluator_version
    return tuple(
        vector
        for vector in valid
        if vector.objective_version == current_version
        and vector.evaluator_version == current_evaluator
    )


def _per_run_rows(loaded: Sequence[LoadedRun]) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    for item in loaded:
        metrics_value = None if item.metrics is None else to_json_value(item.metrics)
        rows.append(
            {
                "run_id": item.record.run_id,
                "condition": item.record.condition,
                "task_id": item.record.task_id,
                "task_group_id": item.record.task_group_id,
                "environment_id": item.record.environment_id,
                "environment_version": item.record.environment_version,
                "evaluator_version": item.record.evaluator_version,
                "objective_version": item.record.objective_version,
                "seed": item.record.seed,
                "replicate_id": item.record.replicate_id,
                "source_revision": item.record.source_revision,
                "configuration_digest": item.record.configuration_digest,
                "censored": item.record.censored,
                "event_path": item.record.event_path,
                "event_sha256": item.record.event_sha256,
                "metrics": metrics_value,
                "storage": dict(item.storage_facts),
            }
        )
    return rows


def _repetition_groups(
    pairs: Sequence[Mapping[str, JsonValue]], minimum: int
) -> list[dict[str, JsonValue]]:
    groups: dict[tuple[str, str, str, int, str], list[Mapping[str, JsonValue]]] = {}
    for pair in pairs:
        if pair.get("status") != "paired":
            continue
        identities = tuple(
            pair.get(name)
            for name in (
                "task_group_id",
                "environment_id",
                "environment_version",
                "objective_version",
                "evaluator_version",
            )
        )
        if not all(isinstance(value, (str, int)) for value in identities):
            continue
        objective_version = identities[3]
        if not isinstance(objective_version, int):
            continue
        key = (
            str(identities[0]),
            str(identities[1]),
            str(identities[2]),
            objective_version,
            str(identities[4]),
        )
        groups.setdefault(key, []).append(pair)
    return [
        {
            "task_group_id": key[0],
            "environment_id": key[1],
            "environment_version": key[2],
            "objective_version": key[3],
            "evaluator_version": key[4],
            "paired_repetitions": len(rows),
            "requirement_met": len(rows) >= minimum,
            "classification_counts": {
                name: sum(row.get("classification") == name for row in rows)
                for name in ("improvement", "equivalence", "regression", "uncertainty")
            },
        }
        for key, rows in sorted(groups.items())
    ]


def _efficiency_summary(
    loaded: Sequence[LoadedRun], campaign: Campaign
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for condition in (campaign.baseline, campaign.candidate):
        rows = [item for item in loaded if item.record.condition == condition]
        groups: dict[tuple[str, str, str, int, str], list[LoadedRun]] = {}
        for item in rows:
            key = (
                item.record.task_group_id,
                item.record.environment_id,
                item.record.environment_version,
                item.record.objective_version,
                item.record.evaluator_version,
            )
            groups.setdefault(key, []).append(item)
        group_rows: list[dict[str, JsonValue]] = []
        for key, group_rows_input in sorted(groups.items()):
            uncensored_rows = [
                item for item in group_rows_input if not item.record.censored
            ]
            censored_rows = [item for item in group_rows_input if item.record.censored]
            completed = sum(
                item.metrics is not None
                and item.metrics.facts["external_completion"].availability
                is MetricAvailability.KNOWN
                and item.metrics.facts["external_completion"].value is True
                for item in uncensored_rows
            )
            usage_known = all(
                item.metrics is not None
                and item.metrics.counters.input_tokens is not None
                and item.metrics.counters.output_tokens is not None
                for item in uncensored_rows
            ) and bool(uncensored_rows)
            input_total = sum(
                item.metrics.counters.input_tokens or 0
                for item in uncensored_rows
                if item.metrics is not None
            )
            output_total = sum(
                item.metrics.counters.output_tokens or 0
                for item in uncensored_rows
                if item.metrics is not None
            )
            completion_observations = _completion_counts(uncensored_rows)
            token_value = (
                (input_total + output_total) / completed
                if usage_known and completed > 0
                else None
            )
            group_rows.append(
                {
                    "task_group_id": key[0],
                    "environment_id": key[1],
                    "environment_version": key[2],
                    "objective_version": key[3],
                    "evaluator_version": key[4],
                    "uncensored_run_count": len(uncensored_rows),
                    "excluded_censored_count": len(censored_rows),
                    "completion_observation_denominator": completion_observations[
                        "denominator"
                    ],
                    "completion_unknown_count": completion_observations[
                        "unknown_or_unavailable"
                    ],
                    "excluded_censored_completion_observations": _completion_counts(
                        censored_rows
                    ),
                    "tokens_per_external_completion": token_value,
                    "token_availability": (
                        "known" if token_value is not None else "unknown"
                    ),
                    "token_reason": (
                        None
                        if token_value is not None
                        else (
                            "no uncensored runs in this group"
                            if not uncensored_rows
                            else "usage unknown or no externally accepted completion"
                        )
                    ),
                    "numerator_tokens": input_total + output_total,
                    "completion_denominator": completed,
                    "cost_per_external_completion": None,
                    "cost_availability": "unavailable",
                    "cost_reason": (
                        "campaign does not declare a common currency/unit identity; "
                        "provider-native monetary amounts are not pooled"
                    ),
                }
            )
        result[condition] = {"comparable_groups": tuple(group_rows)}
    return result


def _campaign_disposition(
    pairs: Sequence[Mapping[str, JsonValue]],
    repetition_groups: Sequence[Mapping[str, JsonValue]],
    campaign: Campaign,
) -> str:
    missing = any(pair.get("status") == "missing" for pair in pairs)
    censored = any(pair.get("status") == "censored" for pair in pairs)
    weak = not repetition_groups or any(
        group.get("requirement_met") is not True for group in repetition_groups
    )
    uncertain = any(
        pair.get("status") == "paired" and pair.get("classification") == "uncertainty"
        for pair in pairs
    )
    if any(
        (
            missing and campaign.missing_policy == "inconclusive",
            censored and campaign.censored_policy == "inconclusive",
            weak,
            uncertain,
        )
    ):
        return "inconclusive"
    return "descriptive_only"


def _comparable_identity(
    baseline: LoadedRun,
    candidate: LoadedRun,
    base_vectors: Sequence[ObjectiveVector],
    candidate_vectors: Sequence[ObjectiveVector],
) -> bool:
    if not base_vectors or not candidate_vectors:
        return False
    assert baseline.metrics is not None and candidate.metrics is not None
    left, right = base_vectors[-1], candidate_vectors[-1]
    return (
        left.objective_version == right.objective_version
        and left.evaluator_version == right.evaluator_version
        and baseline.metrics.run_manifest.environment_id
        == candidate.metrics.run_manifest.environment_id
        and baseline.metrics.run_manifest.environment_version
        == candidate.metrics.run_manifest.environment_version
    )


def _classify_delta(delta: float, tolerance: Mapping[str, JsonValue]) -> str:
    if delta > _tolerance_number(tolerance, "improvement"):
        return "improvement"
    if delta < -_tolerance_number(tolerance, "regression"):
        return "regression"
    if abs(delta) <= _tolerance_number(tolerance, "equivalence"):
        return "equivalence"
    if abs(delta) <= _tolerance_number(tolerance, "uncertainty"):
        return "uncertainty"
    return "uncertainty"


def _tolerance_number(tolerance: Mapping[str, JsonValue], name: str) -> float:
    value = tolerance.get(name)
    if not _number(value):
        raise IntegrityError(f"objective tolerance {name} is not numeric")
    return float(value)


def _validate_tolerances(
    value: Mapping[str, JsonValue],
) -> Mapping[str, Mapping[str, JsonValue]]:
    result: dict[str, Mapping[str, JsonValue]] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name or not isinstance(item, Mapping):
            raise IntegrityError("objective tolerance entries must be named objects")
        direction = item.get("direction")
        if direction not in {"maximize", "minimize"}:
            raise IntegrityError(
                f"objective {name} direction must be maximize or minimize"
            )
        tolerance: dict[str, JsonValue] = {"direction": direction}
        for field in ("improvement", "equivalence", "regression", "uncertainty"):
            number = item.get(field)
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not math.isfinite(number)
                or number < 0
            ):
                raise IntegrityError(
                    f"objective {name} {field} tolerance must be finite and "
                    "non-negative"
                )
            tolerance[field] = float(number)
        result[name] = tolerance
    return result


# Strict run-table boundary checks intentionally cover each optional identity.
# pylint: disable=too-many-branches
def _run_input(value: object) -> RunInput:
    if not isinstance(value, Mapping):
        raise IntegrityError("each run table entry must be an object")
    event_path = value.get("event_path")
    event_digest = value.get("event_sha256")
    journal_path = value.get("journal_path")
    journal_digest = value.get("journal_sha256")
    if event_path is not None and not isinstance(event_path, str):
        raise IntegrityError("event_path must be a string or null")
    if event_digest is not None and not isinstance(event_digest, str):
        raise IntegrityError("event_sha256 must be a string or null")
    if journal_path is not None and not isinstance(journal_path, str):
        raise IntegrityError("journal_path must be a string or null")
    if journal_digest is not None and not isinstance(journal_digest, str):
        raise IntegrityError("journal_sha256 must be a string or null")
    if (journal_path is None) != (journal_digest is None):
        raise IntegrityError(
            "journal_path and journal_sha256 must be supplied together"
        )
    censored = value.get("censored", False)
    if not isinstance(censored, bool):
        raise IntegrityError("censored must be a boolean")
    return RunInput(
        _mapping_string(value, "run_id"),
        _mapping_string(value, "condition"),
        _mapping_string(value, "task_id"),
        _mapping_string(value, "task_group_id"),
        _mapping_string(value, "environment_id"),
        _mapping_string(value, "environment_version"),
        _mapping_string(value, "evaluator_version"),
        _positive_int(value, "objective_version"),
        _require_int(value, "seed"),
        _mapping_string(value, "replicate_id"),
        _mapping_string(value, "source_revision"),
        _require_sha256(value, "configuration_digest"),
        event_path,
        event_digest,
        journal_path,
        journal_digest,
        censored,
    )


def _validate_run_set(
    runs: Sequence[RunInput],
    baseline: str,
    candidate: str,
    pair_roster: Sequence[tuple[str, int, str]] = (),
) -> None:
    if len(set(pair_roster)) != len(pair_roster):
        raise IntegrityError("pair_roster identities must be unique")
    declared_pairs = set(pair_roster)
    run_ids: set[str] = set()
    pairs: set[tuple[str, str, int, str]] = set()
    for run in runs:
        if not run.run_id or run.run_id in run_ids:
            raise IntegrityError("run IDs must be non-empty and unique")
        run_ids.add(run.run_id)
        if run.condition not in {baseline, candidate}:
            raise IntegrityError(f"run {run.run_id} has an undeclared condition")
        if not _GIT_OID.fullmatch(run.source_revision):
            raise IntegrityError(f"run {run.run_id} source revision must be full")
        if not run.task_group_id or not run.replicate_id:
            raise IntegrityError("task_group_id and replicate_id must be non-empty")
        if (
            declared_pairs
            and (
                run.task_group_id,
                run.seed,
                run.replicate_id,
            )
            not in declared_pairs
        ):
            raise IntegrityError(f"run {run.run_id} is not in the frozen pair roster")
        key = (run.condition, run.task_group_id, run.seed, run.replicate_id)
        if key in pairs:
            raise IntegrityError(f"duplicate paired run identity: {key}")
        pairs.add(key)
        if (run.event_path is None) != (run.event_sha256 is None):
            raise IntegrityError(
                "event_path and event_sha256 must be supplied together"
            )
        if not run.censored and (
            run.event_path is None or not _SHA256.fullmatch(run.event_sha256 or "")
        ):
            raise IntegrityError(f"run {run.run_id} lacks a valid event evidence hash")
        if run.event_sha256 is not None and not _SHA256.fullmatch(run.event_sha256):
            raise IntegrityError(f"run {run.run_id} event hash is invalid")
        if (run.journal_path is None) != (run.journal_sha256 is None):
            raise IntegrityError(
                "journal_path and journal_sha256 must be supplied together"
            )
        if run.journal_sha256 is not None and not _SHA256.fullmatch(run.journal_sha256):
            raise IntegrityError(f"run {run.run_id} journal hash is invalid")


def _frozen_rules(manifest: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    keys = (
        "campaign_id",
        "provenance",
        "conditions",
        "pairing",
        "objective_tolerances",
        "assistance_command_types",
        "pair_roster",
    )
    missing = [key for key in keys if key not in manifest]
    if missing:
        raise IntegrityError(f"frozen rules lack required fields: {', '.join(missing)}")
    return {key: manifest[key] for key in keys}


def _pair_roster(value: JsonValue | None) -> tuple[tuple[str, int, str], ...]:
    if not isinstance(value, (tuple, list)) or not value:
        raise IntegrityError("pair_roster must be a non-empty array")
    roster: list[tuple[str, int, str]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise IntegrityError("pair_roster entries must be objects")
        task_group = _mapping_string(row, "task_group_id")
        replicate = _mapping_string(row, "replicate_id")
        seed = row.get("seed")
        if (
            not task_group
            or not replicate
            or isinstance(seed, bool)
            or not isinstance(seed, int)
        ):
            raise IntegrityError(
                "pair_roster entries need task group, seed, and replicate"
            )
        roster.append((task_group, seed, replicate))
    if len(set(roster)) != len(roster):
        raise IntegrityError("pair_roster identities must be unique")
    return tuple(roster)


def _repository_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise IntegrityError("evidence paths must be repository-relative")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise IntegrityError("evidence path escapes repository_root")
    return resolved


def _read_verified(path: Path, digest: str, label: str) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IntegrityError(f"cannot read {label}: {exc}") from exc
    if _sha256(raw) != digest:
        raise IntegrityError(f"{label} SHA-256 does not match frozen manifest")
    return raw


def _require_string(value: Mapping[str, JsonValue], name: str) -> str:
    candidate = value.get(name)
    if not isinstance(candidate, str) or not candidate:
        raise IntegrityError(f"{name} must be a non-empty string")
    return candidate


def _require_int(
    value: Mapping[str, JsonValue], name: str, expected: int | None = None
) -> int:
    candidate = value.get(name)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise IntegrityError(f"{name} must be an integer")
    if expected is not None and candidate != expected:
        raise IntegrityError(f"{name} must equal {expected}")
    return candidate


def _positive_int(value: Mapping[str, JsonValue], name: str) -> int:
    candidate = _require_int(value, name)
    if candidate < 1:
        raise IntegrityError(f"{name} must be positive")
    return candidate


def _require_sha256(value: Mapping[str, JsonValue], name: str) -> str:
    candidate = _require_string(value, name)
    if not _SHA256.fullmatch(candidate):
        raise IntegrityError(f"{name} must be a SHA-256 digest")
    return candidate


def _mapping_string(value: Mapping[str, JsonValue], name: str) -> str:
    candidate = value.get(name)
    if not isinstance(candidate, str):
        raise IntegrityError(f"{name} must be a string")
    return candidate


def _policy(value: Mapping[str, JsonValue], name: str) -> str:
    candidate = _mapping_string(value, name)
    if candidate not in _POLICIES:
        raise IntegrityError(f"{name} must be inconclusive or exclude")
    return candidate


def _number(value: object) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _string_sequence(value: JsonValue | None) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise IntegrityError("assistance_command_types must be an array of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise IntegrityError("assistance_command_types must be non-empty strings")
        result.append(item)
    return tuple(result)


def _json_object(value: Mapping[str, object]) -> dict[str, JsonValue]:
    encoded = to_json_value(value)
    if not isinstance(encoded, Mapping):
        raise IntegrityError("report output must be a JSON object")
    return dict(encoded)


def _quantile(values: Sequence[float], p: float) -> float:
    if len(values) == 1:
        return values[0]
    index = (len(values) - 1) * p
    low = math.floor(index)
    high = math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the CLI request and compose the evidence report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = write_report(arguments.manifest, arguments.output)
    print(canonical_json(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through CLI composition
    raise SystemExit(main())
