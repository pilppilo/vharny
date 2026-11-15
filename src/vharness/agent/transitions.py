"""Pure session transition checks and resource accounting."""

# pylint: disable=too-many-lines
# Model-call reservation/settlement belongs with the other ledger transitions.

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib

from ..core import MODEL_FAILURE_CLASSES
from .codec import canonical_json
from .errors import TransitionError
from .models import (
    Attempt,
    AttemptHistory,
    AttemptDisposition,
    CommittedNode,
    EvaluationReceipt,
    EvaluationContract,
    ExecutionReceipt,
    ExecutionStatus,
    EvaluationCommand,
    EvaluationDecision,
    EvaluationRequest,
    CheckpointRequest,
    Message,
    OPERATOR_ANSWER_WAIT,
    PROVIDER_RECOVERY_WAIT,
    KnowledgeSource,
    Observation,
    OperatorCommand,
    Pause,
    PromotionDisposition,
    ResourceReservation,
    SessionStatus,
    Resume,
    StateRef,
    Steer,
    Stop,
    TaskSpec,
    UsageMeasurement,
)
from .usage_ledger import UsageLedger


class EvaluationOperationStatus(str, Enum):
    """Monotonic lifecycle of one externally requested evaluation."""

    INTENDED = "intended"
    INTAKE_RECORDED = "intake_recorded"
    VERIFICATION_FAILED = "verification_failed"
    RECEIPT_CONSUMED = "receipt_consumed"
    NOT_SUBMITTED = "not_submitted"


class ActionOperationStatus(str, Enum):
    """Monotonic lifecycle of one externally proposed runtime action."""

    INTENDED = "intended"
    NOT_SUBMITTED = "not_submitted"
    RECEIPT_RECEIVED = "receipt_received"


class ReconciliationStatus(str, Enum):
    """Durable external answer to recovery of an interrupted operation."""

    RECEIPT = "receipt"
    NOT_SUBMITTED = "not_submitted"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


class OperationKind(str, Enum):
    """The external port that owns one durable operation."""

    ACTION = "action"
    EVALUATION = "evaluation"


@dataclass(frozen=True, slots=True)
# These persisted facts are the replay contract, not independent responsibilities.
# pylint: disable=too-many-instance-attributes
class ActionOperation:
    """Replay-owned lifecycle and accounting facts for one runtime action."""

    kind: OperationKind
    proposal_id: str
    attempt_id: str
    objective_version: int
    reservation: ResourceReservation
    status: ActionOperationStatus = ActionOperationStatus.INTENDED
    receipt_status: str | None = None
    settled: bool = False
    reconciliation: ReconciliationStatus | None = None
    reconciliation_receipt: ExecutionReceipt | None = None
    receipts: tuple[ExecutionReceipt, ...] = ()
    expected_revision: str | None = None
    requires_revision: bool = False


@dataclass(frozen=True, slots=True)
# These persisted facts are the replay contract, not independent responsibilities.
# pylint: disable=too-many-instance-attributes
class EvaluationOperation:
    """Replay-owned facts for one evaluation request and its terminal handling."""

    request: EvaluationRequest
    reservation: ResourceReservation
    status: EvaluationOperationStatus = EvaluationOperationStatus.INTENDED
    receipt_id: str | None = None
    raw_receipt: str | None = None
    reconciliation: ReconciliationStatus | None = None
    reconciliation_receipt: EvaluationReceipt | None = None
    kind: OperationKind = field(default=OperationKind.EVALUATION, init=False)


OperationRecord = ActionOperation | EvaluationOperation


@dataclass(frozen=True, slots=True)
class ModelCallOperation:
    """Durable identities captured before one model terminal is accepted."""

    request_id: str
    reservation: ResourceReservation
    input_text: str | None = None
    manifest: object | None = None
    policy_version: int | None = None
    token_counter: str | None = None
    model_id: str | None = None
    cursor: int | None = None
    settled: bool = False


@dataclass(frozen=True, slots=True)
class CommandOperation:
    """Replay-owned lifecycle for one admitted operator command."""

    command: OperatorCommand
    transition_kind: str | None = None
    disposition: str | None = None


# The projection intentionally holds each scheduling fact needed by replay.
# pylint: disable=too-many-instance-attributes
@dataclass(frozen=True, slots=True)
class SessionState:
    """Small rebuildable projection needed for phase-one scheduling."""

    task: TaskSpec
    status: SessionStatus
    lineage_head: CommittedNode
    initial_observation: Observation | None = None
    knowledge_sources: tuple[KnowledgeSource, ...] = ()
    evaluation_contract: EvaluationContract | None = None
    active_attempt: Attempt | None = None
    pending_proposal_id: str | None = None
    wait_reason: str | None = None
    blocking_question_id: str | None = None
    ledger: UsageLedger = UsageLedger()
    operations: tuple[OperationRecord, ...] = ()
    model_operations: tuple[ModelCallOperation, ...] = ()
    latest_evaluation: (
        tuple[EvaluationRequest, EvaluationReceipt, PromotionDisposition] | None
    ) = None
    command_operations: tuple[CommandOperation, ...] = ()
    attempt_ids: tuple[str, ...] = ()
    attempt_history: tuple[AttemptHistory, ...] = ()
    accepted_lineage: tuple[CommittedNode, ...] = ()
    reservation_ids: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()
    measurement_ids: tuple[str, ...] = ()

    @property
    def action_operations(self) -> tuple[ActionOperation, ...]:
        """Return runtime operations without duplicating the persisted sequence."""
        return tuple(
            item for item in self.operations if isinstance(item, ActionOperation)
        )

    @property
    def evaluation_operations(self) -> tuple[EvaluationOperation, ...]:
        """Return evaluator operations without duplicating the persisted sequence."""
        return tuple(
            item for item in self.operations if isinstance(item, EvaluationOperation)
        )


