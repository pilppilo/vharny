"""Kernel coverage for the ADR-0014 switch to ``monitor.p6.v1``.

The shipped default composes the retained EXP-0001 repetition policy with the
EXP-0002 R1/R2 evidence-repeat fingerprints and replaces the B2 claim-free
burn trigger with the ADR-0013 supported-progress budget checkpoint: ledger-
based consumed-call counting, an anchor reset only by novel supported
progress, the shared four-call cooldown with repetition precedence, and the
consumed/threshold/anchor journaling the replay validator accepts. The
retained ``monitor_policy_p5`` and ``monitor_policy_p1`` selections keep
reproducing historical journals under their recorded semantics (ADR-0014
decision 4).
"""

from __future__ import annotations

from typing import Mapping

import pytest

from vharness.agent import (
    ActionChoice,
    ActionDefinition,
    BudgetLimits,
    CompletionMode,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError, TransitionError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ActionProposal,
    ComponentIdentity,
    EnvironmentContract,
    Event,
    EvaluationReceipt,
    ExecutionReceipt,
    ExecutionStatus,
    Message,
    RuntimeCapabilities,
    StateRef,
    UsageMeasurement,
)
from vharness.agent.replay import replay
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.supervision import (
    CHECKPOINT_CAUSE,
    MONITOR_POLICY_P5_VERSION,
    MONITOR_POLICY_VERSION,
    Guidance,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
    checkpoint_suppression,
    evaluate_supervision,
    facts_history,
    monitor_policy_p1,
    monitor_policy_p5,
    policy_from_payload,
    policy_payload,
)

_SESSION_ID = "session-p6"
_RECORDED_AT = "2026-11-06T00:00:00+00:00"


def _event(sequence: int, kind: str, payload: Mapping[str, object]) -> Event:
    return Event(
        event_id=f"event-{sequence}",
        session_id=_SESSION_ID,
        sequence=sequence,
        kind=kind,
        objective_version=1,
        payload=payload,
        recorded_at=_RECORDED_AT,
    )


def _decision(
    sequence: int,
    *,
    calls: int = 1,
    consult: bool = False,
    unknown_consult: bool = False,
    knowledge: tuple[str, int] | None = None,
    pending_claim: str | None = None,
    evidence: tuple[str, ...] | None = None,
) -> tuple[list[Event], int]:
    """Build one decision segment with labeled ledger and progress inputs.

    ``calls`` is the settled model-call count journaled on the terminal (the
    main call including retries and repairs). ``consult`` appends a settled
    supervisor consult charged to the same budget; ``unknown_consult`` appends
    one settled with unknown usage, which must add nothing. ``knowledge`` is a
    (statement, item index) supported knowledge record — the index only varies
    the item ID, so equal statements stay equal progress identities.
    ``pending_claim`` records a claim that never assesses to supported.
    ``evidence`` journals fresh R1/R2 digests on the decision's receipt.
    """
    events = [
        _event(sequence, "model_intended", {"request_id": f"decision-{sequence}"}),
        _event(
            sequence + 1,
            "model_decided",
            {"result_kind": "ActionChoice", "usage": {"model_calls": calls}},
        ),
    ]
    cursor = sequence + 2
    if consult:
        events.append(
            _event(
                cursor,
                "guidance_received",
                {"request_id": f"consult-{cursor}", "usage": {"model_calls": 1}},
            )
        )
        cursor += 1
    if unknown_consult:
        events.append(
            _event(
                cursor,
                "supervisor_failed",
                {"request_id": f"consult-{cursor}", "error": "supervisor failed"},
            )
        )
        cursor += 1
    if knowledge is not None:
        statement, index = knowledge
        events.append(
            _event(
                cursor,
                "knowledge_recorded",
                {
                    "item": {
                        "item_id": f"item-{statement}-{index}",
                        "statement": statement,
                        "scope": "fixture",
                        "status": "supported",
                    }
                },
            )
        )
        cursor += 1
    if pending_claim is not None:
        events.append(
            _event(
                cursor,
                "progress_claim_recorded",
                {
                    "claim": {
                        "claim_id": f"claim-{cursor}",
                        "category": "knowledge",
                        "before": "",
                        "after": pending_claim,
                        "scope": "fixture",
                        "status": "pending",
                    }
                },
            )
        )
        cursor += 1
    if evidence is not None:
        proposal: dict[str, object] = {
            "proposal_id": f"proposal-{sequence}",
            "action_name": "exec",
            "arguments": {"command": f"probe-{sequence}"},
        }
        events.extend(
            (
                _event(cursor, "action_intended", {"proposal": proposal}),
                _event(
                    cursor + 1,
                    "receipt_received",
                    {
                        "proposal": proposal,
                        "receipt": {
                            "proposal_id": f"proposal-{sequence}",
                            "status": "succeeded",
                        },
                        "evidence_fingerprints": list(evidence),
                    },
                ),
            )
        )
        cursor += 2
    return events, cursor


