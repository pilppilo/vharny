"""Replay-derived Phase 1 run evidence."""

# The BENCH derivation stays beside unchanged RunCounters to preserve the exact
# public compatibility boundary; its functions are cohesive fact derivations.
# pylint: disable=too-many-lines

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping

from .codec import canonical_json, parse_json_object
from .errors import ContractError, IntegrityError
from .models import ComponentIdentity, Event, JsonValue, RecordEnvelope, frozen_mapping
from .replay import replay
from .transitions import SessionState
from .usage_ledger import UsageLedger

OPERATIONAL_MEASUREMENT_SCOPE = "session_context_and_evaluation_successful_reads_v1"
_ARTIFACT_READ_PURPOSES = frozenset(
    {"context_observation", "context_receipt_content", "evaluation_evidence"}
)


@dataclass(frozen=True, slots=True)
# BENCH-0001 evidence intentionally retains each independently reported measure.
# pylint: disable=too-many-instance-attributes
class RunCounters:
    """Diagnostic BENCH-0001 counters derived only from durable facts.

    ``model_calls`` is the measured total for main and supervisor calls;
    ``supervisor_calls`` is the measured supervisor subset of it, and
    ``conservative_supervisor_calls`` adds unresolved supervisor reservation
    exposure. The supervisor token dimensions are reported separately as
    known-or-unknown.
    """

    actions: int
    evaluations: int
    model_calls: int
    supervisor_calls: int
    conservative_supervisor_calls: int
    supervisor_input_tokens: int | None
    supervisor_output_tokens: int | None
    attempts: int
    commits: int
    checkpoints: int
    reconciliations: int
    runtime_receipts: int
    operator_commands: int
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    known_cost: str
    unknown_cost_operations: int
    abandoned_attempts: int
    rejected_attempts: int
    stale_attempts: int
    incomparable_attempts: int
    invalid_attempts: int
    hard_constraint_failed_attempts: int
    envelope: RecordEnvelope


@dataclass(frozen=True, slots=True)
# Manifest identity intentionally mirrors the external run boundary.
# pylint: disable=too-many-instance-attributes
class RunManifest:
    """Durable identity facts captured with session creation."""

    session_id: str
    task_id: str
    environment_id: str
    environment_version: str
    evaluator_version: str
    runtime: ComponentIdentity
    evaluator: ComponentIdentity
    package_version: str
    configuration_digest: str
    schema_version: int
    envelope: RecordEnvelope


class MetricAvailability(str, Enum):
    """Whether a metric is measured, unknown, unavailable, or deferred."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    DEFERRED = "deferred"


@dataclass(frozen=True, slots=True)
class MetricFact:
    """One metric value with its source and explicit missing-data semantics."""

    value: JsonValue | None
    availability: MetricAvailability
    source: str
    reason: str | None = None
    denominator: str | None = None

    def __post_init__(self) -> None:
        frozen = frozen_mapping({"value": self.value})
        object.__setattr__(self, "value", frozen["value"])


@dataclass(frozen=True, slots=True)
class ObjectiveVector:
    """One external objective vector retained at its native version boundary."""

    receipt_id: str
    objective_version: int
    evaluator_version: str
    valid: bool
    completed: bool
    externally_accepted: bool
    external_comparison: str
    promotion_disposition: str
    objectives: Mapping[str, JsonValue]
    hard_constraints: Mapping[str, bool]
    evidence_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "objectives", frozen_mapping(self.objectives))
        object.__setattr__(
            self,
            "hard_constraints",
            MappingProxyType(dict(self.hard_constraints)),
        )


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Automatically derived run facts without changing RunCounters' wire shape."""

    run_manifest: RunManifest
    counters: RunCounters
    facts: Mapping[str, MetricFact]
    objective_vectors: tuple[ObjectiveVector, ...]
    attempt_dispositions: tuple[tuple[str, str], ...]
    model_attempt_signals: Mapping[str, int]
    action_signals: Mapping[str, int]
    operator_commands: tuple[tuple[str, str, str], ...]
    per_commit_costs: tuple[tuple[str, MetricFact], ...]
    source_event_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))
        object.__setattr__(
            self,
            "model_attempt_signals",
            MappingProxyType(dict(self.model_attempt_signals)),
        )
        object.__setattr__(
            self, "action_signals", MappingProxyType(dict(self.action_signals))
        )


def events_from_jsonl(raw: bytes) -> tuple[Event, ...]:
    """Decode the canonical exported event JSONL at the evidence boundary."""
    events: list[Event] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        try:
            item = parse_json_object(line)
            payload = item.get("payload")
            if not isinstance(payload, Mapping):
                raise IntegrityError("event payload must be an object")
            event = Event(
                event_id=_json_string(item, "event_id"),
                session_id=_json_string(item, "session_id"),
                sequence=_json_integer(item, "sequence"),
                kind=_json_string(item, "kind"),
                objective_version=_json_integer(item, "objective_version"),
                payload=payload,
                recorded_at=_json_string(item, "recorded_at"),
                causation_id=_json_optional_string(item, "causation_id"),
                correlation_id=_json_optional_string(item, "correlation_id"),
                schema_version=_json_integer(item, "schema_version"),
            )
            events.append(event)
        except (ContractError, IntegrityError) as exc:
            raise IntegrityError(f"event JSONL line {line_number}: {exc}") from exc
    if not events:
        raise IntegrityError("event JSONL must contain at least one event")
    return tuple(events)