def admit_command(state: SessionState, command: OperatorCommand) -> SessionState:
    """Record a command once before any transition can acknowledge it."""
    if any(
        item.command.command_id == command.command_id
        for item in state.command_operations
    ):
        raise TransitionError("command ID is already admitted")
    return replace(
        state, command_operations=(*state.command_operations, CommandOperation(command))
    )


def record_command_transition(
    state: SessionState,
    command_id: str,
    transition_kind: str,
    *,
    embedded_command: OperatorCommand | None = None,
    evaluated_state: StateRef | None = None,
) -> SessionState:
    """Bind one durable state transition to its still-pending command."""
    operation = _command_operation(state, command_id)
    if _command_transition_kind(operation.command) != transition_kind:
        raise TransitionError("command transition kind does not match admission")
    if embedded_command is not None and embedded_command != operation.command:
        raise TransitionError("command transition does not match admission")
    if isinstance(operation.command, EvaluationCommand):
        if evaluated_state != operation.command.state:
            raise TransitionError("evaluation command state does not match request")
    if operation.transition_kind is not None or operation.disposition is not None:
        raise TransitionError("command transition is already recorded")
    return replace(
        state,
        command_operations=_replace_command(
            state, replace(operation, transition_kind=transition_kind)
        ),
    )


def settle_command(
    state: SessionState, command_id: str, disposition: str
) -> SessionState:
    """Irreversibly record an applied or rejected disposition."""
    operation = _command_operation(state, command_id)
    if operation.disposition is not None:
        raise TransitionError("command disposition is already recorded")
    if disposition == "applied" and operation.transition_kind is None:
        raise TransitionError("command applied without a transition")
    if disposition == "rejected" and operation.transition_kind is not None:
        raise TransitionError("command rejected after a transition")
    return replace(
        state,
        command_operations=_replace_command(
            state, replace(operation, disposition=disposition)
        ),
    )


def _command_operation(state: SessionState, command_id: str) -> CommandOperation:
    for operation in state.command_operations:
        if operation.command.command_id == command_id:
            return operation
    raise TransitionError("command disposition has no admission")


def _replace_command(
    state: SessionState, updated: CommandOperation
) -> tuple[CommandOperation, ...]:
    return tuple(
        updated if item.command.command_id == updated.command.command_id else item
        for item in state.command_operations
    )


_COMMAND_TRANSITION_KINDS = {
    Message: "operator_message",
    Steer: "objective_steered",
    Pause: "session_paused",
    Resume: "session_resumed",
    Stop: "session_stopped",
    CheckpointRequest: "checkpoint",
    EvaluationCommand: "evaluation_intended",
    EvaluationDecision: "evaluation_received",
}


def _command_transition_kind(command: OperatorCommand) -> str:
    try:
        return _COMMAND_TRANSITION_KINDS[type(command)]
    except KeyError as exc:  # pragma: no cover - decoder admits only known variants
        raise TransitionError("command type has no transition kind") from exc


def start_attempt(
    state: SessionState, attempt_id: str, event_cursor: int = 1
) -> SessionState:
    """Start a new trajectory from the current committed lineage node."""
    if state.active_attempt is not None:
        raise TransitionError("an attempt is already active")
    if attempt_id in state.attempt_ids:
        raise TransitionError("attempt ID is already recorded")
    if state.status not in (SessionStatus.CREATED, SessionStatus.RUNNING):
        raise TransitionError(
            f"cannot start an attempt while session is {state.status.value}"
        )
    attempt = Attempt(
        attempt_id=attempt_id,
        objective_version=state.task.objective_version,
        base_node_id=state.lineage_head.node_id,
        starting_state=state.lineage_head.state,
    )
    return replace(
        state,
        status=SessionStatus.RUNNING,
        active_attempt=attempt,
        attempt_ids=(*state.attempt_ids, attempt_id),
        attempt_history=(*state.attempt_history, AttemptHistory(attempt, event_cursor)),
    )


def abandon_attempt(state: SessionState, event_cursor: int) -> SessionState:
    """Close the active attempt as abandoned so a fresh attempt can start.

    Used by supervision checkpoint/replan and by an agent replan response: the
    attempt keeps its full history with an ABANDONED disposition and never
    enters committed lineage.
    """
    if state.active_attempt is None:
        raise TransitionError("no active attempt to abandon")
    if state.pending_proposal_id is not None:
        raise TransitionError("a pending operation cannot be abandoned")
    if state.status is not SessionStatus.RUNNING:
        raise TransitionError(
            f"cannot abandon an attempt while session is {state.status.value}"
        )
    attempt = replace(state.active_attempt, disposition=AttemptDisposition.ABANDONED)
    history = tuple(
        (
            replace(item, attempt=attempt, event_end_cursor=event_cursor)
            if item.attempt.attempt_id == attempt.attempt_id
            and item.event_end_cursor is None
            else item
        )
        for item in state.attempt_history
    )
    return replace(state, active_attempt=None, attempt_history=history)