def _journal(*decisions: list[Event]) -> tuple[Event, ...]:
    return tuple(event for decision in decisions for event in decision)


def test_default_policy_is_the_adopted_p6_composition() -> None:
    policy = MonitorPolicy()
    assert MONITOR_POLICY_VERSION == "monitor.p6.v1"
    assert policy.version == MONITOR_POLICY_VERSION
    assert (policy.window_calls, policy.repeat_threshold, policy.cooldown_calls) == (
        8,
        3,
        4,
    )
    assert policy.evidence_fingerprints is True
    assert policy.burn_trigger is False
    assert policy.checkpoint_trigger is True
    assert policy.burn_fraction == 0.6


def test_retained_p5_policy_keeps_the_adr_0011_composition() -> None:
    policy = monitor_policy_p5()
    assert policy.version == MONITOR_POLICY_P5_VERSION
    assert policy.burn_trigger is True
    assert policy.checkpoint_trigger is False


def test_retained_p1_policy_keeps_every_consumed_call_family_off() -> None:
    policy = monitor_policy_p1()
    assert policy.burn_trigger is False
    assert policy.checkpoint_trigger is False


def test_policy_payload_round_trips_the_p6_composition() -> None:
    policy = MonitorPolicy()
    assert policy_from_payload(policy_payload(policy)) == policy
    assert policy_payload(policy)["checkpoint_trigger"] is True


def test_payload_without_checkpoint_flag_parses_as_the_p5_composition() -> None:
    legacy = {
        "version": MONITOR_POLICY_P5_VERSION,
        "window_calls": 8,
        "repeat_threshold": 3,
        "cooldown_calls": 4,
        "evidence_fingerprints": True,
        "burn_trigger": True,
        "burn_fraction": 0.6,
    }
    assert policy_from_payload(legacy) == monitor_policy_p5()


def test_policy_validation_rejects_inconsistent_compositions() -> None:
    with pytest.raises(ContractError):
        MonitorPolicy(burn_trigger=True)
    with pytest.raises(ContractError):
        MonitorPolicy(burn_trigger=True, checkpoint_trigger=True)
    with pytest.raises(ContractError):
        MonitorPolicy(burn_fraction=0.0)
    with pytest.raises(ContractError):
        MonitorPolicy(checkpoint_trigger="yes")  # type: ignore[arg-type]


