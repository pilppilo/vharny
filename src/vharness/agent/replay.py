"""Validated, pure reconstruction of durable session projections."""

# The event-kind reducer intentionally contains the complete phase-one state
# machine. Splitting branches would obscure the single authoritative reducer.
# pylint: disable=too-many-branches,too-many-locals,too-many-return-statements,too-many-statements
# pylint: disable=too-many-lines

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Iterable, Mapping

from .codec import canonical_json, parse_json_object
from .errors import ContractError, IntegrityError, TransitionError
from .models import (
    AttemptDisposition,
    Event,
    OPERATOR_ANSWER_WAIT,
    OPERATOR_EVALUATION_WAIT,
    PromotionDisposition,
    SessionStatus,
    SUPERVISOR_REQUEST_PREFIX,
    EvaluationDecision,
    OperatorCommand,
    StateRef,
)
from .session_state import (
    attempt_from_payload,
    command_from_payload,
    evaluation_contract_from_payload,
    evaluation_receipt_from_payload,
    evaluation_request_from_payload,
    node_from_payload,
    knowledge_source_from_payload,
    observation_from_payload,
    receipt_from_payload,
    reservation_from_payload,
    state_ref_from,
    string_from_payload,
    task_from_payload,
    integer_from_payload,
    usage_from_payload,
)
from .transitions import (
    OperationKind,
    ReconciliationStatus,
    SessionState,
    abandon_attempt,
    apply_steer,
    admit_command,
    resume_session,
    consume_evaluation,
    evaluation_operation,
    EvaluationOperationStatus,
    operator_evaluation_receipt,
    fail_evaluation_verification,
    intend_action,
    intend_evaluation,
    intend_model_call,
    model_operation,
    promote,
    promotion_disposition,
    provider_recovery_waiting,
    record_evaluation_intake,
    record_evaluation_usage,
    record_command_transition,
    receive_action_receipt,
    reconcile_action,
    reconcile_evaluation,
    start_attempt,
    settle_command,
    settle_model_call,
)
from .supervision import (
    BURN_CAUSE,
    CHECKPOINT_CAUSE,
    TRIGGER_CAUSES,
    GuidanceDecision,
    guidance_from_payload,
    policy_from_payload,
)


def reduce_event(previous: SessionState | None, event: Event) -> SessionState:
    """Apply one typed durable fact without I/O or a stored projection snapshot."""
    if "snapshot" in event.payload:
        raise IntegrityError(
            "legacy snapshot journal is unsupported; create a new session journal"
        )
    if previous is None:
        return _create_session(event)
    if (
        event.objective_version < previous.task.objective_version
        and event.kind not in _HISTORICAL_EVIDENCE_KINDS
    ):
        raise IntegrityError("event objective version regresses the projection")
    if (
        event.objective_version > previous.task.objective_version
        and event.kind != "objective_steered"
    ):
        raise IntegrityError("only objective_steered may advance objective version")
    return _reduce_existing(previous, event)


def _create_session(event: Event) -> SessionState:
    if event.kind != "session_created":
        raise IntegrityError("the first event must create the session")
    measurement_scope = event.payload.get("operational_measurement_scope")
    if measurement_scope is not None and measurement_scope != (
        "session_context_and_evaluation_successful_reads_v1"
    ):
        raise IntegrityError("session_created operational measurement scope is invalid")
    task = task_from_payload(_mapping(event.payload, "task"))
    root = node_from_payload(_mapping(event.payload, "root_node"))
    observation = observation_from_payload(
        _mapping(event.payload, "initial_observation")
    )
    contract = evaluation_contract_from_payload(
        _mapping(event.payload, "evaluation_contract")
    )
    sources = tuple(
        knowledge_source_from_payload(_mapping_value(item, "knowledge_source"))
        for item in _sequence(event.payload, "knowledge_sources")
    )
    if contract.contract_id != task.evaluation_contract_id:
        raise IntegrityError("evaluation contract does not match task")
    _validate_creation_inputs(event, observation, sources, contract)
    if event.objective_version != task.objective_version:
        raise IntegrityError("session_created objective version disagrees with task")
    if root.objective_version != task.objective_version or root.parent_id is not None:
        raise IntegrityError("session_created root node is invalid")
    return SessionState(
        task=task,
        status=SessionStatus.CREATED,
        lineage_head=root,
        initial_observation=observation,
        knowledge_sources=sources,
        evaluation_contract=contract,
        accepted_lineage=(root,),
    )