# Intent binds all immutable action identities at the durable transition boundary.
# pylint: disable=too-many-arguments
def intend_action(
    state: SessionState,
    proposal_id: str,
    attempt_id: str,
    reservation: ResourceReservation,
    *,
    expected_revision: str | None = None,
    requires_revision: bool = False,
) -> SessionState:
    """Reserve and record an action owned by the current active attempt."""
    if state.active_attempt is None or state.active_attempt.attempt_id != attempt_id:
        raise TransitionError("action does not belong to the active attempt")
    if reservation.operation_id != proposal_id:
        raise TransitionError("action reservation does not match proposal")
    if _operation_id_is_recorded(state, proposal_id):
        raise TransitionError("action proposal ID is already recorded")
    if reservation.reservation_id in state.reservation_ids:
        raise TransitionError("reservation ID is already recorded")
    if requires_revision and expected_revision is None:
        raise TransitionError(
            "revision-capable runtime requires expected state revision"
        )
    ledger = state.ledger.reserve(reservation, state.task.budgets)
    return replace(
        state,
        status=SessionStatus.WAITING,
        pending_proposal_id=proposal_id,
        wait_reason="runtime receipt pending",
        ledger=ledger,
        reservation_ids=(*state.reservation_ids, reservation.reservation_id),
        operations=(
            *state.operations,
            ActionOperation(
                OperationKind.ACTION,
                proposal_id,
                attempt_id,
                state.task.objective_version,
                reservation,
                expected_revision=expected_revision,
                requires_revision=requires_revision,
            ),
        ),
    )


def intend_evaluation(
    state: SessionState,
    request: EvaluationRequest,
    reservation: ResourceReservation,
    *,
    wait_reason: str = "evaluation pending",
) -> SessionState:
    """Reserve and register one evaluation before its answer arrives."""
    if (
        state.active_attempt is None
        or state.active_attempt.attempt_id != request.attempt_id
    ):
        raise TransitionError("evaluation does not belong to the active attempt")
    if reservation.operation_id != request.request_id:
        raise TransitionError("evaluation reservation does not match request")
    if _operation_id_is_recorded(state, request.request_id):
        raise TransitionError("evaluation request ID is already recorded")
    if reservation.reservation_id in state.reservation_ids:
        raise TransitionError("reservation ID is already recorded")
    ledger = state.ledger.reserve(reservation, state.task.budgets)
    return replace(
        state,
        status=SessionStatus.WAITING,
        pending_proposal_id=request.request_id,
        wait_reason=wait_reason,
        ledger=ledger,
        reservation_ids=(*state.reservation_ids, reservation.reservation_id),
        attempt_history=_append_evaluation_request(state.attempt_history, request),
        operations=(
            *state.operations,
            EvaluationOperation(request, reservation),
        ),
    )


def intend_model_call(
    state: SessionState,
    reservation: ResourceReservation,
    *,
    request_id: str | None = None,
    input_text: str | None = None,
    manifest: object | None = None,
    policy_version: int | None = None,
    token_counter: str | None = None,
    model_id: str | None = None,
    cursor: int | None = None,
) -> SessionState:
    """Reserve one model-call bound before the provider is invoked."""
    if state.active_attempt is None:
        raise TransitionError("model call does not belong to an active attempt")
    if reservation.model_calls < 1:
        raise TransitionError("model reservation requires a positive model_calls bound")
    request_id = reservation.operation_id if request_id is None else request_id
    if request_id != reservation.operation_id:
        raise TransitionError("model request ID does not match reservation")
    if reservation.reservation_id in state.reservation_ids:
        raise TransitionError("reservation ID is already recorded")
    if any(
        item.operation_id == reservation.operation_id
        for item in state.ledger.reservations
    ):
        raise TransitionError("model operation ID is already recorded")
    ledger = state.ledger.reserve(reservation, state.task.budgets)
    return replace(
        state,
        ledger=ledger,
        reservation_ids=(*state.reservation_ids, reservation.reservation_id),
        model_operations=(
            *state.model_operations,
            ModelCallOperation(
                request_id,
                reservation,
                input_text,
                manifest,
                policy_version,
                token_counter,
                model_id,
                cursor,
            ),
        ),
    )


