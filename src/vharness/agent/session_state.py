"""Validated decoding of session projection payloads at the persistence boundary."""

# One branch per immutable command variant keeps durable command decoding explicit.
# pylint: disable=too-many-return-statements

from __future__ import annotations

from collections.abc import Mapping

from decimal import Decimal, InvalidOperation
from typing import cast

from .errors import ContractError, IntegrityError
from .models import (
    ArtifactRef,
    Attempt,
    AttemptDisposition,
    BudgetLimits,
    ComponentIdentity,
    CommittedNode,
    CompletionMode,
    ExecutionReceipt,
    ExecutionStatus,
    EvaluationCommand,
    EvaluationContract,
    EvaluationDecision,
    EvaluationObjective,
    ObjectiveDirection,
    EvaluationReceipt,
    EvaluationRequest,
    JsonValue,
    CheckpointRequest,
    Message,
    OperatorCommand,
    Observation,
    Pause,
    KnowledgeSource,
    ResourceReservation,
    RecordEnvelope,
    Resume,
    StateRef,
    Steer,
    Stop,
    TaskSpec,
    UsageMeasurement,
)

_ENVELOPE_FIELDS = {
    "record_id",
    "session_id",
    "objective_version",
    "recorded_at",
    "provenance",
    "causation_id",
    "correlation_id",
    "schema_version",
    "extensions",
}


def envelope_from_payload(value: Mapping[str, object]) -> RecordEnvelope:
    """Decode a strict versioned envelope while retaining compatible extensions."""
    missing = (_ENVELOPE_FIELDS - {"extensions"}) - set(value)
    if missing:
        raise IntegrityError(f"record envelope lacks fields: {sorted(missing)}")
    schema_version = _integer(value, "schema_version")
    if schema_version != 1:
        raise IntegrityError(
            f"unsupported record envelope schema_version {schema_version}"
        )
    supplied_extensions = value.get("extensions", {})
    if not isinstance(supplied_extensions, Mapping):
        raise IntegrityError("record envelope extensions must be an object")
    extensions = cast(
        Mapping[str, JsonValue],
        {
            **supplied_extensions,
            **{
                key: item
                for key, item in value.items()
                if key not in _ENVELOPE_FIELDS and key != "__type__"
            },
        },
    )
    try:
        return RecordEnvelope(
            record_id=_string(value, "record_id"),
            session_id=_string(value, "session_id"),
            objective_version=_integer(value, "objective_version"),
            recorded_at=_string(value, "recorded_at"),
            provenance=_string(value, "provenance"),
            causation_id=_optional_string(value, "causation_id"),
            correlation_id=_optional_string(value, "correlation_id"),
            schema_version=schema_version,
            extensions=extensions,
        )
    except ContractError as exc:
        raise IntegrityError(f"invalid record envelope: {exc}") from exc


def evaluation_contract_from_payload(value: Mapping[str, object]) -> EvaluationContract:
    """Decode a versioned evaluator contract and preserve its envelope extensions."""
    evaluator = _mapping(value, "evaluator")
    required_evidence = _strings(
        value.get("required_evidence", ()), "required_evidence"
    )
    envelope = value.get("envelope")
    if not isinstance(envelope, Mapping):
        raise IntegrityError("evaluation contract envelope must be an object")
    try:
        return EvaluationContract(
            contract_id=_string(value, "contract_id"),
            evaluator=ComponentIdentity(
                _string(evaluator, "component_id"),
                _string(evaluator, "version"),
                _optional_string(evaluator, "configuration_digest"),
            ),
            envelope=envelope_from_payload(envelope),
            objectives=tuple(
                EvaluationObjective(
                    _string(_mapping_value(item, "objective"), "name"),
                    ObjectiveDirection(
                        _string(_mapping_value(item, "objective"), "direction")
                    ),
                )
                for item in _sequence(value.get("objectives", ()), "objectives")
            ),
            hard_constraints=_strings(
                value.get("hard_constraints", ()), "hard_constraints"
            ),
            acceptance_semantics=_string(value, "acceptance_semantics"),
            required_evidence=tuple(required_evidence),
            schema_version=_integer(value, "schema_version"),
        )
    except (ContractError, ValueError) as exc:
        raise IntegrityError(f"invalid evaluation contract: {exc}") from exc


