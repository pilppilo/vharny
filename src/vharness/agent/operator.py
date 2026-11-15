"""Live operator observation over durable session state."""

# pylint: disable=too-many-instance-attributes
# The surface names each observable band rather than a presentation blob.

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .errors import ContractError
from .memory import known_evidence_ids, working_memory
from .models import OPERATOR_ANSWER_WAIT, Event, SessionStatus
from .transitions import SessionState

_QUESTION_LIMIT = 8
_STREAM_LIMIT = 24
_LIVE_STATUSES = frozenset(
    {SessionStatus.CREATED, SessionStatus.RUNNING, SessionStatus.WAITING}
)
_STREAM_KINDS = frozenset(
    {
        "action_intended",
        "evaluation_intended",
        "objective_steered",
        "receipt_received",
        "evaluation_received",
        "operator_message",
        "lineage_promoted",
    }
)


@dataclass(frozen=True, slots=True)
class OperatorView:
    """What a human operator needs without a second workflow engine."""

    session_id: str
    status: SessionStatus
    objective: str
    objective_version: int
    lineage_head: str
    active_attempt_id: str | None
    pending_proposal_id: str | None
    wait_reason: str | None
    budgets: str
    remaining_budgets: str
    current_plan: str
    operator_direction: str | None
    questions: tuple[str, ...]
    action_receipt_stream: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    acknowledged_steering: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.objective.strip():
            raise ContractError("operator view requires session and objective")


def build_operator_view(
    session_id: str, state: SessionState, events: tuple[Event, ...]
) -> OperatorView:
    """Project a live operator surface from journal facts."""
    working = working_memory(state, events)
    stream: list[str] = []
    steering: list[str] = []
    for event in events:
        item = _stream_detail(event)
        if item is None:
            item = _supervisor_notice(event)
        if item is not None:
            stream.append(item)
        acknowledged = _acknowledged_steering(event)
        if acknowledged is not None:
            steering.append(acknowledged)
    runner_exit = runner_exit_reason(events)
    status = state.status
    if runner_exit is not None and status in _LIVE_STATUSES:
        # The runner gave up before the session reached a terminal or paused
        # state: the local schedule cannot progress, so the operator surface
        # reports the dead run as failed even though the durable projection
        # stays non-terminal for resume/replay.
        status = SessionStatus.FAILED
    return OperatorView(
        session_id=session_id,
        status=status,
        objective=state.task.objective,
        objective_version=state.task.objective_version,
        lineage_head=state.lineage_head.node_id,
        active_attempt_id=working.active_attempt_id,
        pending_proposal_id=working.pending_proposal_id,
        wait_reason=working.wait_reason,
        budgets=working.budgets,
        remaining_budgets=working.budgets,
        current_plan=working.current_plan,
        operator_direction=working.operator_direction,
        questions=_unanswered_questions(state, events)[-_QUESTION_LIMIT:],
        action_receipt_stream=tuple(stream[-_STREAM_LIMIT:]),
        evidence_ids=tuple(sorted(known_evidence_ids(state, events))),
        acknowledged_steering=tuple(steering[-_QUESTION_LIMIT:]),
    )


def runner_exit_reason(events: tuple[Event, ...]) -> str | None:
    """Return the recorded runner-exit reason when it is the journal tail.

    A bounded runner that gives up without a terminal session status journals
    runner_exited as its last fact; events after it mean the run resumed, so
    the exit is history rather than the current state.
    """
    if not events or events[-1].kind != "runner_exited":
        return None
    reason = events[-1].payload.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason
    return "reason not recorded"


def _unanswered_questions(
    state: SessionState, events: tuple[Event, ...]
) -> tuple[str, ...]:
    """Return the blocking question as id plus text, if still unanswered."""
    open_id = state.blocking_question_id
    if open_id is None or state.wait_reason != OPERATOR_ANSWER_WAIT:
        return ()
    text = None
    for event in events:
        if (
            event.kind == "question_recorded"
            and event.payload.get("question_id") == open_id
        ):
            recorded = event.payload.get("text")
            if isinstance(recorded, str) and recorded.strip():
                text = recorded
        if (
            event.kind in {"question_answered", "question_retired"}
            and event.payload.get("question_id") == open_id
        ):
            return ()
    if text is None:
        return ()
    return (f"{open_id}: {text}",)