def metrics(events: Iterable[Event]) -> RunMetrics:
    """Derive BENCH-0001 metrics from a replay-validated durable event stream."""
    stream = tuple(events)
    state = replay(stream)
    run_identity = manifest(stream)
    run_counters = counters(stream)
    evaluations = tuple(
        event for event in stream if event.kind == "evaluation_received"
    )
    promoted_ids = {
        _mapping_string(event.payload.get("node"), "evaluation_receipt_id")
        for event in stream
        if event.kind == "lineage_promoted"
    }
    vectors = tuple(_objective_vector(event) for event in evaluations)
    completion = _completion_fact(
        evaluations,
        promoted_ids,
        current_objective_version=state.task.objective_version,
        finite_task=state.task.completion_mode.value == "finite",
        session_completed=state.status.value == "completed",
    )
    facts: dict[str, MetricFact] = {
        "external_completion": completion,
        "native_objective_vectors": MetricFact(
            value=tuple(
                {
                    "receipt_id": vector.receipt_id,
                    "objective_version": vector.objective_version,
                    "evaluator_version": vector.evaluator_version,
                    "externally_accepted": vector.externally_accepted,
                    "external_comparison": vector.external_comparison,
                    "promotion_disposition": vector.promotion_disposition,
                    "objectives": vector.objectives,
                }
                for vector in vectors
            ),
            availability=MetricAvailability.KNOWN,
            source="evaluation_received.receipt.objectives",
            reason=(
                "vectors retain external names and objective-version boundaries; "
                "no scalar progress is calculated"
            ),
        ),
        "final_external_native_vector": _final_native_vector_fact(
            vectors, state.task.objective_version
        ),
        "external_best_native_vector": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "external evaluation receipt contract",
            reason="the journal has no separately defined best-so-far score fact",
        ),
        "regression_vs_prior_best": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "external evaluation receipt comparison semantics",
            reason=(
                "local objective vectors are incomparable without an evaluator-owned "
                "prior-best reference"
            ),
        ),
        "resume_success": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "session_resumed event and external completion evidence",
            reason="a resume event does not independently label recovery success",
        ),
        "progress_per_resource": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "native objective vector and measured resource ledger",
            reason=(
                "no scalar progress function is defined for the native objective "
                "vector"
            ),
        ),
        "repeated_failure_signals": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "independent labeled failure outcomes",
            reason=(
                "repeated proposal or provider errors do not establish repeated "
                "task failure"
            ),
        ),
        "hard_constraints": MetricFact(
            value=tuple(
                {
                    "receipt_id": vector.receipt_id,
                    "objective_version": vector.objective_version,
                    "evaluator_version": vector.evaluator_version,
                    "results": vector.hard_constraints,
                }
                for vector in vectors
            ),
            availability=(
                MetricAvailability.KNOWN
                if any(vector.hard_constraints for vector in vectors)
                else MetricAvailability.UNAVAILABLE
            ),
            source="evaluation_received.receipt.hard_constraints",
            reason=(
                None
                if any(vector.hard_constraints for vector in vectors)
                else "no external constraint outcomes were recorded"
            ),
        ),
        "lineage_successors": MetricFact(
            value=len(state.accepted_lineage) - 1,
            availability=MetricAvailability.KNOWN,
            source="replay.accepted_lineage",
            denominator="one run",
        ),
        "lineage_sequence": MetricFact(
            tuple(
                {
                    "node_id": node.node_id,
                    "parent_id": node.parent_id,
                    "objective_version": node.objective_version,
                    "evaluation_receipt_id": node.evaluation_receipt_id,
                    "state_ref": node.state.value,
                }
                for node in state.accepted_lineage
            ),
            MetricAvailability.KNOWN,
            "replay.accepted_lineage in commit order",
            denominator="accepted lineage nodes including root",
        ),
        "terminal_references": MetricFact(
            {
                "session_status": state.status.value,
                "event_cursor": stream[-1].sequence,
                "objective_version": state.task.objective_version,
                "lineage_head_id": state.lineage_head.node_id,
                "lineage_head_receipt_id": state.lineage_head.evaluation_receipt_id,
                "lineage_head_state_id": state.lineage_head.state.value,
                "active_attempt_id": (
                    None
                    if state.active_attempt is None
                    else state.active_attempt.attempt_id
                ),
            },
            MetricAvailability.KNOWN,
            "final replayed session projection",
            denominator="one run",
        ),
        "wall_interval_ms": _wall_interval_fact(stream),
        "attempt_to_commit_ratio": _attempt_commit_ratio_fact(state),
        "attempted_directions": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "action_intended proposal names",
            reason=(
                "the event contract records action names, not variation-direction "
                "semantics"
            ),
        ),
        "intended_actions_by_name": _attempted_directions_fact(stream),
        "closed_attempt_dispositions": _attempt_rate_fact(state),
        "external_evaluation_outcomes": _external_outcomes_fact(vectors),
        "malformed_model_rate": _malformed_rate_fact(stream),
        "repeated_action_proposals": _repeated_action_fact(stream),
        "resume_events": MetricFact(
            sum(event.kind == "session_resumed" for event in stream),
            MetricAvailability.KNOWN,
            "session_resumed events",
            denominator="one run",
        ),
        "receipts_missing_optional_envelope_correlation": _uncorrelated_receipt_fact(
            stream
        ),
        "unmatched_external_receipts": MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "receipt admission and operation reconciliation",
            reason=(
                "receipts are admitted against existing operations; the journal "
                "does not expose an independent unmatched-receipt outcome"
            ),
        ),
        "recovery_interval_ms": MetricFact(
            None,
            MetricAvailability.DEFERRED,
            "operation reconciliation and resume events",
            reason=(
                "events do not pair a recovery start with a terminal recovery "
                "boundary"
            ),
        ),
        "artifacts_referenced": _artifact_fact(stream),
        "terminal_artifact_references": _terminal_artifacts_fact(
            vectors, state.lineage_head.evaluation_receipt_id
        ),
        "input_tokens": _usage_fact(run_counters.input_tokens, state, "input_tokens"),
        "output_tokens": _usage_fact(
            run_counters.output_tokens, state, "output_tokens"
        ),
        "measured_cost": _usage_fact(run_counters.known_cost, state, "cost"),
        "model_calls": _usage_fact(run_counters.model_calls, state, "model_calls"),
        "actions": _usage_fact(run_counters.actions, state, "actions"),
        "evaluations": _usage_fact(run_counters.evaluations, state, "evaluations"),
        "between_commit_activity": _between_commit_activity(stream, state),
        "model_duration_ms": _model_duration_fact(stream),
        "context_token_utilization": _context_utilization_fact(stream),
        "context_allocations": _context_allocations_fact(stream),
        "context_selection_duration_ms": _context_selection_duration_fact(stream),
        "session_artifact_reads": _session_artifact_reads_fact(stream),
        "context_artifact_bytes_read": _context_artifact_bytes_fact(stream),
        "duplicate_external_effects": _independent_evidence_unavailable(
            "requires external runtime identity and labeled duplicate-effect evidence"
        ),
        "recovery_correctness": _independent_evidence_unavailable(
            "requires independently labeled recovery outcomes"
        ),
        "lost_actions": _independent_evidence_unavailable(
            "requires an external action ledger or labeled reconciliation evidence"
        ),
        "replay_agreement": _independent_evidence_unavailable(
            "successful replay validates this projection; it is not independent "
            "agreement evidence"
        ),
        "trigger_precision": _independent_evidence_unavailable(
            "requires externally labeled trigger truth and false-positive outcomes"
        ),
    }
    return RunMetrics(
        run_manifest=run_identity,
        counters=run_counters,
        facts=MappingProxyType(facts),
        objective_vectors=vectors,
        attempt_dispositions=tuple(
            (item.attempt.attempt_id, item.attempt.disposition.value)
            for item in state.attempt_history
        ),
        model_attempt_signals=MappingProxyType(_model_attempt_signals(stream)),
        action_signals=MappingProxyType(_action_signals(stream)),
        operator_commands=tuple(
            (
                type(item.command).__name__,
                item.transition_kind or "",
                item.disposition or "pending",
            )
            for item in state.command_operations
        ),
        per_commit_costs=_per_commit_costs(stream, state),
        source_event_count=len(stream),
    )