def settle_model_call(
    state: SessionState,
    reservation: ResourceReservation,
    measurement: UsageMeasurement | None,
) -> SessionState:
    """Record measured model usage, including malformed and failed calls."""
    if reservation.reservation_id not in state.reservation_ids:
        raise TransitionError("model reservation is not recorded")
    operation = model_operation(state, reservation.operation_id)
    if operation.reservation != reservation:
        raise TransitionError("model terminal reservation does not match intent")
    if operation.settled:
        raise TransitionError("model terminal is already recorded")
    ledger = state.ledger.settle(reservation, measurement)
    measurement_ids = state.measurement_ids
    if measurement is not None and measurement.measurement_id not in measurement_ids:
        measurement_ids = (*measurement_ids, measurement.measurement_id)
    return replace(
        state,
        ledger=ledger,
        measurement_ids=measurement_ids,
        model_operations=_replace_model_operation(
            state, replace(operation, settled=True)
        ),
    )


def model_operation(state: SessionState, request_id: str) -> ModelCallOperation:
    """Return the durable model intent or fail closed on an unknown request."""
    for operation in state.model_operations:
        if operation.request_id == request_id:
            return operation
    raise TransitionError("model terminal has no durable intent")


def _replace_model_operation(
    state: SessionState, updated: ModelCallOperation
) -> tuple[ModelCallOperation, ...]:
    return tuple(
        updated if item.request_id == updated.request_id else item
        for item in state.model_operations
    )


def interrupted_model_reservations(
    state: SessionState,
) -> tuple[ResourceReservation, ...]:
    """Return model reservations left active after a crash or unexpected failure."""
    runtime_ids = {item.reservation.reservation_id for item in state.operations}
    return tuple(
        item
        for item in state.ledger.active_reservations
        if item.model_calls >= 1 and item.reservation_id not in runtime_ids
    )


def record_evaluation_intake(
    state: SessionState, request_id: str, receipt_id: str, raw_receipt: str
) -> SessionState:
    """Record verified raw intake exactly once for its intended evaluation."""
    operation = _evaluation_operation(state, request_id)
    _require_new_intake(operation)
    if receipt_id in state.receipt_ids:
        raise TransitionError("receipt ID is already recorded")
    updated = replace(
        operation,
        status=EvaluationOperationStatus.INTAKE_RECORDED,
        receipt_id=receipt_id,
        raw_receipt=raw_receipt,
    )
    return replace(
        state,
        operations=_replace_operation(state, updated),
        receipt_ids=(*state.receipt_ids, receipt_id),
    )


def fail_evaluation_verification(
    state: SessionState,
    request_id: str,
    receipt_id: str,
    usage: UsageMeasurement | None = None,
) -> SessionState:
    """Make an evidence-verification failure terminal for that evaluation."""
    operation = _evaluation_operation(state, request_id)
    if operation.receipt_id != receipt_id:
        raise TransitionError("evaluation verification failure has no active intake")
    _require_consumable(operation)
    return _terminalize_evaluation(
        state,
        operation,
        EvaluationOperationStatus.VERIFICATION_FAILED,
        usage,
    )


def consume_evaluation(
    state: SessionState,
    receipt: EvaluationReceipt,
    disposition: PromotionDisposition,
    event_cursor: int,
) -> SessionState:
    """Consume an intake once, preserving late evidence without clearing other work."""
    operation = _evaluation_operation(state, receipt.request_id)
    if operation.receipt_id != receipt.receipt_id:
        raise TransitionError("evaluation receipt does not match its verified intake")
    _require_consumable(operation)
    result = _terminalize_evaluation(
        state,
        operation,
        EvaluationOperationStatus.RECEIPT_CONSUMED,
        receipt.usage,
    )
    result = replace(
        result,
        latest_evaluation=(operation.request, receipt, disposition),
    )
    if disposition is PromotionDisposition.ACCEPTED:
        return result
    return _close_attempt(
        result, operation.request.attempt_id, receipt, disposition, event_cursor
    )


def record_evaluation_usage(
    state: SessionState, request_id: str, usage: UsageMeasurement
) -> SessionState:
    """Apply a late evaluator measurement without changing evaluation outcome."""
    operation = _evaluation_operation(state, request_id)
    if operation.status is EvaluationOperationStatus.NOT_SUBMITTED:
        raise TransitionError("evaluation usage follows not-submitted operation")
    ledger = state.ledger.record_measurement(request_id, usage)
    measurement_ids = state.measurement_ids
    if usage.measurement_id not in measurement_ids:
        measurement_ids = (*measurement_ids, usage.measurement_id)
    return replace(state, ledger=ledger, measurement_ids=measurement_ids)


def reconcile_action(
    state: SessionState,
    proposal_id: str,
    result: ReconciliationStatus,
    receipt: ExecutionReceipt | None = None,
) -> SessionState:
    """Record one runtime recovery answer without disturbing other operations."""
    operation = action_operation(state, proposal_id)
    if (
        operation.reconciliation is result
        and operation.reconciliation_receipt == receipt
    ):
        return state
    if operation.status is not ActionOperationStatus.INTENDED:
        raise TransitionError("action reconciliation follows a terminal operation")
    if (result is ReconciliationStatus.RECEIPT) != (receipt is not None):
        raise TransitionError("reconciliation receipt does not match its result")
    if receipt is not None:
        validate_action_reconciliation_receipt(state, proposal_id, receipt)
    updated = replace(
        operation,
        reconciliation=result,
        reconciliation_receipt=receipt,
    )
    if result is ReconciliationStatus.NOT_SUBMITTED:
        updated = replace(
            updated,
            status=ActionOperationStatus.NOT_SUBMITTED,
            settled=True,
        )
        return _clear_matching_pending(
            replace(
                state,
                operations=_replace_operation(state, updated),
                ledger=state.ledger.release(operation.reservation),
            ),
            proposal_id,
        )
    return _wait_for_matching_pending(
        replace(state, operations=_replace_operation(state, updated)),
        proposal_id,
        f"runtime reconciliation: {result.value}",
    )