def _stream_detail(  # pylint: disable=too-many-return-statements
    event: Event,
) -> str | None:
    """Render action/receipt detail instead of event-kind labels."""
    payload = event.payload
    if event.kind == "action_intended":
        proposal = payload.get("proposal")
        if isinstance(proposal, Mapping):
            name = proposal.get("action_name")
            arguments = proposal.get("arguments", {})
            return f"ACTION {name} {_compact_arguments(arguments)}"
    if event.kind == "receipt_received":
        receipt = payload.get("receipt")
        if isinstance(receipt, Mapping):
            status = receipt.get("status")
            detail = receipt.get("error") or receipt.get("operation_id") or ""
            return f"RUNTIME {status} {detail}".strip()
    if event.kind == "evaluation_intended":
        return "EVALUATION requested"
    if event.kind == "evaluation_received":
        receipt = payload.get("receipt")
        if isinstance(receipt, Mapping):
            accepted = "accepted" if receipt.get("accepted") else "rejected"
            return f"EVALUATION {accepted} comparison={receipt.get('comparison')}"
    if event.kind == "operator_message":
        text = _mapping_text(payload.get("command"), "text")
        if text is not None:
            return f"OPERATOR {text}"
    if event.kind == "objective_steered":
        objective = _mapping_text(payload.get("command"), "objective")
        if objective is not None:
            return f"STEER {objective}"
    if event.kind == "lineage_promoted":
        return "LINEAGE promoted"
    return None


def _supervisor_notice(event: Event) -> str | None:
    """Render one advisory supervision notice.

    The notice is display only: it never dispatches guidance, and the session
    still applies any guidance through the ordinary event path.
    """
    payload = event.payload
    notice: str | None
    if event.kind == "supervision_triggered":
        cause = payload.get("cause")
        call = payload.get("call")
        notice = f"SUPERVISOR trigger cause={cause} call={call}"
    elif event.kind == "guidance_received":
        diagnosis = _mapping_text(payload.get("guidance"), "diagnosis")
        notice = f"GUIDANCE {diagnosis}" if diagnosis else "GUIDANCE received"
    elif event.kind in {"guidance_accepted", "guidance_rejected"}:
        decision = payload.get("decision")
        reason = payload.get("reason")
        notice = f"GUIDANCE {decision} reason={reason}"
    elif event.kind == "guidance_expired":
        notice = "GUIDANCE expired"
    elif event.kind == "guidance_suppressed":
        notice = f"GUIDANCE suppressed reason={payload.get('reason')}"
    elif event.kind == "supervisor_failed":
        notice = f"SUPERVISOR failed {payload.get('error')}".strip()
    elif event.kind == "checkpoint_suppressed":
        notice = (
            f"SUPERVISOR checkpoint suppressed call={payload.get('call')} "
            f"cause={payload.get('cause')}"
        )
    elif event.kind == "operator_escalated":
        stage = payload.get("stage")
        cause = payload.get("cause")
        reason = payload.get("reason")
        suffix = f" reason={reason}" if reason else ""
        notice = f"SUPERVISOR escalation stage={stage} cause={cause}{suffix}"
    elif event.kind == "provider_recovery_waiting":
        operator_action = payload.get("operator_action")
        suffix = " operator-action" if operator_action is True else ""
        notice = (
            f"RECOVERY waiting class={payload.get('failure_class')} "
            f"calls={payload.get('consumed_calls')}{suffix}"
        )
    else:
        notice = None
    return notice


def _compact_arguments(value: object) -> str:
    if not isinstance(value, Mapping):
        return "{}"
    items = ", ".join(f"{key}={value[key]!r}" for key in sorted(value))
    return "{" + items + "}"


def _acknowledged_steering(event: Event) -> str | None:
    """Return applied message or steer text; admission alone is not acknowledgement."""
    if event.kind not in {"operator_message", "objective_steered"}:
        return None
    command = event.payload.get("command")
    if event.kind == "operator_message":
        return _mapping_text(command, "text")
    return _mapping_text(command, "objective")


def _mapping_text(value: object, name: str) -> str | None:
    if not isinstance(value, Mapping):
        return None
    field = value.get(name)
    if isinstance(field, str) and field.strip():
        return field
    return None