def observation_from_payload(value: Mapping[str, object]) -> Observation:
    """Decode one durable initial observation."""
    envelope = value.get("envelope")
    if not isinstance(envelope, Mapping):
        raise IntegrityError("observation envelope must be an object")
    raw_record = value.get("raw_record")
    return Observation(
        _string(value, "observation_id"),
        state_ref_from(_mapping(value, "state")),
        _string(value, "observed_at"),
        _string(value, "provenance"),
        envelope_from_payload(envelope),
        (
            None
            if raw_record is None
            else artifact_ref_from(_mapping_value(raw_record, "raw_record"))
        ),
        _integer(value, "schema_version"),
    )


def knowledge_source_from_payload(value: Mapping[str, object]) -> KnowledgeSource:
    """Decode one durable knowledge source."""
    envelope = value.get("envelope")
    if not isinstance(envelope, Mapping):
        raise IntegrityError("knowledge source envelope must be an object")
    artifact = value.get("artifact")
    return KnowledgeSource(
        _string(value, "source_id"),
        _string(value, "provenance"),
        _string(value, "observed_at"),
        envelope_from_payload(envelope),
        (
            None
            if artifact is None
            else artifact_ref_from(_mapping_value(artifact, "artifact"))
        ),
        _integer(value, "schema_version"),
    )


def task_from_payload(value: Mapping[str, object]) -> TaskSpec:
    """Decode the task fact carried by session creation."""
    budgets = _mapping(value, "budgets")
    return TaskSpec(
        _string(value, "task_id"),
        _string(value, "objective"),
        CompletionMode(_string(value, "completion_mode")),
        _string(value, "success_evidence"),
        BudgetLimits(
            actions=_optional_int(budgets, "actions"),
            model_calls=_optional_int(budgets, "model_calls"),
            evaluations=_optional_int(budgets, "evaluations"),
            cost=_optional_decimal(budgets, "cost"),
        ),
        _string(value, "environment_id"),
        _string(value, "evaluation_contract_id"),
        _integer(value, "objective_version"),
        tuple(_strings(value.get("constraints", []), "constraints")),
    )


def reservation_from_payload(value: Mapping[str, object]) -> ResourceReservation:
    """Decode a durable operation reservation."""
    return ResourceReservation(
        _string(value, "reservation_id"),
        _string(value, "operation_id"),
        _integer(value, "objective_version"),
        _integer(value, "actions"),
        _integer(value, "evaluations"),
        _optional_decimal(value, "cost"),
        _integer_default(value, "model_calls", 0),
    )


def receipt_from_payload(value: Mapping[str, object]) -> ExecutionReceipt:
    """Decode one durable runtime receipt."""
    state = value.get("state")
    usage = value.get("usage")
    raw_artifact = value.get("raw_artifact")
    return ExecutionReceipt(
        _string(value, "receipt_id"),
        _string(value, "proposal_id"),
        _integer(value, "objective_version"),
        ExecutionStatus(_string(value, "status")),
        _optional_string(value, "operation_id"),
        None if state is None else state_ref_from(_mapping_value(state, "state")),
        _optional_string(value, "error"),
        None if usage is None else usage_from_payload(_mapping_value(usage, "usage")),
        (
            None
            if raw_artifact is None
            else artifact_ref_from(_mapping_value(raw_artifact, "raw_artifact"))
        ),
    )


def evaluation_request_from_payload(value: Mapping[str, object]) -> EvaluationRequest:
    """Decode one durable evaluation request."""
    return EvaluationRequest(
        _string(value, "request_id"),
        _string(value, "session_id"),
        _integer(value, "objective_version"),
        _string(value, "attempt_id"),
        state_ref_from(_mapping(value, "evaluated_state")),
        _string(value, "baseline_node_id"),
        _string(value, "evaluator_version"),
    )