def _json_string(value: Mapping[str, JsonValue], name: str) -> str:
    candidate = value.get(name)
    if not isinstance(candidate, str):
        raise IntegrityError(f"event {name} must be a string")
    return candidate


def _json_integer(value: Mapping[str, JsonValue], name: str) -> int:
    candidate = value.get(name)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise IntegrityError(f"event {name} must be an integer")
    return candidate


def _json_optional_string(value: Mapping[str, JsonValue], name: str) -> str | None:
    candidate = value.get(name)
    if candidate is not None and not isinstance(candidate, str):
        raise IntegrityError(f"event {name} must be a string or null")
    return candidate


def _mapping(value: object) -> Mapping[str, JsonValue]:
    return value if isinstance(value, Mapping) else MappingProxyType({})


def _mapping_string(value: object, name: str) -> str:
    candidate = _mapping(value).get(name)
    return candidate if isinstance(candidate, str) else ""


def _objective_vector(event: Event) -> ObjectiveVector:
    receipt = _mapping(event.payload.get("receipt"))
    disposition = event.payload.get("disposition")
    objectives = receipt.get("objectives", {})
    constraints = receipt.get("hard_constraints", {})
    evidence = receipt.get("evidence", ())
    if not isinstance(objectives, Mapping) or not isinstance(constraints, Mapping):
        raise IntegrityError("evaluation vector and constraints must be objects")
    if not all(isinstance(item, bool) for item in constraints.values()):
        raise IntegrityError("evaluation hard constraints must be boolean")
    digest_values: list[str] = []
    if isinstance(evidence, (tuple, list)):
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            digest = item.get("digest")
            if isinstance(digest, str):
                digest_values.append(digest)
    digests = tuple(digest_values)
    receipt_version = receipt.get("objective_version")
    objective_version = (
        receipt_version
        if isinstance(receipt_version, int) and not isinstance(receipt_version, bool)
        else event.objective_version
    )
    comparison = receipt.get("comparison")
    constraint_results = {
        name: result
        for name, result in constraints.items()
        if isinstance(name, str) and isinstance(result, bool)
    }
    return ObjectiveVector(
        receipt_id=_mapping_string(receipt, "receipt_id"),
        objective_version=objective_version,
        evaluator_version=_string({"value": receipt.get("evaluator_version")}, "value"),
        valid=receipt.get("valid") is True,
        completed=receipt.get("completed") is True,
        externally_accepted=receipt.get("accepted") is True,
        external_comparison=(comparison if isinstance(comparison, str) else "unknown"),
        promotion_disposition=(
            disposition if isinstance(disposition, str) else "unknown"
        ),
        objectives=MappingProxyType(dict(objectives)),
        hard_constraints=constraint_results,
        evidence_digests=digests,
    )


def _completion_fact(
    evaluations: tuple[Event, ...],
    promoted_ids: set[str],
    *,
    current_objective_version: int,
    finite_task: bool,
    session_completed: bool,
) -> MetricFact:
    if not finite_task:
        return MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "task completion_mode",
            reason="the task is ongoing and has no terminal completion outcome",
            denominator="one run",
        )
    for event in reversed(evaluations):
        receipt = _mapping(event.payload.get("receipt"))
        objective_version = receipt.get("objective_version")
        if objective_version != current_objective_version:
            continue
        receipt_id = _mapping_string(receipt, "receipt_id")
        valid = receipt.get("valid")
        completed = receipt.get("completed")
        evidence = receipt.get("completion_evidence")
        disposition = event.payload.get("disposition")
        if valid is True and completed is True:
            if (
                isinstance(evidence, (tuple, list))
                and evidence
                and disposition == "accepted"
                and receipt_id in promoted_ids
                and session_completed
            ):
                return MetricFact(
                    True,
                    MetricAvailability.KNOWN,
                    "valid evaluation_received receipt plus replayed lineage_promoted",
                    reason="external completion includes completion evidence",
                    denominator="one externally evaluated run",
                )
            return MetricFact(
                None,
                MetricAvailability.UNKNOWN,
                "evaluation_received receipt",
                reason=(
                    "completion claim lacks accepted promotion or completion evidence"
                ),
                denominator="one externally evaluated run",
            )
        if valid is True and completed is False:
            return MetricFact(
                False,
                MetricAvailability.KNOWN,
                "valid evaluation_received receipt.completed",
                reason="external evaluator reported incomplete",
                denominator="one externally evaluated run",
            )
    return MetricFact(
        None,
        MetricAvailability.UNAVAILABLE,
        "evaluation_received receipt",
        reason="no valid external completion outcome was recorded",
        denominator="one run",
    )