def reconcile_evaluation(
    state: SessionState,
    request_id: str,
    result: ReconciliationStatus,
    receipt: EvaluationReceipt | None = None,
) -> SessionState:
    """Record evaluator recovery facts and release only proved-unsent work."""
    operation = evaluation_operation(state, request_id)
    if (
        operation.reconciliation is result
        and operation.reconciliation_receipt == receipt
    ):
        return state
    if operation.status is not EvaluationOperationStatus.INTENDED:
        raise TransitionError("evaluation reconciliation follows a terminal operation")
    if (result is ReconciliationStatus.RECEIPT) != (receipt is not None):
        raise TransitionError("reconciliation receipt does not match its result")
    if receipt is not None:
        validate_evaluation_reconciliation_receipt(state, request_id, receipt)
    updated = replace(
        operation,
        reconciliation=result,
        reconciliation_receipt=receipt,
    )
    if result is ReconciliationStatus.NOT_SUBMITTED:
        updated = replace(updated, status=EvaluationOperationStatus.NOT_SUBMITTED)
        return _clear_matching_pending(
            replace(
                state,
                operations=_replace_operation(state, updated),
                ledger=state.ledger.release(operation.reservation),
            ),
            request_id,
        )
    return _wait_for_matching_pending(
        replace(state, operations=_replace_operation(state, updated)),
        request_id,
        f"evaluator reconciliation: {result.value}",
    )


def receive_action_receipt(
    state: SessionState, receipt: ExecutionReceipt
) -> SessionState:
    """Apply one runtime receipt through its owning action operation."""
    operation = action_operation(state, receipt.proposal_id)
    if receipt.receipt_id in state.receipt_ids:
        raise TransitionError("receipt ID is already recorded")
    if operation.status is ActionOperationStatus.NOT_SUBMITTED:
        raise TransitionError("receipt follows not-submitted action")
    if receipt.objective_version != operation.objective_version:
        raise TransitionError("receipt objective version disagrees with operation")
    if receipt.operation_id is not None and receipt.operation_id != receipt.proposal_id:
        raise TransitionError("receipt operation ID does not match its proposal")
    if _receipt_lacks_required_revision(operation, receipt):
        raise TransitionError("revision-capable runtime receipt lacks state revision")
    receipt_ids = (*state.receipt_ids, receipt.receipt_id)
    ledger = state.ledger
    if receipt.usage is not None:
        ledger = ledger.record_measurement(receipt.proposal_id, receipt.usage)
    measurement_ids = state.measurement_ids
    if (
        receipt.usage is not None
        and receipt.usage.measurement_id not in measurement_ids
    ):
        measurement_ids = (*measurement_ids, receipt.usage.measurement_id)
    state = replace(
        record_receipt_state(state, receipt.proposal_id, receipt.status.value),
        ledger=ledger,
        receipt_ids=receipt_ids,
        measurement_ids=measurement_ids,
    )
    active = state.active_attempt
    if active is not None and _receipt_updates_active_attempt(
        operation, active, receipt
    ):
        state = replace(
            state, active_attempt=replace(active, result_state=receipt.state)
        )
    receipts = (*operation.receipts, receipt)
    if operation.settled:
        return replace(
            state,
            operations=_replace_operation(state, replace(operation, receipts=receipts)),
        )
    if receipt.status in (ExecutionStatus.ACCEPTED, ExecutionStatus.RUNNING):
        return replace(
            state,
            operations=_replace_operation(
                state,
                replace(
                    operation, receipt_status=receipt.status.value, receipts=receipts
                ),
            ),
        )
    return replace(
        state,
        ledger=state.ledger.settle(operation.reservation, receipt.usage),
        operations=_replace_operation(
            state,
            replace(
                operation,
                status=ActionOperationStatus.RECEIPT_RECEIVED,
                receipt_status=receipt.status.value,
                settled=True,
                receipts=receipts,
            ),
        ),
    )


def validate_action_reconciliation_receipt(
    state: SessionState, proposal_id: str, receipt: ExecutionReceipt
) -> None:
    """Validate runtime receipt correlation before it enters a recovery event."""
    operation = action_operation(state, proposal_id)
    if receipt.proposal_id != proposal_id:
        raise TransitionError("reconciliation receipt does not match action")
    if receipt.objective_version != operation.objective_version:
        raise TransitionError("receipt objective version disagrees with operation")
    if receipt.operation_id is not None and receipt.operation_id != proposal_id:
        raise TransitionError("receipt operation ID does not match its proposal")
    if _receipt_lacks_required_revision(operation, receipt):
        raise TransitionError("revision-capable runtime receipt lacks state revision")