def evaluation_receipt_from_payload(value: Mapping[str, object]) -> EvaluationReceipt:
    """Decode one verified durable evaluation receipt."""
    evidence = value.get("evidence", ())
    if not isinstance(evidence, (list, tuple)):
        raise IntegrityError("event payload evidence must be an array")
    accepted = value.get("accepted")
    if not isinstance(accepted, bool):
        raise IntegrityError("event payload accepted must be a boolean")
    hard_constraints = _boolean_mapping(value, "hard_constraints")
    valid = value.get("valid", True)
    completed = value.get("completed", False)
    if not isinstance(valid, bool) or not isinstance(completed, bool):
        raise IntegrityError(
            "event payload evaluation validity and completion must be booleans"
        )
    completion_evidence = value.get("completion_evidence", ())
    required_evidence = value.get("required_evidence", ())
    if not isinstance(completion_evidence, (list, tuple)):
        raise IntegrityError("event payload completion_evidence must be an array")
    if not isinstance(required_evidence, (list, tuple)) or not all(
        isinstance(item, str) for item in required_evidence
    ):
        raise IntegrityError(
            "event payload required_evidence must be an array of strings"
        )
    usage = value.get("usage")
    raw_receipt = value.get("raw_receipt")
    return EvaluationReceipt(
        _string(value, "receipt_id"),
        _string(value, "request_id"),
        _integer(value, "objective_version"),
        state_ref_from(_mapping(value, "evaluated_state")),
        _string(value, "baseline_node_id"),
        _string(value, "evaluator_version"),
        accepted,
        _string(value, "comparison"),
        cast(Mapping[str, JsonValue], _mapping_or_empty(value, "objectives")),
        tuple(artifact_ref_from(_mapping_value(item, "evidence")) for item in evidence),
        valid,
        hard_constraints,
        completed,
        tuple(
            artifact_ref_from(_mapping_value(item, "completion_evidence"))
            for item in completion_evidence
        ),
        tuple(required_evidence),
        None if usage is None else usage_from_payload(_mapping_value(usage, "usage")),
        (
            None
            if raw_receipt is None
            else artifact_ref_from(_mapping_value(raw_receipt, "raw_receipt"))
        ),
    )


def artifact_ref_from(value: Mapping[str, object]) -> ArtifactRef:
    """Decode a verified artifact reference carried by an evaluation receipt."""
    return ArtifactRef(
        _string(value, "digest"),
        _integer(value, "size"),
        _string(value, "media_type"),
        _string(value, "provenance"),
    )


def state_ref_from(value: Mapping[str, object]) -> StateRef:
    """Decode an opaque external state reference."""
    restorable = value.get("restorable", False)
    if not isinstance(restorable, bool):
        raise IntegrityError("event payload restorable must be a boolean")
    return StateRef(
        _string(value, "owner"),
        _string(value, "value"),
        _optional_string(value, "digest"),
        _optional_string(value, "revision"),
        restorable,
    )


def usage_from_payload(value: Mapping[str, object]) -> UsageMeasurement:
    """Decode measured external resource consumption."""
    return UsageMeasurement(
        measurement_id=_string(value, "measurement_id"),
        actions=_optional_int(value, "actions"),
        model_calls=_optional_int(value, "model_calls"),
        evaluations=_optional_int(value, "evaluations"),
        input_tokens=_optional_int(value, "input_tokens"),
        output_tokens=_optional_int(value, "output_tokens"),
        duration_ms=_optional_int(value, "duration_ms"),
        cost=_optional_decimal(value, "cost"),
    )


def command_from_payload(value: Mapping[str, object]) -> OperatorCommand:
    """Decode an admitted command while rebuilding the durable inbox."""
    command_id = _string(value, "command_id")
    session_id = _string(value, "session_id")
    command_type = _string(value, "__type__") if "__type__" in value else None
    if command_type == "Message":
        return Message(
            command_id,
            session_id,
            _string(value, "text"),
            _optional_question_id(value),
        )
    if command_type == "Steer":
        return Steer(
            command_id,
            session_id,
            _string(value, "objective"),
            _string(value, "success_evidence"),
        )
    if command_type == "Pause":
        return Pause(command_id, session_id)
    if command_type == "Resume":
        return Resume(command_id, session_id)
    if command_type == "Stop":
        return Stop(command_id, session_id)
    if command_type == "CheckpointRequest":
        return CheckpointRequest(command_id, session_id)
    if command_type == "EvaluationCommand":
        return EvaluationCommand(
            command_id, session_id, state_ref_from(_mapping(value, "state"))
        )
    if command_type == "EvaluationDecision":
        evidence = value.get("evidence_ref")
        return EvaluationDecision(
            command_id,
            session_id,
            _string(value, "request_id"),
            _boolean(value, "accepted"),
            _string(value, "reason"),
            _boolean_default(value, "completed", False),
            (
                None
                if evidence is None
                else artifact_ref_from(_mapping_value(evidence, "evidence_ref"))
            ),
        )
    raise IntegrityError("admitted command has an unsupported type")


def string_from_payload(value: Mapping[str, object], name: str) -> str:
    """Decode a required string from a durable event payload."""
    return _string(value, name)