def _reduce_existing(previous: SessionState, event: Event) -> SessionState:
    payload = event.payload
    if event.kind == "command_admitted":
        command = command_from_payload(_mapping(payload, "command"))
        if command.session_id != event.session_id:
            raise IntegrityError("admitted command belongs to another session")
        try:
            return admit_command(previous, command)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "operator_message":
        command = command_from_payload(_mapping(payload, "command"))
        _command_causation(event, _mapping(payload, "command"))
        return _with_command_transition(previous, event, embedded_command=command)
    if event.kind in {"command_applied", "command_rejected"}:
        command_id = string_from_payload(payload, "command_id")
        if event.causation_id != command_id:
            raise IntegrityError("command disposition has invalid causation")
        state = previous
        transition_kind = _optional_string(payload, "transition_kind")
        if transition_kind is not None:
            if event.kind != "command_applied":
                raise IntegrityError("only an applied command binds a transition")
            if transition_kind != "evaluation_received":
                raise IntegrityError(
                    "only an equivalent evaluation decision binds a transition"
                )
            decision = _admitted_decision(state, command_id)
            try:
                operation = evaluation_operation(state, decision.request_id)
            except TransitionError as exc:
                raise IntegrityError(str(exc)) from exc
            if operation.status is not EvaluationOperationStatus.RECEIPT_CONSUMED:
                raise IntegrityError(
                    "evaluation decision binding requires a consumed receipt"
                )
            if (
                operation.receipt_id
                != operator_evaluation_receipt(operation.request, decision).receipt_id
            ):
                raise IntegrityError(
                    "evaluation decision binding does not match its consumed receipt"
                )
            try:
                state = record_command_transition(state, command_id, transition_kind)
            except TransitionError as exc:
                raise IntegrityError(str(exc)) from exc
        try:
            return settle_command(
                state,
                command_id,
                "applied" if event.kind == "command_applied" else "rejected",
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "attempt_started":
        attempt = attempt_from_payload(_mapping(payload, "attempt"))
        _same_objective(event, previous)
        try:
            state = start_attempt(previous, attempt.attempt_id, event.sequence)
        except TransitionError as exc:
            raise IntegrityError("attempt_started is not a legal transition") from exc
        if state.active_attempt != attempt:
            raise IntegrityError("attempt_started payload is not the derived attempt")
        return state
    if event.kind == "action_intended":
        _exact_keys(payload, {"proposal", "reservation"})
        proposal = _mapping(payload, "proposal")
        reservation = reservation_from_payload(_mapping(payload, "reservation"))
        _same_objective(event, previous)
        active = previous.active_attempt
        if (
            active is None
            or string_from_payload(proposal, "attempt_id") != active.attempt_id
        ):
            raise IntegrityError(
                "action_intended does not belong to the active attempt"
            )
        proposal_id = string_from_payload(proposal, "proposal_id")
        requires_revision = proposal.get("requires_revision", False)
        if not isinstance(requires_revision, bool):
            raise IntegrityError("action_intended requires_revision is invalid")
        expected_revision = state_ref_from(
            _mapping(proposal, "expected_state")
        ).revision
        if (
            reservation.operation_id != proposal_id
            or reservation.objective_version != event.objective_version
        ):
            raise IntegrityError("action_intended operation identity is invalid")
        try:
            return intend_action(
                previous,
                proposal_id,
                active.attempt_id,
                reservation,
                expected_revision=expected_revision,
                requires_revision=requires_revision,
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "receipt_received":
        if not {"receipt"} <= set(payload) <= {"receipt", "evidence_fingerprints"}:
            raise IntegrityError("event payload has unexpected or missing fields")
        receipt = receipt_from_payload(_mapping(payload, "receipt"))
        evidence = payload.get("evidence_fingerprints")
        if evidence is not None and (
            not isinstance(evidence, (list, tuple))
            or not all(isinstance(item, str) for item in evidence)
        ):
            raise IntegrityError("receipt_received evidence_fingerprints are invalid")
        try:
            return receive_action_receipt(previous, receipt)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "evaluation_intended":
        if (
            not {"request", "reservation"}
            <= set(payload)
            <= {
                "request",
                "reservation",
                "wait_reason",
            }
        ):
            raise IntegrityError("evaluation_intended payload keys are invalid")
        request = evaluation_request_from_payload(_mapping(payload, "request"))
        reservation = reservation_from_payload(_mapping(payload, "reservation"))
        wait_reason = _optional_string(payload, "wait_reason") or "evaluation pending"
        if wait_reason not in ("evaluation pending", OPERATOR_EVALUATION_WAIT):
            raise IntegrityError("evaluation_intended wait reason is invalid")
        _same_objective(event, previous)
        if (
            previous.active_attempt is None
            or request.attempt_id != previous.active_attempt.attempt_id
        ):
            raise IntegrityError(
                "evaluation_intended does not belong to the active attempt"
            )
        if reservation.operation_id != request.request_id:
            raise IntegrityError("evaluation_intended operation identity is invalid")
        try:
            state = intend_evaluation(
                previous, request, reservation, wait_reason=wait_reason
            )
        except TransitionError as exc:
            raise IntegrityError(
                "evaluation_intended exceeds the durable budget"
            ) from exc
        return _with_command_transition(
            state, event, evaluated_state=request.evaluated_state
        )
    if event.kind == "model_intended":
        _same_objective(event, previous)
        reservation = reservation_from_payload(_mapping(payload, "reservation"))
        request_id = string_from_payload(payload, "request_id")
        input_text = payload.get("input")
        if input_text is not None and not isinstance(input_text, str):
            raise IntegrityError("model_intended input must be a string")
        manifest = payload.get("manifest")
        if manifest is not None and not isinstance(manifest, Mapping):
            raise IntegrityError("model_intended manifest must be an object")
        policy_version = _optional_integer(payload, "policy_version")
        token_counter = _optional_string(payload, "token_counter")
        model_id = _optional_string(payload, "model_id")
        cursor = _optional_integer(payload, "cursor")
        if manifest is None:
            # Older interrupted intents did not persist the full identity set;
            # retain their replay compatibility without claiming exact checks.
            input_text = None
            policy_version = None
            token_counter = None
            model_id = None
            cursor = None
        try:
            return intend_model_call(
                previous,
                reservation,
                request_id=request_id,
                input_text=input_text,
                manifest=manifest,
                policy_version=policy_version,
                token_counter=token_counter,
                model_id=model_id,
                cursor=cursor,
            )
        except TransitionError as exc:
            raise IntegrityError("model_intended exceeds the durable budget") from exc
    if event.kind == "model_decided":
        _same_objective(event, previous)
        _validate_model_response_identity(payload)
        return _settle_model_event(previous, payload)
    if event.kind == "model_malformed":
        _same_objective(event, previous)
        return _settle_model_event(previous, payload)
    if event.kind == "model_repaired":
        # ADR-0015: the repair note annotates one model terminal; it changes
        # no projection and is validated with the same strictness.
        _same_objective(event, previous)
        _exact_keys(
            payload,
            {"request_id", "failure_class", "result_kind", "field", "marker"},
        )
        for name in ("request_id", "failure_class", "result_kind", "field", "marker"):
            value = string_from_payload(payload, name)
            if not value.strip():
                raise IntegrityError(f"model_repaired {name} must be non-empty")
        return previous
    if event.kind == "token_budget_breach":
        # ADR-0016: provider-reported input plus reserved output exceeded the
        # declared window. The note annotates one model terminal; it changes no
        # projection and is validated with the same strictness.
        _same_objective(event, previous)
        _exact_keys(
            payload,
            {
                "request_id",
                "estimated_input_tokens",
                "provider_input_tokens",
                "reserved_output_tokens",
                "token_budget",
            },
        )
        string_from_payload(payload, "request_id")
        for name in (
            "estimated_input_tokens",
            "provider_input_tokens",
            "reserved_output_tokens",
        ):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntegrityError(
                    f"token_budget_breach {name} must be a non-negative integer"
                )
        budget = payload.get("token_budget")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise IntegrityError(
                "token_budget_breach token_budget must be a positive integer"
            )
        return previous
    if event.kind == "model_result_suppressed":
        _same_objective(event, previous)
        _exact_keys(payload, {"request_id", "reason"})
        string_from_payload(payload, "request_id")
        reason = string_from_payload(payload, "reason")
        if not reason.strip():
            raise IntegrityError("model_result_suppressed reason must be non-empty")
        return previous
    if event.kind == "model_application_rejected":
        _same_objective(event, previous)
        _exact_keys(payload, {"request_id", "reason"})
        request_id = string_from_payload(payload, "request_id")
        reason = string_from_payload(payload, "reason")
        if not reason.strip():
            raise IntegrityError("model_application_rejected reason must be non-empty")
        try:
            operation = model_operation(previous, request_id)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
        if not operation.settled:
            raise IntegrityError(
                "model_application_rejected requires a settled model terminal"
            )
        return previous
    if event.kind == "provider_recovery_waiting":
        _same_objective(event, previous)
        _exact_keys(
            payload,
            {
                "request_id",
                "failure_class",
                "consumed_calls",
                "operator_action",
                "detail",
            },
        )
        consumed = payload.get("consumed_calls")
        if isinstance(consumed, bool) or not isinstance(consumed, int) or consumed < 1:
            raise IntegrityError(
                "provider_recovery_waiting consumed_calls must be a positive integer"
            )
        if not isinstance(payload.get("operator_action"), bool):
            raise IntegrityError(
                "provider_recovery_waiting operator_action must be a boolean"
            )
        try:
            return provider_recovery_waiting(
                previous,
                request_id=string_from_payload(payload, "request_id"),
                failure_class=string_from_payload(payload, "failure_class"),
                consumed_calls=consumed,
                operator_action=bool(payload["operator_action"]),
                detail=string_from_payload(payload, "detail"),
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "supervision_enabled":
        _same_objective(event, previous)
        try:
            policy_from_payload(_mapping(payload, "policy"))
        except ContractError as exc:
            raise IntegrityError(str(exc)) from exc
        return previous
    if event.kind == "supervision_triggered":
        _same_objective(event, previous)
        _validate_supervision_triggered(payload)
        return previous
    if event.kind == "checkpoint_suppressed":
        _same_objective(event, previous)
        _validate_checkpoint_suppressed(payload)
        return previous
    if event.kind == "supervisor_intended":
        _same_objective(event, previous)
        reservation = reservation_from_payload(_mapping(payload, "reservation"))
        request_id = string_from_payload(payload, "request_id")
        if not request_id.startswith(SUPERVISOR_REQUEST_PREFIX):
            raise IntegrityError(
                "supervisor_intended request ID must be supervisor-scoped"
            )
        input_text = string_from_payload(payload, "input")
        cursor = _optional_integer(payload, "cursor")
        try:
            return intend_model_call(
                previous,
                reservation,
                request_id=request_id,
                input_text=input_text,
                cursor=cursor,
            )
        except TransitionError as exc:
            raise IntegrityError(
                "supervisor_intended exceeds the durable budget"
            ) from exc
    if event.kind == "guidance_received":
        _same_objective(event, previous)
        guidance = payload.get("guidance")
        if not isinstance(guidance, Mapping):
            raise IntegrityError("guidance_received payload lacks guidance")
        try:
            guidance_from_payload(guidance)
        except ContractError as exc:
            raise IntegrityError(str(exc)) from exc
        return _settle_model_event(previous, payload)
    if event.kind == "supervisor_failed":
        _same_objective(event, previous)
        string_from_payload(payload, "error")
        if payload.get("request_id") is None:
            # The consult never reserved (for example an exhausted budget).
            return previous
        return _settle_model_event(previous, payload)
    if event.kind in {"guidance_accepted", "guidance_rejected"}:
        _same_objective(event, previous)
        _validated_guidance_decision(payload)
        string_from_payload(payload, "reason")
        return previous
    if event.kind == "guidance_response_rejected":
        _same_objective(event, previous)
        _validated_guidance_decision(payload)
        string_from_payload(payload, "reason")
        string_from_payload(payload, "error")
        return previous
    if event.kind == "guidance_expired":
        _same_objective(event, previous)
        expiry = payload.get("expires_after_call")
        if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < 1:
            raise IntegrityError("guidance_expired expiry must be positive")
        string_from_payload(payload, "reason")
        return previous
    if event.kind == "guidance_suppressed":
        _same_objective(event, previous)
        string_from_payload(payload, "reason")
        return previous
    if event.kind == "operator_escalated":
        _same_objective(event, previous)
        _validate_operator_escalated(payload)
        return previous
    if event.kind == "attempt_abandoned":
        _same_objective(event, previous)
        attempt_id = string_from_payload(payload, "attempt_id")
        string_from_payload(payload, "reason")
        if (
            previous.active_attempt is None
            or previous.active_attempt.attempt_id != attempt_id
        ):
            raise IntegrityError("attempt_abandoned does not match the active attempt")
        try:
            return abandon_attempt(previous, event.sequence)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "artifact_eviction_intended":
        _same_objective(event, previous)
        _validate_artifact_eviction_intended(payload)
        return previous
    if event.kind == "artifact_evicted":
        _same_objective(event, previous)
        _validate_artifact_evicted(payload)
        return previous
    if event.kind == "artifact_read":
        _same_objective(event, previous)
        _validate_artifact_read(payload)
        return previous
    if event.kind == "evaluation_intake":
        _exact_keys(payload, {"request_id", "receipt_id", "raw_receipt"})
        request_id = string_from_payload(payload, "request_id")
        try:
            operation = evaluation_operation(previous, request_id)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
        raw_receipt = string_from_payload(payload, "raw_receipt")
        receipt = evaluation_receipt_from_payload(parse_json_object(raw_receipt))
        if raw_receipt != canonical_json(receipt):
            raise IntegrityError("evaluation intake raw receipt is not canonical")
        if (
            receipt.request_id != request_id
            or receipt.receipt_id != string_from_payload(payload, "receipt_id")
        ):
            raise IntegrityError("evaluation intake receipt identity is invalid")
        try:
            return record_evaluation_intake(
                previous, request_id, receipt.receipt_id, raw_receipt
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "evaluation_verification_failed":
        _exact_keys(payload, {"request_id", "receipt_id", "reason"})
        try:
            request_id = string_from_payload(payload, "request_id")
            operation = evaluation_operation(previous, request_id)
            if operation.raw_receipt is None:
                raise IntegrityError("evaluation verification failure lacks intake")
            receipt = evaluation_receipt_from_payload(
                parse_json_object(operation.raw_receipt)
            )
            return fail_evaluation_verification(
                previous,
                request_id,
                string_from_payload(payload, "receipt_id"),
                receipt.usage,
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "evaluation_received":
        _exact_keys(payload, {"receipt", "disposition"})
        receipt = evaluation_receipt_from_payload(_mapping(payload, "receipt"))
        try:
            operation = evaluation_operation(previous, receipt.request_id)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
        request, reservation = operation.request, operation.reservation
        if receipt.request_id != reservation.operation_id:
            raise IntegrityError("evaluation receipt does not link to its operation")
        if receipt.objective_version != reservation.objective_version:
            raise IntegrityError(
                "evaluation receipt objective version disagrees with operation"
            )
        if (
            operation.receipt_id != receipt.receipt_id
            or operation.raw_receipt != canonical_json(receipt)
        ):
            raise IntegrityError(
                "evaluation receipt was not durably verified at intake"
            )
        disposition = promotion_disposition(previous, request, receipt)
        if _promotion_disposition(payload) is not disposition:
            raise IntegrityError("evaluation disposition is not derived from its facts")
        try:
            consumed = consume_evaluation(
                previous, receipt, disposition, event.sequence
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
        if event.causation_id is not None:
            decision = next(
                (
                    item.command
                    for item in previous.command_operations
                    if item.command.command_id == event.causation_id
                ),
                None,
            )
            if not isinstance(decision, EvaluationDecision):
                raise IntegrityError(
                    "evaluation_received causation is not an evaluation decision"
                )
            if decision.request_id != receipt.request_id:
                raise IntegrityError(
                    "evaluation_received causation decision answers another request"
                )
        return _with_command_transition(consumed, event)
    if event.kind == "evaluation_usage_received":
        _exact_keys(payload, {"request_id", "usage"})
        request_id = string_from_payload(payload, "request_id")
        try:
            return record_evaluation_usage(
                previous,
                request_id,
                usage_from_payload(_mapping(payload, "usage")),
            )
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "lineage_promoted":
        _exact_keys(payload, {"node", "evaluation_receipt_id"})
        node = node_from_payload(_mapping(payload, "node"))
        _same_objective(event, previous)
        evaluation = previous.latest_evaluation
        if evaluation is None or evaluation[2] is not PromotionDisposition.ACCEPTED:
            raise IntegrityError(
                "lineage_promoted lacks a matching accepted evaluation"
            )
        request, receipt, _disposition = evaluation
        if string_from_payload(payload, "evaluation_receipt_id") != receipt.receipt_id:
            raise IntegrityError("lineage_promoted receipt identity is invalid")
        try:
            derived = promote(previous, request, receipt, node.node_id, event.sequence)
        except TransitionError as exc:
            raise IntegrityError("lineage_promoted is not applicable") from exc
        if derived.lineage_head != node:
            raise IntegrityError("lineage_promoted node is not derived from evaluation")
        return derived
    if event.kind == "objective_steered":
        command = _mapping(payload, "command")
        _command_causation(event, command)
        state = apply_steer(
            previous,
            string_from_payload(command, "objective"),
            string_from_payload(command, "success_evidence"),
            event.sequence,
        )
        if event.objective_version != state.task.objective_version:
            raise IntegrityError(
                "objective_steered must advance objective version once"
            )
        return _with_command_transition(
            state,
            event,
            embedded_command=command_from_payload(command),
        )
    if event.kind == "session_paused":
        command = _mapping(payload, "command")
        _command_causation(event, command)
        _same_objective(event, previous)
        if previous.status not in {
            SessionStatus.CREATED,
            SessionStatus.RUNNING,
            SessionStatus.WAITING,
        }:
            raise IntegrityError("session_paused is not a legal command transition")
        return _with_command_transition(
            replace(previous, status=SessionStatus.PAUSED),
            event,
            embedded_command=command_from_payload(command),
        )
    if event.kind == "session_resumed":
        command = _mapping(payload, "command")
        _command_causation(event, command)
        _same_objective(event, previous)
        try:
            resumed = resume_session(previous)
        except TransitionError as exc:
            raise IntegrityError(
                "session_resumed is not a legal command transition"
            ) from exc
        return _with_command_transition(
            resumed,
            event,
            embedded_command=command_from_payload(command),
        )
    if event.kind == "session_stopped":
        command = _mapping(payload, "command")
        _command_causation(event, command)
        _same_objective(event, previous)
        if previous.status in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.STOPPED,
        }:
            raise IntegrityError("session_stopped is not a legal command transition")
        return _with_command_transition(
            replace(previous, status=SessionStatus.STOPPED),
            event,
            embedded_command=command_from_payload(command),
        )
    if event.kind == "operation_reconciled":
        _same_objective(event, previous)
        _exact_keys(payload, {"operation_id", "operation_kind", "result", "receipt"})
        operation_id = string_from_payload(payload, "operation_id")
        try:
            operation_kind = OperationKind(
                string_from_payload(payload, "operation_kind")
            )
            result = ReconciliationStatus(string_from_payload(payload, "result"))
        except ValueError as exc:
            raise IntegrityError(
                "operation reconciliation has an invalid kind"
            ) from exc
        receipt_payload = payload["receipt"]
        if operation_kind is OperationKind.ACTION:
            receipt = (
                None
                if receipt_payload is None
                else receipt_from_payload(_mapping_value(receipt_payload, "receipt"))
            )
            if receipt is not None and receipt.proposal_id != operation_id:
                raise IntegrityError("reconciliation receipt does not match action")
            try:
                return reconcile_action(previous, operation_id, result, receipt)
            except TransitionError as exc:
                raise IntegrityError(str(exc)) from exc
        receipt = (
            None
            if receipt_payload is None
            else evaluation_receipt_from_payload(
                _mapping_value(receipt_payload, "receipt")
            )
        )
        if receipt is not None and receipt.request_id != operation_id:
            raise IntegrityError("reconciliation receipt does not match evaluation")
        try:
            return reconcile_evaluation(previous, operation_id, result, receipt)
        except TransitionError as exc:
            raise IntegrityError(str(exc)) from exc
    if event.kind == "checkpoint":
        checkpoint = _mapping(payload, "checkpoint")
        string_from_payload(checkpoint, "checkpoint_id")
        if string_from_payload(checkpoint, "session_id") != event.session_id:
            raise IntegrityError("checkpoint session identity is invalid")
        if (
            integer_from_payload(checkpoint, "objective_version")
            != previous.task.objective_version
        ):
            raise IntegrityError("checkpoint objective version is invalid")
        if integer_from_payload(checkpoint, "cursor") != event.sequence - 1:
            raise IntegrityError(
                "checkpoint cursor does not represent its event prefix"
            )
        pending_operation_ids = _string_sequence_from_payload(
            checkpoint, "pending_operation_ids"
        )
        expected_pending = (
            ()
            if previous.pending_proposal_id is None
            else (previous.pending_proposal_id,)
        )
        if pending_operation_ids != expected_pending:
            raise IntegrityError("checkpoint pending operation IDs are invalid")
        expected = hashlib.sha256(
            canonical_json(projection_payload(previous)).encode()
        ).hexdigest()
        supplied = string_from_payload(checkpoint, "projection_digest")
        if supplied != expected and supplied != _legacy_projection_digest(previous):
            raise IntegrityError(
                "checkpoint projection digest does not match its prefix"
            )
        return _with_command_transition(previous, event)
    if event.kind == "agent_waited":
        _same_objective(event, previous)
        reason = string_from_payload(payload, "reason")
        if previous.pending_proposal_id is not None:
            raise IntegrityError("agent wait is illegal while an operation is pending")
        if reason == OPERATOR_ANSWER_WAIT:
            raise IntegrityError("operator-answer wait is reserved for questions")
        return replace(previous, status=SessionStatus.WAITING, wait_reason=reason)
    if event.kind == "question_recorded":
        _same_objective(event, previous)
        if previous.pending_proposal_id is not None:
            raise IntegrityError(
                "operator question is illegal while an operation is pending"
            )
        if previous.blocking_question_id is not None:
            raise IntegrityError("a blocking operator question is already open")
        question_id = string_from_payload(payload, "question_id")
        string_from_payload(payload, "text")
        return replace(
            previous,
            status=SessionStatus.WAITING,
            wait_reason=OPERATOR_ANSWER_WAIT,
            blocking_question_id=question_id,
        )
    if event.kind == "question_answered":
        _same_objective(event, previous)
        if previous.wait_reason != OPERATOR_ANSWER_WAIT:
            raise IntegrityError("question_answered requires an operator-answer wait")
        question_id = string_from_payload(payload, "question_id")
        if previous.blocking_question_id != question_id:
            raise IntegrityError("question_answered does not match the open question")
        string_from_payload(payload, "command_id")
        string_from_payload(payload, "text")
        return replace(
            previous,
            status=(
                previous.status
                if previous.status in {SessionStatus.PAUSED, SessionStatus.STOPPED}
                else SessionStatus.RUNNING
            ),
            wait_reason=None,
            blocking_question_id=None,
        )
    if event.kind == "question_retired":
        _same_objective(event, previous)
        question_id = string_from_payload(payload, "question_id")
        if previous.blocking_question_id != question_id:
            raise IntegrityError("question_retired does not match the open question")
        string_from_payload(payload, "reason")
        return previous
    if event.kind == "plan_recorded":
        _same_objective(event, previous)
        _exact_keys(payload, {"text", "source_event"})
        text = string_from_payload(payload, "text")
        if not text.strip():
            raise IntegrityError("plan_recorded text must be non-empty")
        source_event = integer_from_payload(payload, "source_event")
        if source_event != event.sequence:
            raise IntegrityError("plan_recorded source_event must match its event")
        return previous
    if event.kind == "episode_recorded":
        _same_objective(event, previous)
        _validate_episode_payload(payload, event.sequence)
        return previous
    if event.kind == "knowledge_recorded":
        _same_objective(event, previous)
        _validate_knowledge_recorded(payload, event.sequence)
        return previous
    if event.kind == "knowledge_inspected":
        _same_objective(event, previous)
        _validate_knowledge_inspected(payload)
        return previous
    if event.kind == "investigation_recorded":
        _same_objective(event, previous)
        _validate_investigation_recorded(payload, event.sequence)
        return previous
    if event.kind in {"progress_claim_recorded", "progress_claim_rejected"}:
        _same_objective(event, previous)
        _validate_progress_claim(payload, event.sequence)
        return previous
    if event.kind == "lineage_inspected":
        _same_objective(event, previous)
        _validate_lineage_inspected(payload)
        return previous
    if event.kind == "attempt_inspected":
        _same_objective(event, previous)
        _validate_attempt_inspected(payload, event.sequence)
        return previous
    if event.kind in _IDENTITY_EVENT_KINDS:
        _same_objective(event, previous)
        if event.kind in {"context_assembled", "context_overflow"}:
            duration_ns = payload.get("duration_ns")
            if duration_ns is not None:
                _validate_nonnegative_integer(duration_ns, "context duration_ns")
        return previous
    raise IntegrityError(f"unknown event kind {event.kind!r}")


def _settle_model_event(
    previous: SessionState, payload: Mapping[str, object]
) -> SessionState:
    """Apply measured model usage from a decided or malformed event."""
    request_id = string_from_payload(payload, "request_id")
    reservation = reservation_from_payload(_mapping(payload, "reservation"))
    try:
        operation = model_operation(previous, request_id)
    except TransitionError as exc:
        raise IntegrityError(str(exc)) from exc
    if operation.reservation != reservation:
        raise IntegrityError("model terminal reservation does not match intent")
    _validate_model_identity(operation, payload)
    usage = payload.get("usage")
    measurement = (
        None if usage is None else usage_from_payload(_mapping_value(usage, "usage"))
    )
    try:
        return settle_model_call(previous, reservation, measurement)
    except TransitionError as exc:
        raise IntegrityError(str(exc)) from exc


def _validate_model_response_identity(payload: Mapping[str, object]) -> None:
    """Validate optional semantic duplicate identity on newer model terminals."""
    if "response_identity" not in payload:
        return
    identity = payload["response_identity"]
    if (
        not isinstance(identity, str)
        or len(identity) != 64
        or any(character not in "0123456789abcdef" for character in identity)
    ):
        raise IntegrityError("model response_identity must be a lowercase SHA-256")


def _validate_model_identity(operation, payload: Mapping[str, object]) -> None:
    """Require a terminal event to repeat every identity captured at intent."""
    if (
        operation.input_text is not None
        and payload.get("input") != operation.input_text
    ):
        raise IntegrityError("model terminal input does not match intent")
    if operation.manifest is not None:
        manifest = payload.get("manifest")
        if not isinstance(manifest, Mapping):
            raise IntegrityError("model terminal manifest is missing")
        if canonical_json(manifest) != canonical_json(operation.manifest):
            raise IntegrityError("model terminal manifest does not match intent")
    for name in ("policy_version", "token_counter"):
        expected = getattr(operation, name)
        if expected is not None and payload.get(name) != expected:
            raise IntegrityError(f"model terminal {name} does not match intent")
    for name in ("model_id",):
        expected = getattr(operation, name)
        if expected is not None and payload.get(name) != expected:
            raise IntegrityError(f"model terminal {name} does not match intent")
    expected_cursor = operation.cursor
    if expected_cursor is not None and payload.get("cursor") not in (
        expected_cursor,
        expected_cursor + 1,
    ):
        raise IntegrityError("model terminal cursor does not match intent")


def _validate_supervision_triggered(payload: Mapping[str, object]) -> None:
    """Validate one monitor-trigger payload; the event changes no projection."""
    cause = payload.get("cause")
    if not isinstance(cause, str) or cause not in TRIGGER_CAUSES:
        raise IntegrityError(
            f"supervision_triggered cause must be one of {sorted(TRIGGER_CAUSES)}"
        )
    call = payload.get("call")
    if isinstance(call, bool) or not isinstance(call, int) or call < 1:
        raise IntegrityError("supervision_triggered call must be positive")
    for name in ("actions_used", "tokens_used"):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IntegrityError(
                f"supervision_triggered {name} must be a non-negative integer"
            )
    repeat_count = payload.get("repeat_count")
    if cause in (BURN_CAUSE, CHECKPOINT_CAUSE):
        # Both consumed-call triggers are claim-free: they fire without a
        # repeated fingerprint, so their payloads record neither fingerprint
        # nor count. The checkpoint additionally journals its ledger inputs.
        if repeat_count is not None:
            raise IntegrityError(
                f"supervision_triggered repeat_count must be null for {cause}"
            )
        if payload.get("fingerprint") is not None:
            raise IntegrityError(
                f"supervision_triggered fingerprint must be null for {cause}"
            )
    elif (
        isinstance(repeat_count, bool)
        or not isinstance(repeat_count, int)
        or repeat_count < 0
    ):
        raise IntegrityError(
            "supervision_triggered repeat_count must be a non-negative integer"
        )
    consumed_calls = payload.get("consumed_calls")
    threshold = payload.get("threshold")
    progress_anchor = payload.get("progress_anchor")
    if cause == CHECKPOINT_CAUSE:
        # The ADR-0013 checkpoint journals the settled ledger state it fired
        # on, and replay must be able to reproduce its due-ness from them.
        ledger: dict[str, int] = {}
        for name, value in (
            ("consumed_calls", consumed_calls),
            ("threshold", threshold),
            ("progress_anchor", progress_anchor),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntegrityError(
                    f"supervision_triggered {name} must be a non-negative "
                    "integer for budget-checkpoint"
                )
            ledger[name] = value
        if ledger["threshold"] < 1:
            raise IntegrityError(
                "supervision_triggered threshold must be positive for "
                "budget-checkpoint"
            )
        if ledger["consumed_calls"] - ledger["progress_anchor"] < ledger["threshold"]:
            raise IntegrityError(
                "supervision_triggered checkpoint was not due at its "
                "recorded ledger state"
            )
    elif any(
        value is not None for value in (consumed_calls, threshold, progress_anchor)
    ):
        raise IntegrityError(
            "supervision_triggered checkpoint fields must be null without "
            "budget-checkpoint"
        )
    version = payload.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise IntegrityError("supervision_triggered policy_version must be a string")
    fingerprint = payload.get("fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise IntegrityError("supervision_triggered fingerprint must be text")
    event_ids = payload.get("input_event_ids")
    if not isinstance(event_ids, (list, tuple)) or not all(
        isinstance(item, str) for item in event_ids
    ):
        raise IntegrityError("supervision_triggered input_event_ids must be strings")


def _validate_checkpoint_suppressed(payload: Mapping[str, object]) -> None:
    """Validate one suppressed-checkpoint disposition; no projection changes.

    The event records a checkpoint that was due on the settled ledger and
    suppressed only by the shared repetition cooldown (ADR-0013); replay
    reproduces both conditions from the recorded ledger state.
    """
    version = payload.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        raise IntegrityError("checkpoint_suppressed policy_version must be a string")
    cause = payload.get("cause")
    if cause != CHECKPOINT_CAUSE:
        raise IntegrityError(f"checkpoint_suppressed cause must be {CHECKPOINT_CAUSE}")
    call = payload.get("call")
    if isinstance(call, bool) or not isinstance(call, int) or call < 1:
        raise IntegrityError("checkpoint_suppressed call must be positive")
    ledger: dict[str, int] = {}
    for name, value in (
        ("consumed_calls", payload.get("consumed_calls")),
        ("threshold", payload.get("threshold")),
        ("progress_anchor", payload.get("progress_anchor")),
        ("previous_trigger_call", payload.get("previous_trigger_call")),
        ("cooldown_calls", payload.get("cooldown_calls")),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IntegrityError(
                f"checkpoint_suppressed {name} must be a non-negative integer"
            )
        ledger[name] = value
    if ledger["threshold"] < 1:
        raise IntegrityError("checkpoint_suppressed threshold must be positive")
    if ledger["cooldown_calls"] < 1:
        raise IntegrityError("checkpoint_suppressed cooldown_calls must be positive")
    if ledger["consumed_calls"] - ledger["progress_anchor"] < ledger["threshold"]:
        raise IntegrityError(
            "checkpoint_suppressed checkpoint was not due at its recorded "
            "ledger state"
        )
    if call - ledger["previous_trigger_call"] >= ledger["cooldown_calls"]:
        raise IntegrityError(
            "checkpoint_suppressed checkpoint was not inside the recorded cooldown"
        )
    event_ids = payload.get("input_event_ids")
    if not isinstance(event_ids, (list, tuple)) or not all(
        isinstance(item, str) for item in event_ids
    ):
        raise IntegrityError("checkpoint_suppressed input_event_ids must be strings")


def _validated_guidance_decision(payload: Mapping[str, object]) -> GuidanceDecision:
    """Return one validated guidance decision from a durable payload."""
    value = payload.get("decision")
    if not isinstance(value, str):
        raise IntegrityError("guidance decision must be text")
    try:
        return GuidanceDecision(value)
    except ValueError as exc:
        raise IntegrityError("guidance decision is invalid") from exc


def _validate_operator_escalated(payload: Mapping[str, object]) -> None:
    """Validate one escalation payload; the event changes no projection."""
    stage = payload.get("stage")
    if isinstance(stage, bool) or not isinstance(stage, int) or stage < 1:
        raise IntegrityError("operator_escalated stage must be at least 1")
    call = payload.get("call")
    if isinstance(call, bool) or not isinstance(call, int) or call < 1:
        raise IntegrityError("operator_escalated call must be positive")
    cause = payload.get("cause")
    if not isinstance(cause, str) or cause not in TRIGGER_CAUSES:
        raise IntegrityError(
            f"operator_escalated cause must be one of {sorted(TRIGGER_CAUSES)}"
        )
    fingerprint = payload.get("fingerprint")
    if fingerprint is not None and not isinstance(fingerprint, str):
        raise IntegrityError("operator_escalated fingerprint must be text")
    reason = payload.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise IntegrityError("operator_escalated reason must be non-empty text")
    if stage < 3 and reason is None:
        raise IntegrityError(
            "operator_escalated below the stage ladder requires a reason"
        )


_IDENTITY_EVENT_KINDS = frozenset(
    {
        "context_assembled",
        "context_overflow",
        "lineage_inspected",
        "knowledge_inspected",
        "knowledge_recorded",
        "question_rejected",
        "wait_rejected",
        "plan_recorded",
        "episode_recorded",
        "completion_rejected",
        "investigation_recorded",
        "progress_claim_recorded",
        "progress_claim_rejected",
        "model_configuration_bound",
        "runner_exited",
    }
)


_HISTORICAL_EVIDENCE_KINDS = frozenset(
    {
        "receipt_received",
        "evaluation_intake",
        "evaluation_verification_failed",
        "evaluation_received",
        "evaluation_usage_received",
        "operation_reconciled",
    }
)


def projection_payload(state: SessionState) -> Mapping[str, object]:
    """Return the derived projection used only for checkpoint integrity."""
    return {
        "task": state.task,
        "initial_observation": state.initial_observation,
        "knowledge_sources": state.knowledge_sources,
        "evaluation_contract": state.evaluation_contract,
        "status": state.status,
        "lineage_head": state.lineage_head,
        "active_attempt": state.active_attempt,
        "pending_proposal_id": state.pending_proposal_id,
        "wait_reason": state.wait_reason,
        "blocking_question_id": state.blocking_question_id,
        "ledger": state.ledger,
        "operations": state.operations,
        "model_operations": state.model_operations,
        "latest_evaluation": state.latest_evaluation,
        "command_operations": state.command_operations,
        "attempt_ids": state.attempt_ids,
        "attempt_history": state.attempt_history,
        "accepted_lineage": state.accepted_lineage,
        "reservation_ids": state.reservation_ids,
        "receipt_ids": state.receipt_ids,
        "measurement_ids": state.measurement_ids,
    }


def _legacy_projection_digest(state: SessionState) -> str:
    """Accept checkpoints written before model-operation identity was projected."""
    ledger = state.ledger
    legacy_ledger = {
        "__type__": "UsageLedger",
        "reservations": ledger.reservations,
        "active_reservation_ids": ledger.active_reservation_ids,
        "measurements": ledger.measurements,
        "settled_operation_ids": ledger.settled_operation_ids,
        "released_operation_ids": ledger.released_operation_ids,
        "unknown_cost_operation_ids": ledger.unknown_cost_operation_ids,
        "unknown_usage_operation_ids": ledger.unknown_usage_operation_ids,
    }
    legacy = {
        "task": state.task,
        "initial_observation": state.initial_observation,
        "knowledge_sources": state.knowledge_sources,
        "evaluation_contract": state.evaluation_contract,
        "status": state.status,
        "lineage_head": state.lineage_head,
        "active_attempt": state.active_attempt,
        "pending_proposal_id": state.pending_proposal_id,
        "wait_reason": state.wait_reason,
        "blocking_question_id": state.blocking_question_id,
        "ledger": legacy_ledger,
        "operations": state.operations,
        "latest_evaluation": state.latest_evaluation,
        "command_operations": state.command_operations,
        "attempt_ids": state.attempt_ids,
        "attempt_history": state.attempt_history,
        "accepted_lineage": state.accepted_lineage,
        "reservation_ids": state.reservation_ids,
        "receipt_ids": state.receipt_ids,
        "measurement_ids": state.measurement_ids,
    }
    return hashlib.sha256(canonical_json(legacy).encode()).hexdigest()


def replay(events: Iterable[Event]) -> SessionState:
    """Validate sequence and session identity before reducing all event facts."""
    stream = tuple(events)
    _validate_episode_history(stream)
    _validate_retention_history(stream)
    _validate_model_configuration_history(stream)
    state: SessionState | None = None
    session_id: str | None = None
    sequence = 0
    for event in stream:
        if session_id is None:
            session_id = event.session_id
        elif event.session_id != session_id:
            raise IntegrityError("journal stream mixes session identities")
        sequence += 1
        if event.sequence != sequence:
            raise IntegrityError("journal event sequence is not contiguous")
        state = reduce_event(state, event)
    if state is None:
        raise IntegrityError("journal has no events")
    _validate_attempt_inspection_sources(stream)
    return state


def _validate_attempt_inspection_sources(events: tuple[Event, ...]) -> None:
    """Resolve attempt inspection references against preceding source events."""
    by_sequence = {event.sequence: event for event in events}
    started: set[str] = set()
    closed: set[str] = set()
    request_attempts: dict[str, str] = {}
    for event in events:
        if event.kind == "attempt_started":
            started.add(
                string_from_payload(_mapping(event.payload, "attempt"), "attempt_id")
            )
            continue
        if event.kind == "evaluation_intended":
            request = _mapping(event.payload, "request")
            request_attempts[string_from_payload(request, "request_id")] = (
                string_from_payload(request, "attempt_id")
            )
            continue
        if event.kind == "evaluation_received":
            receipt = _mapping(event.payload, "receipt")
            attempt_id = request_attempts.get(
                string_from_payload(receipt, "request_id")
            )
            if attempt_id is not None:
                closed.add(attempt_id)
            continue
        if event.kind == "objective_steered":
            closed.update(started - closed)
            continue
        if event.kind != "attempt_inspected":
            continue
        attempt = _mapping(event.payload, "attempt")
        attempt_id = string_from_payload(attempt, "attempt_id")
        if not attempt.get("found"):
            if attempt_id in closed:
                raise IntegrityError("attempt inspection hides a closed attempt")
            continue
        if attempt_id not in started:
            raise IntegrityError("attempt inspection references an unknown attempt")
        if attempt_id not in closed:
            raise IntegrityError("attempt inspection references an open attempt")
        for source in _sequence(attempt, "sources"):
            reference = _mapping_value(source, "attempt evidence source")
            sequence = integer_from_payload(reference, "sequence")
            original = by_sequence.get(sequence)
            if (
                original is None
                or original.sequence >= event.sequence
                or original.event_id != reference.get("event_id")
                or original.kind != reference.get("kind")
            ):
                raise IntegrityError("attempt inspection source link is invalid")


def _validate_retention_history(events: tuple[Event, ...]) -> None:
    """Validate eviction identities and their crash-recovery pairing."""
    pending: dict[str, tuple[int, str, int]] = {}
    for event in events:
        if event.kind == "artifact_eviction_intended":
            _validate_artifact_eviction_intended(event.payload)
            digest = _validate_digest(event.payload["digest"])
            if digest in pending:
                raise IntegrityError("artifact eviction intent is duplicated")
            pending[digest] = (
                _validate_nonnegative_integer(
                    event.payload["size"], "artifact eviction size"
                ),
                _validate_eviction_reason(event.payload["reason"]),
                _validate_nonnegative_integer(
                    event.payload["byte_budget"], "artifact eviction byte budget"
                ),
            )
        elif event.kind == "artifact_evicted":
            _validate_artifact_evicted(event.payload)
            digest = _validate_digest(event.payload["digest"])
            expected = pending.pop(digest, None)
            if expected is None:
                raise IntegrityError("artifact eviction outcome has no intent")
            actual = (
                event.payload["size"],
                event.payload["reason"],
                event.payload["byte_budget"],
            )
            if actual != expected:
                raise IntegrityError(
                    "artifact eviction outcome identity mismatches intent"
                )


def _validate_model_configuration_history(events: tuple[Event, ...]) -> None:
    """Keep model intents tied to the configuration captured before calling."""
    configurations = [
        event.payload.get("configuration")
        for event in events
        if event.kind == "model_configuration_bound"
    ]
    if not configurations:
        return
    if len(configurations) != 1 or not isinstance(configurations[0], Mapping):
        raise IntegrityError("model configuration binding is duplicated or invalid")
    configuration = configurations[0]
    _validate_model_configuration(configuration)
    for event in events:
        if event.kind != "model_intended":
            continue
        for name in ("model_id", "policy_version", "token_counter"):
            if event.payload.get(name) != configuration[name]:
                raise IntegrityError(
                    f"model intent {name} differs from durable configuration"
                )


def _validate_model_configuration(payload: Mapping[str, object]) -> None:
    _exact_keys(
        payload,
        {
            "model_id",
            "provider_id",
            "policy_version",
            "system_prompt_digest",
            "output_settings",
            "repair_settings",
            "retry_settings",
            "token_counter",
            "context_coefficients",
            "token_budget",
        },
    )
    for name in ("model_id", "provider_id", "token_counter"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"model configuration {name} is invalid")
    for name in ("policy_version", "token_budget"):
        value = _validate_nonnegative_integer(
            payload.get(name), f"model configuration {name}"
        )
        if value < 1:
            raise IntegrityError(f"model configuration {name} is invalid")
    _validate_digest(payload.get("system_prompt_digest"))
    for name in (
        "output_settings",
        "repair_settings",
        "retry_settings",
        "context_coefficients",
    ):
        if not isinstance(payload.get(name), Mapping):
            raise IntegrityError(f"model configuration {name} must be an object")


def _validate_artifact_eviction_intended(payload: Mapping[str, object]) -> None:
    _exact_keys(payload, {"digest", "size", "reason", "byte_budget"})
    _validate_digest(payload.get("digest"))
    _validate_nonnegative_integer(payload.get("size"), "artifact eviction size")
    _validate_nonnegative_integer(
        payload.get("byte_budget"), "artifact eviction byte budget"
    )
    _validate_eviction_reason(payload.get("reason"))


def _validate_artifact_evicted(payload: Mapping[str, object]) -> None:
    _exact_keys(
        payload,
        {"digest", "size", "reason", "byte_budget", "available", "dropped"},
    )
    _validate_digest(payload.get("digest"))
    _validate_nonnegative_integer(payload.get("size"), "artifact eviction size")
    _validate_nonnegative_integer(
        payload.get("byte_budget"), "artifact eviction byte budget"
    )
    _validate_eviction_reason(payload.get("reason"))
    if not isinstance(payload.get("available"), bool):
        raise IntegrityError("artifact eviction availability must be a boolean")
    if not isinstance(payload.get("dropped"), bool):
        raise IntegrityError("artifact eviction outcome must be a boolean")
    if payload["dropped"] and payload["available"]:
        raise IntegrityError("dropped artifact cannot remain available")


def _validate_artifact_read(payload: Mapping[str, object]) -> None:
    """Validate one measured successful read in the declared session scope."""
    _exact_keys(payload, {"scope", "purpose", "digest", "bytes_read"})
    if payload.get("scope") != "session_context_and_evaluation_successful_reads_v1":
        raise IntegrityError("artifact_read scope is invalid")
    purpose = payload.get("purpose")
    if not isinstance(purpose, str) or purpose not in {
        "context_observation",
        "context_receipt_content",
        "evaluation_evidence",
    }:
        raise IntegrityError("artifact_read purpose is invalid")
    _validate_digest(payload.get("digest"))
    _validate_nonnegative_integer(payload.get("bytes_read"), "artifact bytes_read")


def _validate_digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise IntegrityError("artifact eviction digest must be a SHA-256 digest")
    return value


def _validate_nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IntegrityError(f"{name} must be a non-negative integer")
    return value


def _validate_eviction_reason(value: object) -> str:
    if not isinstance(value, str) or value != "byte_budget":
        raise IntegrityError("artifact eviction reason is invalid")
    return value


def _validate_episode_history(events: tuple[Event, ...]) -> None:
    """Validate episode spans against the checkpointed source history."""
    spans: set[tuple[int, int]] = set()
    for event in events:
        if event.kind != "episode_recorded":
            continue
        episode = _validate_episode_payload(event.payload, event.sequence)
        source_start = episode[0]
        source_end = episode[1]
        span = (source_start, source_end)
        if span in spans:
            raise IntegrityError("episode source span is already recorded")
        spans.add(span)
        source_versions = {
            source.objective_version
            for source in events
            if source_start <= source.sequence <= source_end
        }
        if len(source_versions) != 1:
            raise IntegrityError("episode source span crosses objective versions")
        if not any(
            checkpoint.kind == "checkpoint"
            and source_end < checkpoint.sequence < event.sequence
            and _checkpoint_covers(checkpoint, source_end)
            for checkpoint in events
        ):
            raise IntegrityError("episode span has no covering checkpoint")


def _validate_episode_payload(
    payload: Mapping[str, object], event_sequence: int
) -> tuple[int, int]:
    """Validate one source-linked episode fact and return its source span."""
    _exact_keys(payload, {"episode"})
    episode = _mapping(payload, "episode")
    _exact_keys(
        episode,
        {
            "episode_id",
            "source_start",
            "source_end",
            "summary",
            "evidence_ids",
            "model_id",
            "model_version",
            "source_event",
        },
    )
    for name in ("episode_id", "summary", "model_id", "model_version"):
        value = string_from_payload(episode, name)
        if not value.strip():
            raise IntegrityError(f"episode {name} must be non-empty")
    source_start = integer_from_payload(episode, "source_start")
    source_end = integer_from_payload(episode, "source_end")
    source_event = integer_from_payload(episode, "source_event")
    if source_start < 1 or source_end < source_start:
        raise IntegrityError("episode source span is invalid")
    if source_end >= event_sequence or source_event != event_sequence:
        raise IntegrityError("episode source span or source_event is invalid")
    _nonempty_string_sequence(episode, "evidence_ids")
    return source_start, source_end


def _validate_knowledge_recorded(
    payload: Mapping[str, object], event_sequence: int
) -> None:
    _exact_keys(payload, {"item", "assessment"})
    item = _mapping(payload, "item")
    _exact_keys(
        item,
        {
            "item_id",
            "statement",
            "scope",
            "status",
            "evidence_ids",
            "provenance",
            "source_event",
        },
    )
    for name in ("item_id", "statement", "scope", "provenance"):
        if not string_from_payload(item, name).strip():
            raise IntegrityError(f"knowledge item {name} must be non-empty")
    if string_from_payload(item, "status") not in {
        "hypothesis",
        "supported",
        "contradicted",
        "retired",
    }:
        raise IntegrityError("knowledge item status is invalid")
    _nonempty_string_sequence(item, "evidence_ids")
    if integer_from_payload(item, "source_event") != event_sequence:
        raise IntegrityError("knowledge item source_event must match its event")
    _validate_assessment(payload)


def _validate_knowledge_inspected(payload: Mapping[str, object]) -> None:
    _exact_keys(payload, {"item_ids", "items"})
    item_ids = _string_sequence_from_payload(payload, "item_ids")
    if len(item_ids) != len(set(item_ids)):
        raise IntegrityError("knowledge inspection IDs are duplicated")
    items = _sequence(payload, "items")
    if len(item_ids) != len(items):
        raise IntegrityError("knowledge inspection IDs do not match items")
    for item in items:
        candidate = _mapping_value(item, "knowledge item")
        _exact_keys(
            candidate,
            {
                "item_id",
                "statement",
                "scope",
                "status",
                "evidence_ids",
                "provenance",
                "source_event",
            },
        )
        for name in ("item_id", "statement", "scope", "provenance"):
            value = candidate.get(name)
            if not isinstance(value, str) or not value.strip():
                raise IntegrityError(f"knowledge inspected {name} must be non-empty")
        if candidate.get("status") not in {
            "hypothesis",
            "supported",
            "contradicted",
            "retired",
        }:
            raise IntegrityError("knowledge inspected status is invalid")
        _string_sequence_from_payload(candidate, "evidence_ids")
        if integer_from_payload(candidate, "source_event") < 1:
            raise IntegrityError("knowledge inspected source_event is invalid")
        if candidate["item_id"] not in item_ids:
            raise IntegrityError("knowledge inspection item identity is invalid")


def _validate_investigation_recorded(
    payload: Mapping[str, object], event_sequence: int
) -> None:
    _exact_keys(payload, {"investigation"})
    item = _mapping(payload, "investigation")
    _exact_keys(
        item,
        {
            "investigation_id",
            "question",
            "disposition",
            "hypotheses",
            "attempt_ids",
            "evidence_ids",
            "usage_ids",
            "uncertainty",
            "source_event",
        },
    )
    for name in ("investigation_id", "question", "uncertainty"):
        if not isinstance(item.get(name), str):
            raise IntegrityError(f"investigation {name} must be a string")
    if not str(item["investigation_id"]).strip() or not str(item["question"]).strip():
        raise IntegrityError("investigation identity must be non-empty")
    if item.get("disposition") not in {"active", "paused", "concluded", "abandoned"}:
        raise IntegrityError("investigation disposition is invalid")
    for name in ("hypotheses", "attempt_ids", "evidence_ids", "usage_ids"):
        _string_sequence_from_payload(item, name)
    if integer_from_payload(item, "source_event") != event_sequence:
        raise IntegrityError("investigation source_event must match its event")


def _validate_progress_claim(
    payload: Mapping[str, object], event_sequence: int
) -> None:
    _exact_keys(payload, {"claim", "assessment"})
    claim = _mapping(payload, "claim")
    _exact_keys(
        claim,
        {
            "claim_id",
            "category",
            "before",
            "after",
            "scope",
            "evidence_ids",
            "assessor",
            "status",
            "source_event",
        },
    )
    for name in ("claim_id", "before", "after", "scope", "assessor"):
        value = claim.get(name)
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"progress claim {name} must be non-empty")
    if claim.get("category") not in {"outcome", "knowledge"}:
        raise IntegrityError("progress claim category is invalid")
    if claim.get("status") not in {"pending", "supported", "rejected", "stale"}:
        raise IntegrityError("progress claim status is invalid")
    _string_sequence_from_payload(claim, "evidence_ids")
    if integer_from_payload(claim, "source_event") != event_sequence:
        raise IntegrityError("progress claim source_event must match its event")
    _validate_assessment(payload)


def _validate_lineage_inspected(payload: Mapping[str, object]) -> None:
    _exact_keys(payload, {"node_ids", "nodes"})
    node_ids = _string_sequence_from_payload(payload, "node_ids")
    nodes = _sequence(payload, "nodes")
    if len(node_ids) != len(nodes):
        raise IntegrityError("lineage inspection IDs do not match nodes")
    for item in nodes:
        node = _mapping_value(item, "lineage node")
        decoded = node_from_payload(node)
        if decoded.node_id not in node_ids:
            raise IntegrityError("lineage inspection node identity is invalid")


def _validate_attempt_inspected(
    payload: Mapping[str, object], event_sequence: int
) -> None:
    """Validate the bounded source links recorded for an inspected attempt."""
    _exact_keys(payload, {"attempt"})
    attempt = _mapping(payload, "attempt")
    _exact_keys(
        attempt,
        {
            "attempt_id",
            "found",
            "disposition",
            "base_node_id",
            "source_start",
            "source_end",
            "sources",
        },
    )
    string_from_payload(attempt, "attempt_id")
    found = attempt.get("found")
    if not isinstance(found, bool):
        raise IntegrityError("attempt inspection found flag is invalid")
    start = integer_from_payload(attempt, "source_start")
    end = integer_from_payload(attempt, "source_end")
    sources = _sequence(attempt, "sources")
    if len(sources) > 24:
        raise IntegrityError("attempt inspection exceeds its source bound")
    if not found:
        if (
            attempt.get("disposition") is not None
            or attempt.get("base_node_id") is not None
            or start != 0
            or end != 0
            or sources
        ):
            raise IntegrityError("missing attempt inspection contains evidence")
        return
    string_from_payload(attempt, "base_node_id")
    disposition = attempt.get("disposition")
    try:
        parsed_disposition = AttemptDisposition(disposition)
    except (TypeError, ValueError) as exc:
        raise IntegrityError("attempt inspection disposition is invalid") from exc
    if parsed_disposition is AttemptDisposition.ACTIVE:
        raise IntegrityError("attempt inspection requires a closed attempt")
    if start < 1 or end < start or end >= event_sequence:
        raise IntegrityError("attempt inspection source range is invalid")
    seen: set[str] = set()
    for source in sources:
        item = _mapping_value(source, "attempt evidence source")
        _exact_keys(item, {"event_id", "sequence", "kind", "summary"})
        event_id = string_from_payload(item, "event_id")
        sequence = integer_from_payload(item, "sequence")
        string_from_payload(item, "kind")
        string_from_payload(item, "summary")
        if sequence < start or sequence > end or sequence >= event_sequence:
            raise IntegrityError("attempt evidence source cursor is invalid")
        if event_id in seen:
            raise IntegrityError("attempt inspection repeats a source event")
        seen.add(event_id)


def _validate_assessment(payload: Mapping[str, object]) -> None:
    """Validate the evidence assessment attached to a projection event."""
    assessment = _mapping(payload, "assessment")
    _exact_keys(
        assessment,
        {
            "target_id",
            "category",
            "before",
            "after",
            "scope",
            "assessor",
            "evidence_ids",
            "provenance",
            "status",
            "reasons",
        },
    )
    for name in ("target_id", "before", "after", "scope", "assessor"):
        if not isinstance(assessment.get(name), str):
            raise IntegrityError(f"assessment {name} must be a string")
    if assessment.get("category") not in {"outcome", "knowledge"}:
        raise IntegrityError("assessment category is invalid")
    if assessment.get("status") not in {
        "pending",
        "supported",
        "rejected",
        "stale",
    }:
        raise IntegrityError("assessment status is invalid")
    _string_sequence_from_payload(assessment, "evidence_ids")
    _string_sequence_from_payload(assessment, "provenance")
    _string_sequence_from_payload(assessment, "reasons")


def _checkpoint_covers(event: Event, source_end: int) -> bool:
    checkpoint = event.payload.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        return False
    cursor = checkpoint.get("cursor")
    return (
        isinstance(cursor, int)
        and not isinstance(cursor, bool)
        and cursor >= source_end
    )


def _mapping(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    candidate = value.get(name)
    if not isinstance(candidate, Mapping):
        raise IntegrityError(f"event payload {name} must be an object")
    return candidate


def _mapping_value(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise IntegrityError(f"event payload {name} must be an object")
    return value


def _optional_string(value: Mapping[str, object], name: str) -> str | None:
    candidate = value.get(name)
    if candidate is not None and not isinstance(candidate, str):
        raise IntegrityError(f"event payload {name} must be a string or null")
    return candidate


def _optional_integer(value: Mapping[str, object], name: str) -> int | None:
    candidate = value.get(name)
    if candidate is None:
        return None
    if isinstance(candidate, bool) or not isinstance(candidate, int):
        raise IntegrityError(f"event payload {name} must be an integer or null")
    return candidate


def _sequence(value: Mapping[str, object], name: str) -> tuple[object, ...]:
    candidate = value.get(name)
    if not isinstance(candidate, (list, tuple)):
        raise IntegrityError(f"event payload {name} must be an array")
    return tuple(candidate)


def _validate_creation_inputs(event: Event, observation, sources, contract) -> None:
    """Reproduce creation-boundary provenance checks during durable replay."""
    records = (
        (
            observation.observation_id,
            observation.envelope,
            observation.observed_at,
            observation.provenance,
        ),
        *(
            (source.source_id, source.envelope, source.observed_at, source.provenance)
            for source in sources
        ),
        (
            contract.contract_id,
            contract.envelope,
            contract.envelope.recorded_at,
            contract.envelope.provenance,
        ),
    )
    ids = [item[0] for item in records]
    if len(ids) != len(set(ids)):
        raise IntegrityError("creation input record identities must be unique")
    if any(
        envelope.record_id != record_id
        or envelope.session_id != event.session_id
        or envelope.objective_version != event.objective_version
        or envelope.recorded_at != timestamp
        or envelope.provenance != provenance
        for record_id, envelope, timestamp, provenance in records
    ):
        raise IntegrityError("creation input envelope does not match its record")
    identity = event.payload.get("run_identity")
    if not isinstance(identity, Mapping) or not isinstance(
        identity.get("evaluator"), Mapping
    ):
        raise IntegrityError("session creation lacks evaluator identity")
    if canonical_json(contract.evaluator) != canonical_json(identity["evaluator"]):
        raise IntegrityError("evaluation contract evaluator differs from run identity")


def _exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    missing = expected - set(value)
    unsupported = set(value) - expected - {"extensions", "__type__"}
    if missing or unsupported:
        raise IntegrityError("event payload has unexpected or missing fields")
    extensions = value.get("extensions", {})
    if not isinstance(extensions, Mapping) or not all(
        isinstance(key, str) for key in extensions
    ):
        raise IntegrityError("event payload extensions must be a string-keyed object")


def _string_sequence_from_payload(
    value: Mapping[str, object], name: str
) -> tuple[str, ...]:
    candidate = value.get(name)
    if not isinstance(candidate, (list, tuple)) or not all(
        isinstance(item, str) for item in candidate
    ):
        raise IntegrityError(f"event payload {name} must be an array of strings")
    return tuple(candidate)


def _nonempty_string_sequence(value: Mapping[str, object], name: str) -> None:
    """Validate a string array whose members are durable identities."""
    items = _string_sequence_from_payload(value, name)
    if any(not item.strip() for item in items):
        raise IntegrityError(f"event payload {name} must contain non-empty strings")


def _promotion_disposition(payload: Mapping[str, object]) -> PromotionDisposition:
    try:
        return PromotionDisposition(string_from_payload(payload, "disposition"))
    except ValueError as exc:
        raise IntegrityError("event payload disposition is invalid") from exc


def _same_objective(event: Event, state: SessionState) -> None:
    if event.objective_version != state.task.objective_version:
        raise IntegrityError("event objective version disagrees with projection")


def _command_causation(event: Event, command: Mapping[str, object]) -> None:
    if event.causation_id != string_from_payload(command, "command_id"):
        raise IntegrityError("control event has invalid command causation")


def _admitted_decision(state: SessionState, command_id: str) -> EvaluationDecision:
    """Return the admitted evaluation decision for one command ID.

    The transition-binding shortcut exists only for an equivalent evaluation
    decision, so a control command can never manufacture its own binding.
    """
    for item in state.command_operations:
        if item.command.command_id != command_id:
            continue
        command = item.command
        if isinstance(command, EvaluationDecision):
            return command
        break
    raise IntegrityError("only an equivalent evaluation decision binds a transition")


def _with_command_transition(
    state: SessionState,
    event: Event,
    *,
    embedded_command: OperatorCommand | None = None,
    evaluated_state: StateRef | None = None,
) -> SessionState:
    if event.causation_id is None:
        return state
    try:
        return record_command_transition(
            state,
            event.causation_id,
            event.kind,
            embedded_command=embedded_command,
            evaluated_state=evaluated_state,
        )
    except TransitionError as exc:
        raise IntegrityError(str(exc)) from exc