def _final_native_vector_fact(
    vectors: tuple[ObjectiveVector, ...], current_version: int
) -> MetricFact:
    final = next(
        (
            vector
            for vector in reversed(vectors)
            if vector.valid and vector.objective_version == current_version
        ),
        None,
    )
    if final is None:
        return MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "valid evaluation receipts for current objective version",
            reason="no valid external score exists for the current objective version",
        )
    return MetricFact(
        {
            "receipt_id": final.receipt_id,
            "objective_version": final.objective_version,
            "evaluator_version": final.evaluator_version,
            "objectives": final.objectives,
            "hard_constraints": final.hard_constraints,
            "external_comparison": final.external_comparison,
            "externally_accepted": final.externally_accepted,
            "promotion_disposition": final.promotion_disposition,
        },
        MetricAvailability.KNOWN,
        "latest valid external receipt at current objective version",
        denominator="one run's final current-version receipt",
    )


def _artifact_fact(events: tuple[Event, ...]) -> MetricFact:
    found: dict[str, int] = {}
    for event in events:
        _collect_artifacts(event.payload, found)
    return MetricFact(
        value={"count": len(found), "bytes": sum(found.values())},
        availability=MetricAvailability.KNOWN,
        source="artifact references in canonical event payloads",
        reason=(
            "reference count and declared byte size; physical presence is " "unverified"
        ),
        denominator="unique SHA-256 digest per run",
    )


def _terminal_artifacts_fact(
    vectors: tuple[ObjectiveVector, ...], receipt_id: str | None
) -> MetricFact:
    terminal = next(
        (vector for vector in reversed(vectors) if vector.receipt_id == receipt_id),
        None,
    )
    if terminal is None:
        return MetricFact(
            {"count": 0, "digests": ()},
            MetricAvailability.KNOWN,
            "final committed lineage head evaluation receipt",
            reason="final lineage head has no external evaluation receipt",
            denominator="unique evidence digests on terminal receipt",
        )
    return MetricFact(
        {
            "count": len(terminal.evidence_digests),
            "digests": terminal.evidence_digests,
        },
        MetricAvailability.KNOWN,
        "final committed lineage head evaluation receipt evidence",
        denominator="receipt evidence digest entries",
    )


def _collect_artifacts(value: object, found: dict[str, int]) -> None:
    if isinstance(value, Mapping):
        digest, size = value.get("digest"), value.get("size")
        if (
            isinstance(digest, str)
            and isinstance(size, int)
            and not isinstance(size, bool)
            and _is_artifact_reference(value, digest, size)
        ):
            found[digest] = size
        for child in value.values():
            _collect_artifacts(child, found)
    elif isinstance(value, (tuple, list)):
        for child in value:
            _collect_artifacts(child, found)


def _is_artifact_reference(
    value: Mapping[str, JsonValue], digest: object, size: object
) -> bool:
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and isinstance(value.get("media_type"), str)
        and isinstance(value.get("provenance"), str)
    )


def _usage_fact(value: int | str | None, state: SessionState, name: str) -> MetricFact:
    unavailable = set(
        state.ledger.unknown_cost_operation_ids
        if name == "cost"
        else state.ledger.unknown_usage_operation_ids
    )
    if name == "cost":
        unavailable.update(
            reservation.operation_id for reservation in state.ledger.active_reservations
        )
    if name == "cost" and unavailable:
        reported_value: JsonValue | None = {
            "known_partial": value,
            "unknown_operations": len(unavailable),
        }
    else:
        reported_value = value
    if value is None or unavailable:
        return MetricFact(
            reported_value,
            MetricAvailability.UNKNOWN,
            f"replay.usage_ledger.{name}",
            reason=(
                "one or more relevant usage measurements are unsettled or incomplete"
            ),
            denominator="all recorded run operations",
        )
    return MetricFact(
        value,
        MetricAvailability.KNOWN,
        f"replay.usage_ledger.{name}",
        denominator="all measured run operations",
    )


def _model_duration_fact(events: tuple[Event, ...]) -> MetricFact:
    terminals = tuple(
        event for event in events if event.kind in {"model_decided", "model_malformed"}
    )
    durations = tuple(event.payload.get("duration_ms") for event in terminals)
    pending = {
        _mapping_string(event.payload, "request_id")
        for event in events
        if event.kind == "model_intended"
    } - {
        _mapping_string(event.payload, "request_id")
        for event in events
        if event.kind in {"model_decided", "model_malformed"}
    }
    if pending:
        return MetricFact(
            None,
            MetricAvailability.UNKNOWN,
            "model_intended without model_decided/model_malformed terminal",
            reason="one or more main-model calls have unresolved duration",
            denominator="main-model calls",
        )
    if not terminals:
        return MetricFact(
            0,
            MetricAvailability.KNOWN,
            "no main-model terminal events",
            denominator="main-model calls",
        )
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in durations
    ):
        return MetricFact(
            None,
            MetricAvailability.UNKNOWN,
            "model_decided/model_malformed.duration_ms",
            reason="at least one main-model duration is missing or malformed",
            denominator="main-model calls",
        )
    return MetricFact(
        sum(value for value in durations if isinstance(value, int)),
        MetricAvailability.KNOWN,
        "model_decided/model_malformed.duration_ms",
        reason="excludes evaluator, runtime, and supervisor latency",
        denominator="main-model calls",
    )