def integer_from_payload(value: Mapping[str, object], name: str) -> int:
    """Decode a required integer from a durable event payload."""
    return _integer(value, name)


def node_from_payload(value: Mapping[str, object]) -> CommittedNode:
    """Decode a committed lineage node from its durable event payload."""
    return CommittedNode(
        _string(value, "node_id"),
        _integer(value, "objective_version"),
        state_ref_from(_mapping(value, "state")),
        _optional_string(value, "parent_id"),
        _optional_string(value, "evaluation_receipt_id"),
        cast(Mapping[str, JsonValue], _mapping_or_empty(value, "objectives")),
    )


def attempt_from_payload(value: Mapping[str, object]) -> Attempt:
    """Decode an attempt fact from its durable event payload."""
    result = value.get("result_state")
    return Attempt(
        _string(value, "attempt_id"),
        _integer(value, "objective_version"),
        _string(value, "base_node_id"),
        state_ref_from(_mapping(value, "starting_state")),
        AttemptDisposition(_string(value, "disposition")),
        (
            None
            if result is None
            else state_ref_from(_mapping_value(result, "result_state"))
        ),
    )


def _mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Extract one required JSON object."""
    return _mapping_value(value.get(name), name)


def _mapping_value(value: object, name: str) -> Mapping[str, object]:
    """Validate a JSON object from an untyped persisted payload."""
    if not isinstance(value, Mapping):
        raise IntegrityError(f"event payload {name} must be an object")
    return value


def _mapping_or_empty(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    """Extract an optional JSON object using an empty default."""
    return _mapping_value(value.get(name, {}), name)


def _boolean(value: Mapping[str, object], name: str) -> bool:
    """Decode a required boolean without coercion."""
    candidate = value.get(name)
    if not isinstance(candidate, bool):
        raise IntegrityError(f"event payload {name} must be a boolean")
    return candidate


def _boolean_default(value: Mapping[str, object], name: str, default: bool) -> bool:
    """Decode an optional boolean, applying a default for an absent field."""
    if value.get(name) is None:
        return default
    return _boolean(value, name)


def _boolean_mapping(value: Mapping[str, object], name: str) -> Mapping[str, bool]:
    """Decode a string-keyed boolean map without coercion."""
    candidate = _mapping_or_empty(value, name)
    if not all(isinstance(item, bool) for item in candidate.values()):
        raise IntegrityError(f"event payload {name} values must be booleans")
    return {key: cast(bool, item) for key, item in candidate.items()}


def _string(value: Mapping[str, object], name: str) -> str:
    """Extract a required string without coercion."""
    candidate = value.get(name)
    if not isinstance(candidate, str):
        raise IntegrityError(f"event payload {name} must be a string")
    return candidate


def _optional_string(value: Mapping[str, object], name: str) -> str | None:
    candidate = value.get(name)
    if candidate is not None and not isinstance(candidate, str):
        raise IntegrityError(f"event payload {name} must be a string or null")
    return candidate


def _optional_question_id(value: Mapping[str, object]) -> str | None:
    raw = value.get("question_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise IntegrityError("message question_id must be a non-empty string or null")
    return raw


def _integer(value: Mapping[str, object], name: str) -> int:
    candidate = value.get(name)
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise IntegrityError(f"event payload {name} must be an integer")
    return candidate


def _integer_default(value: Mapping[str, object], name: str, default: int) -> int:
    if value.get(name) is None:
        return default
    return _integer(value, name)


def _optional_int(value: Mapping[str, object], name: str) -> int | None:
    candidate = value.get(name)
    return None if candidate is None else _integer(value, name)


def _decimal(value: Mapping[str, object], name: str) -> Decimal:
    return _parse_decimal(_string(value, name), name)


def _optional_decimal(value: Mapping[str, object], name: str) -> Decimal | None:
    candidate = value.get(name)
    return None if candidate is None else _parse_decimal(_string(value, name), name)


def _parse_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise IntegrityError(f"event payload {name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise IntegrityError(f"event payload {name} must be a finite decimal string")
    return parsed


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise IntegrityError(f"event payload {name} must be an array of strings")
    return tuple(value)


def _sequence(value: object, name: str) -> tuple[object, ...]:
    if not isinstance(value, (tuple, list)):
        raise IntegrityError(f"event payload {name} must be an array")
    return tuple(value)