def test_checkpoint_fires_at_the_predeclared_boundary() -> None:
    decisions = [_decision(sequence)[0] for sequence in range(1, 18, 3)]
    assert (
        evaluate_supervision(_journal(*decisions[:5]), call=6, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(_journal(*decisions), call=7, model_call_budget=10)
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.fingerprint is None
    assert trigger.repeat_count is None
    assert trigger.consumed_calls == 6
    assert trigger.threshold == 6
    assert trigger.progress_anchor == 0
    assert trigger.actions_used == 0
    assert set(trigger.input_event_ids) == {
        event.event_id for event in _journal(*decisions)
    }


def test_novel_supported_progress_resets_the_anchor_exactly_once() -> None:
    first, cursor = _decision(1, knowledge=("finding one", 1))
    rest = [_decision(cursor + 3 * index)[0] for index in range(1, 9)]
    decisions = [first, *rest]
    # Threshold ceil(0.6 * 10) = 6: the supported knowledge at decision 1 sets
    # the anchor to 1, so the checkpoint is due at consumed 7 (call 8) and no
    # earlier boundary.
    assert (
        evaluate_supervision(_journal(*decisions[:6]), call=7, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(
        _journal(*decisions[:7]), call=8, model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.consumed_calls == 7
    assert trigger.progress_anchor == 1


def test_duplicate_support_never_resets_the_anchor() -> None:
    # The same supported statement re-recorded every decision under a fresh
    # item ID: the first support resets the anchor, the repeats never do.
    decisions = [
        _decision(sequence, knowledge=("the same finding", index))[0]
        for index, sequence in enumerate(range(1, 23, 3))
    ]
    assert (
        evaluate_supervision(_journal(*decisions[:6]), call=7, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(
        _journal(*decisions[:7]), call=8, model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    # Consumed 7 at the boundary minus the decision-1 anchor of 1 is exactly
    # the threshold: the six duplicate supports moved nothing.
    assert trigger.consumed_calls == 7
    assert trigger.progress_anchor == 1


def test_pending_claims_never_reset_the_anchor() -> None:
    decisions = [
        _decision(sequence, pending_claim=f"self-reported guess {sequence}")[0]
        for sequence in range(1, 18, 3)
    ]
    assert (
        evaluate_supervision(_journal(*decisions[:5]), call=6, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(_journal(*decisions), call=7, model_call_budget=10)
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.progress_anchor == 0


def test_evidence_novelty_does_not_reset_the_anchor() -> None:
    # The retained run4 shape at fixture scale: novel stdout on every call
    # with no supported progress. P6 must checkpoint; the retained P5
    # evidence-novelty reset stays silent on the same journal.
    decisions = [
        _decision(
            sequence,
            evidence=(
                f"stdout-strict:{index:064x}",
                f"stdout-normalized:{index:064x}",
            ),
        )[0]
        for index, sequence in enumerate(range(1, 24, 3))
    ]
    assert (
        evaluate_supervision(_journal(*decisions[:5]), call=6, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(
        _journal(*decisions[:6]), call=7, model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.progress_anchor == 0
    assert (
        evaluate_supervision(
            _journal(*decisions[:6]),
            call=7,
            policy=monitor_policy_p5(),
            model_call_budget=10,
        )
        is None
    )
    assert (
        evaluate_supervision(
            _journal(*decisions[:7]),
            call=8,
            policy=monitor_policy_p5(),
            model_call_budget=10,
        )
        is None
    )


def test_consumed_calls_count_ledger_settlement_not_decision_manifests() -> None:
    # Three dispatched calls per decision (two labeled retries): the
    # checkpoint is due at consumed 6, which is decision 2, not decision 6.
    decisions = [_decision(sequence, calls=3)[0] for sequence in range(1, 12, 3)]
    assert (
        evaluate_supervision(_journal(*decisions[:1]), call=2, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(
        _journal(*decisions[:2]), call=3, model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.consumed_calls == 6
    assert trigger.threshold == 6


def test_settled_supervisor_consults_are_charged_to_the_budget() -> None:
    # Two consumed calls per decision (main + settled consult); the third
    # decision's consult settled with unknown usage and must add nothing, so
    # the threshold is reached at consumed 7 (call 5), not consumed 6.
    decisions = [_decision(sequence, consult=True)[0] for sequence in range(1, 18, 3)]
    decisions[2] = _decision(7, unknown_consult=True)[0]
    assert (
        evaluate_supervision(_journal(*decisions[:3]), call=4, model_call_budget=10)
        is None
    )
    trigger = evaluate_supervision(
        _journal(*decisions[:4]), call=5, model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.consumed_calls == 7
    assert trigger.progress_anchor == 0


def test_checkpoint_shares_the_repetition_cooldown() -> None:
    decisions = [_decision(sequence)[0] for sequence in range(1, 24, 3)]
    trigger_event = _event(
        98,
        "supervision_triggered",
        {
            "call": 4,
            "policy_version": MONITOR_POLICY_VERSION,
            "cause": "repetition",
        },
    )
    # A recorded trigger at call 4 (budget 4 -> threshold 3): call 5 is inside
    # the shared cooldown, call 8 re-arms and the due checkpoint fires.
    assert (
        evaluate_supervision(
            _journal(*decisions[:4]) + (trigger_event,),
            call=5,
            model_call_budget=4,
        )
        is None
    )
    trigger = evaluate_supervision(
        _journal(*decisions[:7]) + (trigger_event,), call=8, model_call_budget=4
    )
    assert trigger is not None
    assert trigger.cause == CHECKPOINT_CAUSE
    assert trigger.consumed_calls == 7


def test_checkpoint_suppression_reports_a_due_suppressed_boundary() -> None:
    """ADR-0013: a due checkpoint inside the cooldown reports its disposition.

    ``evaluate_supervision`` returns ``None`` on the shared cooldown before
    the checkpoint is considered; the suppression predicate reports exactly
    that case with the ledger state replay reproduces it from, and reports
    nothing when the checkpoint is absent, not due, or past the cooldown.
    """
    decisions = [_decision(sequence)[0] for sequence in range(1, 24, 3)]
    trigger_event = _event(
        98,
        "supervision_triggered",
        {
            "call": 4,
            "policy_version": MONITOR_POLICY_VERSION,
            "cause": "repetition",
        },
    )
    events = _journal(*decisions[:4]) + (trigger_event,)
    assert evaluate_supervision(events, call=5, model_call_budget=4) is None
    suppression = checkpoint_suppression(events, call=5, model_call_budget=4)
    assert suppression is not None
    assert suppression.policy_version == MONITOR_POLICY_VERSION
    assert suppression.call == 5
    assert suppression.cause == CHECKPOINT_CAUSE
    assert suppression.consumed_calls == 4
    assert suppression.threshold == 3
    assert suppression.progress_anchor == 0
    assert suppression.previous_trigger_call == 4
    assert suppression.cooldown_calls == 4

    # Without a recorded trigger the boundary is not suppressed: the due
    # checkpoint itself fires.
    plain = _journal(*decisions[:4])
    assert checkpoint_suppression(plain, call=5, model_call_budget=4) is None
    trigger = evaluate_supervision(plain, call=5, model_call_budget=4)
    assert trigger is not None and trigger.cause == CHECKPOINT_CAUSE
    # Past the cooldown the due checkpoint fires instead of being suppressed.
    assert (
        checkpoint_suppression(
            _journal(*decisions[:7]) + (trigger_event,), call=8, model_call_budget=4
        )
        is None
    )
    # Inside the cooldown but not yet due (budget 10 -> threshold 6): nothing.
    assert checkpoint_suppression(events, call=5, model_call_budget=10) is None
    # The retained P5 burn and P1 compositions keep their unjournaled
    # suppression semantics (ADR-0014 decision 4).
    assert (
        checkpoint_suppression(
            events, call=5, policy=monitor_policy_p5(), model_call_budget=4
        )
        is None
    )
    assert (
        checkpoint_suppression(
            events, call=5, policy=monitor_policy_p1(), model_call_budget=4
        )
        is None
    )


def test_repetition_takes_precedence_over_the_checkpoint() -> None:
    fingerprint = f"stdout-strict:{'e' * 64}"
    decisions = [
        _decision(sequence, evidence=(fingerprint, fingerprint))[0]
        for sequence in range(1, 12, 3)
    ]
    trigger = evaluate_supervision(
        _journal(*decisions[:3]), call=4, model_call_budget=4
    )
    assert trigger is not None
    assert trigger.cause == "repetition"
    assert trigger.fingerprint == fingerprint
    assert trigger.consumed_calls is None


def test_checkpoint_refuses_to_evaluate_without_a_budget() -> None:
    decisions = [_decision(sequence)[0] for sequence in range(1, 8, 3)]
    with pytest.raises(ContractError):
        evaluate_supervision(_journal(*decisions), call=3)


def test_facts_carry_the_ledger_and_progress_inputs() -> None:
    first, cursor = _decision(1, calls=3, knowledge=("finding one", 1))
    second, cursor = _decision(cursor, pending_claim="a guess")
    duplicate, _ = _decision(cursor, knowledge=("finding one", 99))
    history = facts_history(_journal(first, second, duplicate))
    assert [fact.consumed_calls for fact in history] == [3, 1, 1]
    assert len(history[0].progress_digests) == 1
    assert history[0].progress_digests[0].startswith("progress:")
    assert history[0].supported_progress == 1
    assert history[1].progress_digests == ()
    assert history[1].supported_progress == 0
    # The content identity ignores the item ID: re-supporting the same
    # statement under a fresh ID yields the same progress digest.
    assert history[2].progress_digests == history[0].progress_digests


class _ExecEnvironment:
    """Mechanical contract exposing one inspect action."""

    # pylint: disable=too-few-public-methods
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="p6",
            version="1",
            initial_state=StateRef("p6", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect the scenario state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _ExecRuntime:
    """Runtime recording each inspect proposal without artifacts."""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self.proposals: list[ActionProposal] = []

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("p6-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self.proposals.append(proposal)
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("p6", f"after-{proposal.proposal_id}", digest="1" * 64),
            usage=UsageMeasurement(f"receipt-{proposal.proposal_id}-usage", actions=1),
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _Evaluator:
    """Evaluator that never accepts; the scenario measures signals, not score."""

    # pylint: disable=too-few-public-methods
    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("p6-evaluator", "v1")

    def evaluate(self, request) -> EvaluationReceipt:
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=False,
        )

    def reconcile(self, request_id) -> EvaluationReconciliationResult:
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@pytest.fixture
def dependencies(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield (journal, store, _ExecEnvironment(), _ExecRuntime(store), _Evaluator())
    journal.close()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="p6-session-task",
        objective="exercise the supported-progress checkpoint",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=12, evaluations=2, model_calls=12),
        environment_id="p6",
        evaluation_contract_id="p6-evaluator-v1",
    )


def _guidance() -> SupervisorResponse:
    return SupervisorResponse(
        Guidance(
            diagnosis="spending review due without supported progress",
            constraints=("review the budget",),
            suggested_experiments=("change direction or accept the cost",),
            expires_after_call=8,
        ),
        UsageMeasurement("p6-supervisor-usage", model_calls=1),
        "p6-supervisor",
    )


def test_p6_checkpoint_persists_guides_and_replays(dependencies) -> None:
    """The checkpoint cause persists, guides, and replays from the journal.

    The payload journals the ledger inputs the replay validator accepts, and
    the run survives the advisory trigger.
    """
    policy = MonitorPolicy(
        window_calls=4,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=True,
        burn_trigger=False,
        checkpoint_trigger=True,
        burn_fraction=0.1,
    )
    supervisor = ScriptedSupervisor((_guidance(),))
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "p6-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(supervisor, policy),
    )
    # Distinct inspect arguments keep action fingerprints distinct, so the
    # checkpoint family is the unambiguous trigger cause at consumed 2
    # (ceil(0.1 * 12) = 2).
    for index in range(4):
        session.step(
            ScriptedModel(
                (ActionChoice("inspect", "look around", {"target": f"target-{index}"}),)
            )
        )
    events = session.events()
    kinds = [event.kind for event in events]
    assert kinds.count("supervision_triggered") == 1
    trigger = next(event for event in events if event.kind == "supervision_triggered")
    assert trigger.payload["cause"] == CHECKPOINT_CAUSE
    assert trigger.payload["fingerprint"] is None
    assert trigger.payload["repeat_count"] is None
    assert trigger.payload["call"] == 3
    assert trigger.payload["consumed_calls"] == 2
    assert trigger.payload["threshold"] == 2
    assert trigger.payload["progress_anchor"] == 0
    assert "guidance_received" in kinds
    # The run survives the trigger: the boundary still reaches a decision.
    assert kinds.count("model_decided") == 4

    reopened = Session.open(
        "p6-session",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(ScriptedSupervisor(()), policy),
    )
    reopened_kinds = [event.kind for event in reopened.events()]
    assert reopened_kinds.count("supervision_triggered") == 1
    assert "guidance_received" in reopened_kinds


def test_unreviewable_checkpoint_escalates_to_an_operator_question(
    dependencies,
) -> None:
    """ADR-0013: a checkpoint the remaining budget cannot review is a question.

    With the supervisor consult unaffordable, the boundary exposes the due
    checkpoint as a blocking operator question instead of silently skipping
    it or ending the run; answering resumes, and the exhausted budget then
    ends the later step with its typed error.
    """
    policy = MonitorPolicy(
        window_calls=4,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=True,
        burn_trigger=False,
        checkpoint_trigger=True,
        burn_fraction=0.6,
    )
    supervisor = ScriptedSupervisor((_guidance(),))
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "p6-unreviewable",
        TaskSpec(
            task_id="p6-unreviewable-task",
            objective="exercise the unreviewable checkpoint",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=12, evaluations=2, model_calls=3),
            environment_id="p6",
            evaluation_contract_id="p6-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(supervisor, policy),
    )
    # Distinct inspect arguments keep action fingerprints distinct, so the
    # checkpoint family is the unambiguous trigger cause at consumed 2
    # (ceil(0.6 * 3) = 2), where the single remaining call cannot also fund
    # the supervisor consult.
    for index in range(3):
        session.step(
            ScriptedModel(
                (ActionChoice("inspect", "look around", {"target": f"target-{index}"}),)
            )
        )
    events = session.events()
    kinds = [event.kind for event in events]
    assert session.view().status is SessionStatus.WAITING
    assert "supervisor_failed" not in kinds
    assert "guidance_received" not in kinds
    assert supervisor.calls == 0
    escalated = next(event for event in events if event.kind == "operator_escalated")
    assert escalated.payload["stage"] == 1
    assert escalated.payload["cause"] == CHECKPOINT_CAUSE
    assert escalated.payload["reason"] == (
        "no supervisor call fits the remaining model-call budget"
    )
    question = next(event for event in events if event.kind == "question_recorded")
    assert "stage 1" in question.payload["text"]
    assert (
        "no supervisor call fits the remaining model-call budget"
        in question.payload["text"]
    )

    # The operator answers; the last call funds one more decision, and the
    # exhausted budget then ends the following step with its typed error.
    session.enqueue(Message("msg-operator", "p6-unreviewable", "continue"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    session.step(
        ScriptedModel(
            (ActionChoice("inspect", "look around", {"target": "target-resumed"}),)
        )
    )
    with pytest.raises(TransitionError):
        session.step(
            ScriptedModel(
                (ActionChoice("inspect", "look around", {"target": "target-final"}),)
            )
        )


def test_suppressed_checkpoints_journal_their_disposition(dependencies) -> None:
    """ADR-0013: due checkpoints inside the cooldown journal their disposition.

    The checkpoint fires once and guides; while the shared cooldown keeps the
    still-due checkpoint from re-firing, each boundary journals
    ``checkpoint_suppressed`` with the ledger state replay reproduces, and
    the run proceeds without a trigger until the budget runs out.
    """
    policy = MonitorPolicy(
        window_calls=4,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=True,
        burn_trigger=False,
        checkpoint_trigger=True,
        burn_fraction=0.6,
    )
    supervisor = ScriptedSupervisor((_guidance(),))
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "p6-suppressed",
        TaskSpec(
            task_id="p6-suppressed-task",
            objective="exercise the suppressed checkpoint disposition",
            completion_mode=CompletionMode.FINITE,
            success_evidence="external evaluator acceptance",
            budgets=BudgetLimits(actions=12, evaluations=2, model_calls=8),
            environment_id="p6",
            evaluation_contract_id="p6-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(supervisor, policy),
    )
    for index in range(7):
        session.step(
            ScriptedModel(
                (ActionChoice("inspect", "look around", {"target": f"target-{index}"}),)
            )
        )
    with pytest.raises(TransitionError):
        session.step(
            ScriptedModel(
                (ActionChoice("inspect", "look around", {"target": "target-final"}),)
            )
        )
    events = session.events()
    kinds = [event.kind for event in events]
    # Threshold ceil(0.6 * 8) = 5: the checkpoint fires at call 6 (whose
    # consult and decision charge two calls), and the still-due boundaries
    # at calls 7 and 8 are cooldown-suppressed — the eighth journals before
    # the exhausted budget raises.
    assert kinds.count("supervision_triggered") == 1
    suppressed = [event for event in events if event.kind == "checkpoint_suppressed"]
    assert [event.payload["call"] for event in suppressed] == [7, 8]
    assert [event.payload["consumed_calls"] for event in suppressed] == [7, 8]
    assert all(event.payload["threshold"] == 5 for event in suppressed)
    assert all(event.payload["progress_anchor"] == 0 for event in suppressed)
    assert all(event.payload["previous_trigger_call"] == 6 for event in suppressed)
    assert all(event.payload["cooldown_calls"] == 4 for event in suppressed)
    assert "guidance_received" in kinds
    assert kinds.count("supervisor_intended") == 1
    # The run proceeds through the suppressed boundaries without a trigger,
    # and the journal replays to the recorded state.
    assert kinds.count("model_decided") == 7
    assert replay(events) == session._state