def _context_selection_duration_fact(events: tuple[Event, ...]) -> MetricFact:
    """Return measured selection durations only for a declared modern scope."""
    selections = tuple(
        event
        for event in events
        if event.kind in {"context_assembled", "context_overflow"}
    )
    durations = tuple(event.payload.get("duration_ns") for event in selections)
    scope_known = _has_operational_measurement_scope(events)
    complete = scope_known and all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in durations
    )
    observed_ms = tuple(
        value / 1_000_000
        for value in durations
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )
    return MetricFact(
        value=observed_ms,
        availability=(
            MetricAvailability.KNOWN if complete else MetricAvailability.UNKNOWN
        ),
        source="context_assembled/context_overflow.duration_ns",
        reason=(
            "measures context selection only; excludes preceding artifact I/O"
            if complete
            else "legacy or incomplete context selection timing evidence"
        ),
        denominator="context assembly attempts, including overflow",
    )


def _session_artifact_reads_fact(events: tuple[Event, ...]) -> MetricFact:
    """Count successful, purpose-labeled operational reads per invocation."""
    counts = {purpose: 0 for purpose in _ARTIFACT_READ_PURPOSES}
    byte_totals = {purpose: 0 for purpose in _ARTIFACT_READ_PURPOSES}
    read_events = tuple(event for event in events if event.kind == "artifact_read")
    complete_scope = _has_operational_measurement_scope(events)
    for event in read_events:
        read_scope = event.payload.get("scope")
        purpose = event.payload.get("purpose")
        byte_count = event.payload.get("bytes_read")
        valid_byte_count = (
            isinstance(byte_count, int)
            and not isinstance(byte_count, bool)
            and byte_count >= 0
        )
        if (
            read_scope != OPERATIONAL_MEASUREMENT_SCOPE
            or not isinstance(purpose, str)
            or purpose not in counts
            or not valid_byte_count
        ):
            complete_scope = False
        if isinstance(purpose, str) and purpose in counts and valid_byte_count:
            assert isinstance(byte_count, int)
            counts[purpose] += 1
            byte_totals[purpose] += byte_count
    return MetricFact(
        value={
            purpose: {
                "successful_reads": counts[purpose],
                "bytes_read": byte_totals[purpose],
            }
            for purpose in sorted(counts)
        },
        availability=(
            MetricAvailability.KNOWN if complete_scope else MetricAvailability.UNKNOWN
        ),
        source=(
            "artifact_read events in "
            "session_context_and_evaluation_successful_reads_v1"
        ),
        reason=(
            "counts each successful call, including repeated digest reads; excludes "
            "creation validation, replay, and external runtime reads"
            if complete_scope
            else "legacy or incomplete selected-scope artifact read evidence"
        ),
        denominator="context observation and evaluator evidence reads",
    )


def _context_artifact_bytes_fact(events: tuple[Event, ...]) -> MetricFact:
    """Return successful bytes read to build the current context observation."""
    reads = _session_artifact_reads_fact(events)
    context_bytes = 0
    if isinstance(reads.value, Mapping):
        context_values = reads.value.get("context_observation")
        if isinstance(context_values, Mapping):
            bytes_value = context_values.get("bytes_read")
            if isinstance(bytes_value, int) and not isinstance(bytes_value, bool):
                context_bytes = bytes_value
    return MetricFact(
        value=context_bytes,
        availability=reads.availability,
        source="artifact_read bytes_read where purpose=context_observation",
        reason=reads.reason,
        denominator="successful context observation reads",
    )


def _has_operational_measurement_scope(events: tuple[Event, ...]) -> bool:
    """Whether session creation declared the selected additive metric scope."""
    return (
        bool(events)
        and events[0].kind == "session_created"
        and events[0].payload.get("operational_measurement_scope")
        == OPERATIONAL_MEASUREMENT_SCOPE
    )


def _context_utilization_fact(events: tuple[Event, ...]) -> MetricFact:
    allocations: dict[str, Mapping[str, JsonValue]] = {}
    for event in events:
        if event.kind != "model_intended":
            continue
        manifest_value = event.payload.get("manifest")
        if not isinstance(manifest_value, Mapping):
            continue
        request_id = _mapping_string(event.payload, "request_id")
        if request_id in allocations and allocations[request_id] != manifest_value:
            return MetricFact(
                None,
                MetricAvailability.UNKNOWN,
                "model_intended manifests keyed by request_id",
                reason="one request ID has conflicting context allocations",
                denominator="unique main-model request IDs",
            )
        allocations[request_id] = manifest_value
    manifests = tuple(allocations.values())
    if not manifests:
        return MetricFact(
            None,
            MetricAvailability.UNAVAILABLE,
            "model event context manifests",
            reason="no context manifest was recorded",
            denominator="sum of recorded context token budgets",
        )
    pairs = tuple(
        (manifest.get("tokens_used"), manifest.get("token_budget"))
        for manifest in manifests
    )
    if any(
        isinstance(used, bool)
        or not isinstance(used, int)
        or isinstance(budget, bool)
        or not isinstance(budget, int)
        or budget <= 0
        for used, budget in pairs
    ):
        return MetricFact(
            None,
            MetricAvailability.UNKNOWN,
            "context manifest tokens_used/token_budget",
            reason="one or more manifests lack valid token counts",
            denominator="sum of recorded context token budgets",
        )
    used_total = sum(used for used, _ in pairs if isinstance(used, int))
    budget_total = sum(budget for _, budget in pairs if isinstance(budget, int))
    return MetricFact(
        {
            "ratio": used_total / budget_total,
            "tokens_used": used_total,
            "token_budget": budget_total,
        },
        MetricAvailability.KNOWN,
        "unique model_intended manifests keyed by request_id",
        denominator="sum of recorded context token budgets",
    )


def _context_allocations_fact(events: tuple[Event, ...]) -> MetricFact:
    allocations: dict[str, JsonValue] = {}
    for event in events:
        if event.kind != "model_intended":
            continue
        request_id = _mapping_string(event.payload, "request_id")
        manifest_value = event.payload.get("manifest")
        if not isinstance(manifest_value, Mapping):
            continue
        allocation = {
            name: manifest_value.get(name)
            for name in ("tokens_used", "token_budget", "included")
        }
        allocations[request_id] = allocation
    return MetricFact(
        allocations,
        MetricAvailability.KNOWN if allocations else MetricAvailability.UNAVAILABLE,
        "model_intended manifest keyed by request_id",
        reason=None if allocations else "no context allocation manifest was recorded",
        denominator="unique main-model request IDs",
    )