def _receipt_lacks_required_revision(
    operation: ActionOperation, receipt: ExecutionReceipt
) -> bool:
    if not operation.requires_revision:
        return False
    if receipt.status in (ExecutionStatus.ACCEPTED, ExecutionStatus.RUNNING):
        return False
    return receipt.state is None or receipt.state.revision is None


def _receipt_updates_active_attempt(
    operation: ActionOperation, active: Attempt | None, receipt: ExecutionReceipt
) -> bool:
    if operation.settled or receipt.state is None or active is None:
        return False
    if receipt.status in (ExecutionStatus.ACCEPTED, ExecutionStatus.RUNNING):
        return False
    return (
        active.objective_version == operation.objective_version
        and active.attempt_id == operation.attempt_id
    )


def validate_evaluation_reconciliation_receipt(
    state: SessionState, request_id: str, receipt: EvaluationReceipt
) -> None:
    """Validate evaluator receipt correlation before it enters a recovery event."""
    operation = evaluation_operation(state, request_id)
    if receipt.request_id != request_id:
        raise TransitionError("reconciliation receipt does not match evaluation")
    if receipt.objective_version != operation.request.objective_version:
        raise TransitionError(
            "evaluation receipt objective version disagrees with operation"
        )


def evaluation_operation(state: SessionState, request_id: str) -> EvaluationOperation:
    """Return one intended evaluation or reject an unknown request ID."""
    return _evaluation_operation(state, request_id)


def action_operation(state: SessionState, proposal_id: str) -> ActionOperation:
    """Return one durable runtime operation or reject an unknown proposal."""
    for operation in state.action_operations:
        if operation.proposal_id == proposal_id:
            return operation
    raise TransitionError("receipt does not link to an intended action")


def _evaluation_operation(state: SessionState, request_id: str) -> EvaluationOperation:
    for operation in state.evaluation_operations:
        if operation.request.request_id == request_id:
            return operation
    raise TransitionError("evaluation does not link to an intended operation")


def _terminalize_evaluation(
    state: SessionState,
    operation: EvaluationOperation,
    terminal_status: EvaluationOperationStatus,
    usage: UsageMeasurement | None = None,
) -> SessionState:
    updated = replace(operation, status=terminal_status)
    result = replace(
        state,
        operations=_replace_operation(state, updated),
        ledger=state.ledger.settle(operation.reservation, usage),
    )
    if state.pending_proposal_id != operation.request.request_id:
        return result
    status = (
        SessionStatus.RUNNING if state.status is SessionStatus.WAITING else state.status
    )
    return replace(result, status=status, pending_proposal_id=None, wait_reason=None)


def _clear_matching_pending(state: SessionState, operation_id: str) -> SessionState:
    """Clear the scheduling projection only when this operation still owns it."""
    if state.pending_proposal_id != operation_id:
        return state
    if state.status in (SessionStatus.PAUSED, SessionStatus.STOPPED):
        return replace(state, pending_proposal_id=None, wait_reason=None)
    return replace(
        state,
        status=SessionStatus.RUNNING,
        pending_proposal_id=None,
        wait_reason=None,
    )


def _wait_for_matching_pending(
    state: SessionState, operation_id: str, wait_reason: str
) -> SessionState:
    """Project an unresolved recovery result only for its still-current operation."""
    if state.pending_proposal_id != operation_id:
        return state
    if state.status in (SessionStatus.PAUSED, SessionStatus.STOPPED):
        return state
    return replace(state, status=SessionStatus.WAITING, wait_reason=wait_reason)


def _require_new_intake(operation: EvaluationOperation) -> None:
    if operation.status in {
        EvaluationOperationStatus.VERIFICATION_FAILED,
        EvaluationOperationStatus.NOT_SUBMITTED,
    }:
        raise TransitionError("evaluation intake follows verification failure")
    if operation.status is EvaluationOperationStatus.RECEIPT_CONSUMED:
        raise TransitionError("evaluation intake follows consumed receipt")
    if operation.status is EvaluationOperationStatus.INTAKE_RECORDED:
        raise TransitionError("evaluation intake is already recorded")


def _require_consumable(operation: EvaluationOperation) -> None:
    if operation.status in {
        EvaluationOperationStatus.VERIFICATION_FAILED,
        EvaluationOperationStatus.RECEIPT_CONSUMED,
    }:
        raise TransitionError("evaluation receipt is already terminal")
    if operation.status is not EvaluationOperationStatus.INTAKE_RECORDED:
        raise TransitionError("evaluation receipt has no verified intake")


def _replace_operation(
    state: SessionState, updated: OperationRecord
) -> tuple[OperationRecord, ...]:
    return tuple(
        (
            updated
            if isinstance(operation, type(updated))
            and _operation_id(operation) == _operation_id(updated)
            else operation
        )
        for operation in state.operations
    )


def _append_evaluation_request(
    history: tuple[AttemptHistory, ...], request: EvaluationRequest
) -> tuple[AttemptHistory, ...]:
    return tuple(
        (
            replace(
                item,
                evaluation_request_ids=(
                    *item.evaluation_request_ids,
                    request.request_id,
                ),
            )
            if item.attempt.attempt_id == request.attempt_id
            else item
        )
        for item in history
    )