def _independent_evidence_unavailable(reason: str) -> MetricFact:
    return MetricFact(
        None,
        MetricAvailability.UNAVAILABLE,
        "no independent external or labeled evidence attached to the run",
        reason=reason,
    )


def _wall_interval_fact(events: tuple[Event, ...]) -> MetricFact:
    try:
        first = datetime.fromisoformat(events[0].recorded_at)
        last = datetime.fromisoformat(events[-1].recorded_at)
        interval = int((last - first).total_seconds() * 1000)
    except (ValueError, TypeError, OverflowError) as exc:
        return MetricFact(
            None,
            MetricAvailability.UNKNOWN,
            "first and last event recorded_at timestamps",
            reason=f"event timestamp interval is invalid: {type(exc).__name__}",
            denominator="one recorded event stream",
        )
    if interval < 0:
        return MetricFact(
            None,
            MetricAvailability.UNKNOWN,
            "first and last event recorded_at timestamps",
            reason="event timestamps are not monotonic across the stream",
            denominator="one recorded event stream",
        )
    return MetricFact(
        interval,
        MetricAvailability.KNOWN,
        "first and last event recorded_at timestamps",
        reason="recorded event interval; not guaranteed to be process wall time",
        denominator="one run",
    )


def _attempt_commit_ratio_fact(state: SessionState) -> MetricFact:
    commits = len(state.accepted_lineage) - 1
    if commits == 0:
        return MetricFact(
            None,
            MetricAvailability.UNKNOWN,
            "replay attempt history and accepted lineage",
            reason="ratio denominator is zero because the run has no accepted commit",
            denominator="accepted commits",
        )
    return MetricFact(
        len(state.attempt_history) / commits,
        MetricAvailability.KNOWN,
        "replay attempt history and accepted lineage",
        denominator="accepted commits",
    )


def _attempted_directions_fact(events: tuple[Event, ...]) -> MetricFact:
    counts: dict[str, int] = {}
    for event in events:
        if event.kind != "action_intended":
            continue
        proposal = _mapping(event.payload.get("proposal"))
        action = proposal.get("action_name")
        if not isinstance(action, str):
            action = proposal.get("action")
        if isinstance(action, str):
            counts[action] = counts.get(action, 0) + 1
    return MetricFact(
        {"intended_actions_by_name": counts, "total": sum(counts.values())},
        MetricAvailability.KNOWN,
        "action_intended proposal names",
        reason=(
            "action direction names are recorded; semantics remain "
            "environment-native"
        ),
        denominator="action_intended events",
    )


def _attempt_rate_fact(state: SessionState) -> MetricFact:
    closed = tuple(
        item for item in state.attempt_history if item.event_end_cursor is not None
    )
    dispositions: dict[str, int] = {}
    for item in closed:
        name = item.attempt.disposition.value
        dispositions[name] = dispositions.get(name, 0) + 1
    return MetricFact(
        {"counts": dispositions, "closed_attempts": len(closed)},
        MetricAvailability.KNOWN if closed else MetricAvailability.UNKNOWN,
        "replayed closed attempt dispositions",
        reason=None if closed else "no closed attempts were recorded",
        denominator="closed attempts",
    )


def _malformed_rate_fact(events: tuple[Event, ...]) -> MetricFact:
    calls = sum(event.kind in {"model_decided", "model_malformed"} for event in events)
    malformed = sum(event.kind == "model_malformed" for event in events)
    if calls == 0:
        return MetricFact(
            {"rate": None, "numerator": malformed, "denominator_count": 0},
            MetricAvailability.UNKNOWN,
            "model_malformed and main-model terminal events",
            reason="malformed-result rate has a zero model-call denominator",
            denominator="settled main-model calls",
        )
    return MetricFact(
        {
            "rate": malformed / calls,
            "numerator": malformed,
            "denominator_count": calls,
        },
        MetricAvailability.KNOWN,
        "model_malformed and main-model terminal events",
        denominator="settled main-model calls",
    )


def _repeated_action_fact(events: tuple[Event, ...]) -> MetricFact:
    seen: set[str] = set()
    repeats = 0
    intended = 0
    for event in events:
        if event.kind != "action_intended":
            continue
        intended += 1
        proposal = _mapping(event.payload.get("proposal"))
        fingerprint = canonical_json(
            {
                "action_name": proposal.get("action_name", proposal.get("action")),
                "arguments": proposal.get("arguments", {}),
            }
        )
        if fingerprint in seen:
            repeats += 1
        else:
            seen.add(fingerprint)
    return MetricFact(
        {"repeated_proposals": repeats, "intended_proposals": intended},
        MetricAvailability.KNOWN,
        "normalized action_intended action and arguments",
        reason="counts repeated proposals, not duplicate external effects",
        denominator="action_intended events",
    )


def _uncorrelated_receipt_fact(events: tuple[Event, ...]) -> MetricFact:
    receipts = tuple(event for event in events if event.kind == "receipt_received")
    uncorrelated = sum(event.correlation_id is None for event in receipts)
    return MetricFact(
        {
            "count": uncorrelated,
            "rate": (uncorrelated / len(receipts)) if receipts else None,
            "denominator_count": len(receipts),
        },
        MetricAvailability.KNOWN if receipts else MetricAvailability.UNAVAILABLE,
        "receipt_received event correlation_id",
        reason=(
            "counts missing optional event-envelope correlation IDs; receipt "
            "admission uses proposal/request identity"
            if receipts
            else "no runtime receipts were recorded"
        ),
        denominator="runtime receipts",
    )