def _operation_id(operation: OperationRecord) -> str:
    return (
        operation.proposal_id
        if isinstance(operation, ActionOperation)
        else operation.request.request_id
    )


def _operation_id_is_recorded(state: SessionState, operation_id: str) -> bool:
    """Return whether either external port already owns an operation ID."""
    return any(
        _operation_id(operation) == operation_id for operation in state.operations
    )


def apply_steer(
    state: SessionState, objective: str, success_evidence: str, event_cursor: int = 1
) -> SessionState:
    """Version task intent and close old active work without rewriting its history."""
    old_attempt = state.active_attempt
    if old_attempt is not None:
        old_attempt = replace(old_attempt, disposition=AttemptDisposition.ABANDONED)
    history = tuple(
        (
            replace(item, attempt=old_attempt, event_end_cursor=event_cursor)
            if old_attempt is not None
            and item.attempt.attempt_id == old_attempt.attempt_id
            and item.event_end_cursor is None
            else item
        )
        for item in state.attempt_history
    )
    return replace(
        state,
        task=state.task.steered(objective, success_evidence),
        status=SessionStatus.RUNNING,
        active_attempt=None,
        pending_proposal_id=None,
        wait_reason=None,
        blocking_question_id=None,
        attempt_history=history,
    )


def resume_session(state: SessionState) -> SessionState:
    """Apply a Resume command. Operator-answer waits cannot be skipped."""
    if state.status is SessionStatus.PAUSED:
        if state.pending_proposal_id is not None:
            return replace(state, status=SessionStatus.WAITING)
        if state.wait_reason == OPERATOR_ANSWER_WAIT:
            return replace(state, status=SessionStatus.WAITING)
        return replace(state, status=SessionStatus.RUNNING, wait_reason=None)
    if state.status is SessionStatus.WAITING and state.pending_proposal_id is not None:
        raise TransitionError("pending work cannot be resumed away from waiting")
    if (
        state.status is SessionStatus.WAITING
        and state.wait_reason == OPERATOR_ANSWER_WAIT
    ):
        raise TransitionError("operator answer pending")
    if state.status is SessionStatus.WAITING:
        return replace(state, status=SessionStatus.RUNNING, wait_reason=None)
    raise TransitionError("session_resumed is not a legal command transition")


def provider_recovery_waiting(
    state: SessionState,
    *,
    request_id: str,
    failure_class: str,
    consumed_calls: int,
    operator_action: bool,
    detail: str,
) -> SessionState:
    """Enter the durable provider-recovery wait once automatic recovery ends.

    The failed decision must already be settled: waiting burns no calls and
    never leaves an active model reservation behind. Resume, steering, or stop
    end the wait through their own existing transitions.
    """
    if state.status is not SessionStatus.RUNNING:
        raise TransitionError("provider recovery wait requires a running session")
    if state.pending_proposal_id is not None:
        raise TransitionError("provider recovery wait requires no pending proposal")
    if not request_id.strip():
        raise TransitionError("provider recovery wait requires a request ID")
    if failure_class not in MODEL_FAILURE_CLASSES:
        raise TransitionError("provider recovery wait has an unknown failure class")
    if isinstance(consumed_calls, bool) or not isinstance(consumed_calls, int):
        raise TransitionError("provider recovery consumed_calls must be an integer")
    if consumed_calls < 1:
        raise TransitionError("provider recovery consumed_calls must be positive")
    if not isinstance(operator_action, bool):
        raise TransitionError("provider recovery operator_action must be a boolean")
    if not detail.strip():
        raise TransitionError("provider recovery wait requires a detail")
    operation = state.model_operations[-1] if state.model_operations else None
    if operation is None or operation.request_id != request_id or not operation.settled:
        raise TransitionError(
            "provider recovery wait requires the failed decision settled"
        )
    return replace(
        state, status=SessionStatus.WAITING, wait_reason=PROVIDER_RECOVERY_WAIT
    )


def operator_evaluation_receipt(
    request: EvaluationRequest, decision: EvaluationDecision
) -> EvaluationReceipt:
    """Derive the operator-acceptance receipt for one decision.

    Receipt identity is deterministic from the request plus the decision
    content, so the live path and replay derive the same receipt. Accepted
    decisions require typed evidence: a milestone records ``evidence``, while a
    completed objective records ``completion_evidence``.
    """
    digest = hashlib.sha256(
        canonical_json(
            {
                "request_id": decision.request_id,
                "accepted": decision.accepted,
                "completed": decision.completed,
                "reason": decision.reason,
                "evidence": (
                    decision.evidence_ref.digest
                    if decision.evidence_ref is not None
                    else None
                ),
            }
        ).encode("utf-8")
    ).hexdigest()
    reference = (
        (decision.evidence_ref,)
        if decision.accepted and decision.evidence_ref is not None
        else ()
    )
    return EvaluationReceipt(
        receipt_id=f"operator-receipt-{digest[:32]}",
        accepted=decision.accepted,
        comparison="operator acceptance",
        evidence=() if decision.completed else reference,
        completed=decision.completed,
        completion_evidence=reference if decision.completed else (),
        **request.receipt_identity(),
    )


def promotion_disposition(
    state: SessionState, request: EvaluationRequest, receipt: EvaluationReceipt
) -> PromotionDisposition:
    """Check applicability; external evaluator remains sole score authority."""
    if not receipt.valid:
        return PromotionDisposition.INVALID
    if not all(receipt.hard_constraints.values()):
        return PromotionDisposition.HARD_CONSTRAINT_FAILED
    if not receipt.accepted:
        return PromotionDisposition.REJECTED
    if receipt.comparison == "incomparable":
        return PromotionDisposition.INCOMPARABLE
    current = state.task.objective_version
    current_head = state.lineage_head.node_id
    matching = (
        receipt.request_id == request.request_id,
        request.objective_version == current,
        receipt.objective_version == current,
        request.baseline_node_id == current_head,
        receipt.baseline_node_id == current_head,
        request.evaluator_version == receipt.evaluator_version,
        request.evaluated_state == receipt.evaluated_state,
    )
    if not all(matching):
        return PromotionDisposition.STALE
    return PromotionDisposition.ACCEPTED


def promote(
    state: SessionState,
    request: EvaluationRequest,
    receipt: EvaluationReceipt,
    node_id: str,
    event_cursor: int,
) -> SessionState:
    """Advance the committed lineage only from an applicable accepted evaluation."""
    if (
        promotion_disposition(state, request, receipt)
        is not PromotionDisposition.ACCEPTED
    ):
        raise TransitionError("evaluation is not applicable for promotion")
    if (
        state.active_attempt is None
        or state.active_attempt.attempt_id != request.attempt_id
    ):
        raise TransitionError(
            "evaluation request does not belong to the active attempt"
        )
    node = CommittedNode(
        node_id=node_id,
        objective_version=state.task.objective_version,
        state=receipt.evaluated_state,
        parent_id=state.lineage_head.node_id,
        evaluation_receipt_id=receipt.receipt_id,
        objectives=receipt.objectives,
    )
    status = (
        SessionStatus.COMPLETED
        if state.task.completion_mode.value == "finite"
        and receipt.completed
        and receipt.completion_evidence
        else SessionStatus.RUNNING
    )
    result = replace(
        state,
        lineage_head=node,
        active_attempt=None,
        pending_proposal_id=None,
        wait_reason=None,
        status=status,
        latest_evaluation=None,
        accepted_lineage=(*state.accepted_lineage, node),
    )
    return _close_attempt(
        result, request.attempt_id, receipt, PromotionDisposition.ACCEPTED, event_cursor
    )


def _close_attempt(
    state: SessionState,
    attempt_id: str,
    receipt: EvaluationReceipt,
    disposition: PromotionDisposition,
    event_cursor: int,
) -> SessionState:
    outcomes = {
        PromotionDisposition.ACCEPTED: AttemptDisposition.ACCEPTED,
        PromotionDisposition.REJECTED: AttemptDisposition.REJECTED,
        PromotionDisposition.STALE: AttemptDisposition.STALE,
        PromotionDisposition.INCOMPARABLE: AttemptDisposition.INCOMPARABLE,
        PromotionDisposition.INVALID: AttemptDisposition.INVALID,
        PromotionDisposition.HARD_CONSTRAINT_FAILED: (
            AttemptDisposition.HARD_CONSTRAINT_FAILED
        ),
    }
    history = tuple(
        _record_attempt_receipt(
            item, attempt_id, receipt, outcomes[disposition], event_cursor
        )
        for item in state.attempt_history
    )
    active_attempt = state.active_attempt
    if active_attempt is not None and active_attempt.attempt_id == attempt_id:
        active_attempt = None
    return replace(state, active_attempt=active_attempt, attempt_history=history)


def _record_attempt_receipt(
    history: AttemptHistory,
    attempt_id: str,
    receipt: EvaluationReceipt,
    disposition: AttemptDisposition,
    event_cursor: int,
) -> AttemptHistory:
    if history.attempt.attempt_id != attempt_id:
        return history
    receipt_ids = history.evaluation_receipt_ids
    if receipt.receipt_id not in receipt_ids:
        receipt_ids = (*receipt_ids, receipt.receipt_id)
    if history.event_end_cursor is not None:
        return replace(history, evaluation_receipt_ids=receipt_ids)
    return replace(
        history,
        attempt=replace(
            history.attempt,
            disposition=disposition,
            result_state=receipt.evaluated_state,
        ),
        event_end_cursor=event_cursor,
        evaluation_receipt_ids=receipt_ids,
    )


def record_receipt_state(
    state: SessionState, proposal_id: str, status: str
) -> SessionState:
    """Update only the pending wait projection for a matching receipt."""
    if state.pending_proposal_id != proposal_id:
        return state
    if state.status in (SessionStatus.PAUSED, SessionStatus.STOPPED):
        if status in ("accepted", "running"):
            return state
        return replace(state, pending_proposal_id=None, wait_reason=None)
    if status in ("accepted", "running"):
        return replace(
            state, status=SessionStatus.WAITING, wait_reason="runtime receipt pending"
        )
    return replace(
        state, status=SessionStatus.RUNNING, pending_proposal_id=None, wait_reason=None
    )