def _model_attempt_signals(events: tuple[Event, ...]) -> dict[str, int]:
    terminals = tuple(
        event for event in events if event.kind in {"model_decided", "model_malformed"}
    )
    attempt_counts = tuple(
        len(value)
        for event in terminals
        if isinstance((value := event.payload.get("attempts")), (tuple, list))
    )
    attempts = sum(attempt_counts)
    errors = 0
    for event in terminals:
        attempt_rows = event.payload.get("attempts")
        if not isinstance(attempt_rows, (tuple, list)):
            continue
        errors += sum(
            isinstance(attempt, Mapping) and attempt.get("error") is not None
            for attempt in attempt_rows
        )
    return {
        "terminal_calls": len(terminals),
        "provider_attempts": attempts,
        "retries": sum(max(count - 1, 0) for count in attempt_counts),
        "provider_attempt_errors": errors,
        "malformed_model_results": sum(
            event.kind == "model_malformed" for event in terminals
        ),
    }


def _action_signals(events: tuple[Event, ...]) -> dict[str, int]:
    receipts = tuple(event for event in events if event.kind == "receipt_received")
    statuses = tuple(
        _mapping(event.payload.get("receipt")).get("status") for event in receipts
    )
    return {
        "intended": sum(event.kind == "action_intended" for event in events),
        "receipts": len(receipts),
        "accepted_receipts": sum(status == "accepted" for status in statuses),
        "rejected_receipts": sum(status == "rejected" for status in statuses),
        "indeterminate_receipts": sum(status == "indeterminate" for status in statuses),
    }


def _per_commit_costs(
    events: tuple[Event, ...], state: SessionState
) -> tuple[tuple[str, MetricFact], ...]:
    result: list[tuple[str, MetricFact]] = []
    for node_id, _, before, after in _promotion_ledger_deltas(events, state):
        before_ids = {
            record.measurement.measurement_id for record in before.measurements
        }
        records = tuple(
            record
            for record in after.measurements
            if record.measurement.measurement_id not in before_ids
        )
        unknown = _unknown_cost_operations(after) - _unknown_cost_operations(before)
        costs = tuple(record.measurement.cost for record in records)
        known_total = sum((cost for cost in costs if cost is not None), Decimal("0"))
        missing_cost_ids = {
            record.operation_id for record in records if record.measurement.cost is None
        }
        unknown.update(missing_cost_ids)
        if unknown:
            fact = MetricFact(
                {"known_partial": str(known_total), "unknown_operations": len(unknown)},
                MetricAvailability.UNKNOWN,
                "deduplicated replay usage-ledger measurement delta",
                reason="one or more cost measurements or reservation costs are unknown",
                denominator="one interval between accepted promotions",
            )
        else:
            fact = MetricFact(
                str(known_total),
                MetricAvailability.KNOWN,
                "deduplicated replay usage-ledger measurement delta",
                denominator="one interval between accepted promotions",
            )
        result.append((node_id, fact))
    return tuple(result)


def _unknown_cost_operations(ledger: UsageLedger) -> set[str]:
    unknown = set(ledger.unknown_cost_operation_ids)
    unknown.update(
        reservation.operation_id for reservation in ledger.active_reservations
    )
    return unknown


def _promotion_ledger_deltas(
    events: tuple[Event, ...], state: SessionState
) -> tuple[tuple[str, Event, UsageLedger, UsageLedger], ...]:
    promotions = tuple(
        (index, event)
        for index, event in enumerate(events)
        if event.kind == "lineage_promoted"
    )
    if not events:
        return ()
    previous = replay(events[:1]).ledger
    result: list[tuple[str, Event, UsageLedger, UsageLedger]] = []
    for node, (index, promotion) in zip(state.accepted_lineage[1:], promotions):
        after = replay(events[: index + 1]).ledger
        result.append((node.node_id, promotion, previous, after))
        previous = after
    return tuple(result)


def _external_outcomes_fact(vectors: tuple[ObjectiveVector, ...]) -> MetricFact:
    applicable = tuple(vector for vector in vectors if vector.valid)
    externally_rejected = sum(not vector.externally_accepted for vector in applicable)
    constraint_failed = sum(
        any(not value for value in vector.hard_constraints.values())
        for vector in applicable
    )
    return MetricFact(
        {
            "valid_evaluations": len(applicable),
            "externally_not_accepted": externally_rejected,
            "hard_constraint_failed": constraint_failed,
            "rejected_or_constraint_failed": sum(
                (not vector.externally_accepted)
                or any(not value for value in vector.hard_constraints.values())
                for vector in applicable
            ),
            "rejected_or_constraint_failed_rate": (
                sum(
                    (not vector.externally_accepted)
                    or any(not value for value in vector.hard_constraints.values())
                    for vector in applicable
                )
                / len(applicable)
                if applicable
                else None
            ),
        },
        MetricAvailability.KNOWN if applicable else MetricAvailability.UNKNOWN,
        "valid evaluation_received receipts and hard_constraints",
        reason=(
            "invalid receipts are excluded; external non-acceptance and constraint "
            "failure are diagnostic outcomes, not a universal correctness label"
            if applicable
            else "no valid external evaluation receipts were recorded"
        ),
        denominator="valid external evaluation receipts",
    )


def _between_commit_activity(
    events: tuple[Event, ...], state: SessionState
) -> MetricFact:
    rows: list[Mapping[str, JsonValue]] = []
    for node_id, promotion, previous_ledger, ledger in _promotion_ledger_deltas(
        events, state
    ):
        previous_unknown = set(previous_ledger.unknown_usage_operation_ids)
        current_unknown = set(ledger.unknown_usage_operation_ids)
        input_tokens = (
            None
            if previous_ledger.input_tokens is None or ledger.input_tokens is None
            else ledger.input_tokens - previous_ledger.input_tokens
        )
        output_tokens = (
            None
            if previous_ledger.output_tokens is None or ledger.output_tokens is None
            else ledger.output_tokens - previous_ledger.output_tokens
        )
        rows.append(
            {
                "node_id": node_id,
                "objective_version": promotion.objective_version,
                "start_event_sequence_exclusive": max(
                    (
                        item.sequence
                        for item in events
                        if item.kind == "lineage_promoted"
                        and item.sequence < promotion.sequence
                    ),
                    default=0,
                ),
                "end_event_sequence_inclusive": promotion.sequence,
                "actions": ledger.actions - previous_ledger.actions,
                "evaluations": ledger.evaluations - previous_ledger.evaluations,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "unknown_usage_operations": len(current_unknown - previous_unknown),
                "unknown_usage_exposure": bool(current_unknown - previous_unknown),
                "usage_source": (
                    "replay usage-ledger prefix deltas (main and supervisor)"
                ),
            }
        )
    return MetricFact(
        tuple(rows),
        MetricAvailability.KNOWN,
        "replay(events through promotion).ledger deltas",
        reason=(
            "token dimensions remain null where the ledger reports unknown usage; "
            "unknown operation exposure is reported per interval"
        ),
        denominator="one interval between accepted promotions",
    )


def manifest(events: Iterable[Event]) -> RunManifest:
    """Read the immutable run identity from the durable creation event."""
    stream = tuple(events)
    if not stream or stream[0].kind != "session_created":
        raise IntegrityError("run manifest requires a session creation event")
    payload = stream[0].payload
    task = payload.get("task")
    environment = payload.get("environment")
    if not isinstance(task, Mapping) or not isinstance(environment, Mapping):
        raise IntegrityError("session creation lacks manifest identity")
    identity = payload.get("run_identity")
    if not isinstance(identity, Mapping):
        raise IntegrityError("session creation lacks run identity envelope")
    return RunManifest(
        session_id=stream[0].session_id,
        task_id=_string(task, "task_id"),
        environment_id=_string(environment, "environment_id"),
        environment_version=_string(environment, "version"),
        evaluator_version=_string(task, "evaluation_contract_id"),
        runtime=_component_identity(identity, "runtime"),
        evaluator=_component_identity(identity, "evaluator"),
        package_version=_string(identity, "package_version"),
        configuration_digest=_digest(identity, "configuration_digest"),
        schema_version=stream[0].schema_version,
        envelope=_event_envelope(stream[0], "vharness.session"),
    )


def counters(events: Iterable[Event]) -> RunCounters:
    """Derive evidence counters from a validated event stream and ledger facts."""
    stream = tuple(events)
    state = replay(stream)
    return RunCounters(
        actions=state.ledger.actions,
        evaluations=state.ledger.evaluations,
        model_calls=state.ledger.model_calls,
        supervisor_calls=state.ledger.supervisor_model_calls,
        conservative_supervisor_calls=(
            state.ledger.conservative_supervisor_model_calls
        ),
        supervisor_input_tokens=state.ledger.supervisor_input_tokens,
        supervisor_output_tokens=state.ledger.supervisor_output_tokens,
        attempts=len(state.attempt_history),
        commits=len(state.accepted_lineage) - 1,
        checkpoints=sum(event.kind == "checkpoint" for event in stream),
        reconciliations=sum(event.kind == "operation_reconciled" for event in stream),
        runtime_receipts=sum(event.kind == "receipt_received" for event in stream),
        operator_commands=sum(event.kind == "command_admitted" for event in stream),
        input_tokens=state.ledger.input_tokens,
        output_tokens=state.ledger.output_tokens,
        duration_ms=state.ledger.duration_ms,
        known_cost=str(state.ledger.known_cost),
        unknown_cost_operations=len(state.ledger.unknown_cost_operation_ids),
        abandoned_attempts=sum(
            item.attempt.disposition.value == "abandoned"
            for item in state.attempt_history
        ),
        rejected_attempts=sum(
            item.attempt.disposition.value == "rejected"
            for item in state.attempt_history
        ),
        stale_attempts=_attempt_count(state, "stale"),
        incomparable_attempts=_attempt_count(state, "incomparable"),
        invalid_attempts=_attempt_count(state, "invalid"),
        hard_constraint_failed_attempts=_attempt_count(state, "hard_constraint_failed"),
        envelope=_event_envelope(stream[-1], "vharness.replay"),
    )


def _string(value: Mapping[str, object], name: str) -> str:
    candidate = value.get(name)
    if not isinstance(candidate, str):
        raise IntegrityError(f"run manifest {name} must be a string")
    return candidate


def _digest(value: Mapping[str, object], name: str) -> str:
    candidate = _string(value, name)
    if len(candidate) != 64 or any(
        char not in "0123456789abcdef" for char in candidate
    ):
        raise IntegrityError(f"run manifest {name} must be a SHA-256 digest")
    return candidate


def _component_identity(value: Mapping[str, object], name: str) -> ComponentIdentity:
    candidate = value.get(name)
    if not isinstance(candidate, Mapping):
        raise IntegrityError(f"run manifest {name} must be an identity object")
    configuration_digest = candidate.get("configuration_digest")
    if configuration_digest is not None and not isinstance(configuration_digest, str):
        raise IntegrityError(
            f"run manifest {name} configuration digest must be a string"
        )
    try:
        return ComponentIdentity(
            _string(candidate, "component_id"),
            _string(candidate, "version"),
            configuration_digest,
        )
    except ContractError as exc:
        raise IntegrityError(f"run manifest {name} identity is invalid") from exc


def _attempt_count(state: SessionState, disposition: str) -> int:
    return sum(
        item.attempt.disposition.value == disposition for item in state.attempt_history
    )


def _event_envelope(event: Event, provenance: str) -> RecordEnvelope:
    """Expose an event's durable identity through the public record envelope."""
    extensions = event.payload.get("extensions", {})
    if not isinstance(extensions, Mapping):
        raise IntegrityError("event extensions must be an object")
    try:
        return RecordEnvelope(
            record_id=event.event_id,
            session_id=event.session_id,
            objective_version=event.objective_version,
            recorded_at=event.recorded_at,
            provenance=provenance,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
            schema_version=event.schema_version,
            extensions=extensions,
        )
    except ContractError as exc:
        raise IntegrityError(f"invalid run evidence envelope: {exc}") from exc
