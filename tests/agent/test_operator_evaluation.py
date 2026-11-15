"""Operator-evaluation application and paused/stopped intake behavior."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vharness.agent import (
    BudgetLimits,
    CompletionMode,
    EvaluationChoice,
    EvaluationMode,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json
from vharness.agent.errors import IntegrityError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ArtifactRef,
    EvaluationDecision,
    OPERATOR_EVALUATION_WAIT,
    Pause,
    Resume,
    Steer,
    Stop,
)
from vharness.agent.replay import replay
from vharness.agent.session_state import evaluation_receipt_from_payload
from vharness.agent.transitions import EvaluationOperationStatus

from test_session import FakeEnvironment, FakeEvaluator, FakeRuntime


@pytest.fixture
def dependencies(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(
        tmp_path / "artifacts"
    ), FakeEnvironment(), FakeRuntime(), FakeEvaluator()
    journal.close()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-operator-evaluation",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="operator acceptance",
        budgets=BudgetLimits(actions=6, evaluations=4),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def _operator_session(dependencies, session_id: str) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    return Session.create(
        session_id,
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        evaluation_mode=EvaluationMode.OPERATOR,
    )


def _pending_request(session: Session, dependencies):
    _, artifacts, _, _, evaluator = dependencies
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    assert session.request_evaluation(receipt.state) is None
    assert evaluator.requests == []
    operation = session._state.evaluation_operations[-1]
    return operation.request, artifacts


def _decision(session: Session, request_id: str, command_id: str, **kwargs):
    return EvaluationDecision(
        command_id=command_id,
        session_id=session.view().session_id,
        request_id=request_id,
        **kwargs,
    )


def test_operator_request_waits_without_dispatch(dependencies):
    session = _operator_session(dependencies, "session-operator-wait")
    request, _ = _pending_request(session, dependencies)
    view = session.view()
    assert view.status is SessionStatus.WAITING
    assert session._state.wait_reason == OPERATOR_EVALUATION_WAIT
    intended = next(
        event for event in session.events() if event.kind == "evaluation_intended"
    )
    assert intended.payload["wait_reason"] == OPERATOR_EVALUATION_WAIT
    assert replay(session.events()) == session._state
    assert request.request_id.startswith("evaluation-")


def test_accepted_milestone_promotes_and_replays(dependencies):
    session = _operator_session(dependencies, "session-operator-milestone")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"milestone proof", media_type="text/plain", provenance="test"
    )
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-milestone",
            accepted=True,
            reason="milestone demonstrated",
            evidence_ref=evidence,
        )
    )
    session.advance()
    view = session.view()
    assert view.status is SessionStatus.RUNNING
    assert view.lineage_head.evaluation_receipt_id is not None
    kinds = [event.kind for event in session.events()]
    assert kinds[-4:] == [
        "evaluation_intake",
        "evaluation_received",
        "lineage_promoted",
        "command_applied",
    ]
    applied = session.events()[-1]
    promoted = session.events()[-2]
    assert applied.causation_id == "decision-milestone"
    assert applied.payload["command_id"] == "decision-milestone"
    assert promoted.sequence < applied.sequence
    assert replay(session.events()) == session._state


def test_accepted_completion_finishes_finite_task(dependencies):
    session = _operator_session(dependencies, "session-operator-completion")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(b"final proof", media_type="text/plain", provenance="test")
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-complete",
            accepted=True,
            completed=True,
            reason="objective complete",
            evidence_ref=evidence,
        )
    )
    session.advance()
    assert session.view().status is SessionStatus.COMPLETED
    received = next(
        event for event in session.events() if event.kind == "evaluation_received"
    )
    receipt = evaluation_receipt_from_payload(received.payload["receipt"])
    assert receipt.comparison == "operator acceptance"
    assert receipt.completion_evidence == (evidence,)
    assert session.view().lineage_head.evaluation_receipt_id == receipt.receipt_id
    assert replay(session.events()) == session._state


def test_rejected_decision_closes_attempt_without_promotion(dependencies):
    session = _operator_session(dependencies, "session-operator-rejected")
    request, _ = _pending_request(session, dependencies)
    head = session.view().lineage_head
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-reject",
            accepted=False,
            reason="not demonstrated yet",
        )
    )
    session.advance()
    view = session.view()
    assert view.status is SessionStatus.RUNNING
    assert view.lineage_head == head
    assert view.active_attempt is None
    kinds = [event.kind for event in session.events()]
    assert "lineage_promoted" not in kinds
    assert kinds[-2:] == ["evaluation_received", "command_applied"]
    assert replay(session.events()) == session._state


def test_paused_intake_is_consumed_only_after_resume(dependencies):
    session = _operator_session(dependencies, "session-operator-paused")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"paused proof", media_type="text/plain", provenance="test"
    )
    session.enqueue(Pause(command_id="pause-1", session_id=session.view().session_id))
    session.advance()
    assert session.view().status is SessionStatus.PAUSED
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-paused",
            accepted=True,
            reason="accepted while paused",
            evidence_ref=evidence,
        )
    )
    session.advance()
    kinds = [event.kind for event in session.events()]
    assert "evaluation_intake" in kinds
    assert "evaluation_received" not in kinds
    assert session.view().status is SessionStatus.PAUSED
    operation = session._state.evaluation_operations[-1]
    assert operation.status is EvaluationOperationStatus.INTAKE_RECORDED
    session.enqueue(Resume(command_id="resume-1", session_id=session.view().session_id))
    session.advance()
    session.advance()
    view = session.view()
    assert view.status is SessionStatus.RUNNING
    assert view.lineage_head.evaluation_receipt_id is not None
    kinds = [event.kind for event in session.events()]
    assert kinds[-3:] == ["evaluation_received", "lineage_promoted", "command_applied"]
    assert replay(session.events()) == session._state


def test_paused_intake_survives_reopen_before_resume(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = _operator_session(dependencies, "session-operator-reopen")
    request, _ = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"reopen proof", media_type="text/plain", provenance="test"
    )
    session.enqueue(Pause(command_id="pause-1", session_id=session.view().session_id))
    session.advance()
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-reopen",
            accepted=True,
            reason="accepted while paused",
            evidence_ref=evidence,
        )
    )
    session.advance()
    reopened = Session.open(
        "session-operator-reopen",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        evaluation_mode=EvaluationMode.OPERATOR,
    )
    assert reopened.view().status is SessionStatus.PAUSED
    reopened.enqueue(
        Resume(command_id="resume-1", session_id=reopened.view().session_id)
    )
    reopened.advance()
    reopened.advance()
    assert reopened.view().lineage_head.evaluation_receipt_id is not None
    assert replay(reopened.events()) == reopened._state


def test_stopped_intake_is_retained_and_never_consumed(dependencies):
    session = _operator_session(dependencies, "session-operator-stopped")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"stopped proof", media_type="text/plain", provenance="test"
    )
    session.enqueue(Stop(command_id="stop-1", session_id=session.view().session_id))
    session.advance()
    assert session.view().status is SessionStatus.STOPPED
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-stopped",
            accepted=True,
            reason="accepted after stop",
            evidence_ref=evidence,
        )
    )
    session.advance()
    after_intake = list(session.events())
    assert after_intake[-1].kind == "evaluation_intake"
    session.advance()
    assert list(session.events()) == after_intake
    view = session.view()
    assert view.status is SessionStatus.STOPPED
    assert view.lineage_head.evaluation_receipt_id is None
    operation = session._state.evaluation_operations[-1]
    assert operation.status is EvaluationOperationStatus.INTAKE_RECORDED
    command = next(
        item
        for item in session._state.command_operations
        if item.command.command_id == "decision-stopped"
    )
    assert command.disposition is None
    assert replay(session.events()) == session._state


def test_steer_during_paused_wait_makes_decision_stale_on_resume(dependencies):
    session = _operator_session(dependencies, "session-operator-stale")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(b"stale proof", media_type="text/plain", provenance="test")
    session.enqueue(Pause(command_id="pause-1", session_id=session.view().session_id))
    session.advance()
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-stale",
            accepted=True,
            reason="accepted before steer",
            evidence_ref=evidence,
        )
    )
    session.advance()
    session.enqueue(
        Steer(
            command_id="steer-1",
            session_id=session.view().session_id,
            objective="new objective",
            success_evidence="operator acceptance of the new objective",
        )
    )
    session.advance()
    session.enqueue(Resume(command_id="resume-1", session_id=session.view().session_id))
    session.advance()
    session.advance()
    view = session.view()
    assert view.lineage_head.evaluation_receipt_id is None
    assert view.active_attempt is None
    received = next(
        event for event in session.events() if event.kind == "evaluation_received"
    )
    assert received.payload["disposition"] == "stale"
    assert "lineage_promoted" not in [event.kind for event in session.events()]
    assert replay(session.events()) == session._state


def test_identical_replay_is_visible_no_op_and_conflict_rejected(dependencies):
    session = _operator_session(dependencies, "session-operator-replay")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"replay proof", media_type="text/plain", provenance="test"
    )
    decision = _decision(
        session,
        request.request_id,
        "decision-replay",
        accepted=True,
        reason="accepted once",
        evidence_ref=evidence,
    )
    session.enqueue(decision)
    session.advance()
    assert session.view().lineage_head.evaluation_receipt_id is not None
    settled = list(session.events())
    session.enqueue(replace(decision, command_id="decision-replay-again"))
    session.advance()
    kinds = [event.kind for event in session.events()]
    assert kinds[len(settled) :] == ["command_admitted", "command_applied"]
    replayed = session.events()[-1]
    assert replayed.payload["command_id"] == "decision-replay-again"
    replay_command = next(
        item
        for item in session._state.command_operations
        if item.command.command_id == "decision-replay-again"
    )
    assert replay_command.disposition == "applied"
    conflicting = _decision(
        session,
        request.request_id,
        "decision-conflict",
        accepted=False,
        reason="contradicts the accepted decision",
    )
    session.enqueue(conflicting)
    session.advance()
    rejected = session.events()[-1]
    assert rejected.kind == "command_rejected"
    assert "conflicts" in rejected.payload["reason"]
    assert replay(session.events()) == session._state


def test_conflicting_decision_while_paused_is_rejected(dependencies):
    session = _operator_session(dependencies, "session-operator-conflict-paused")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(b"first proof", media_type="text/plain", provenance="test")
    session.enqueue(Pause(command_id="pause-1", session_id=session.view().session_id))
    session.advance()
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-first",
            accepted=True,
            reason="first answer",
            evidence_ref=evidence,
        )
    )
    session.advance()
    operation = session._state.evaluation_operations[-1]
    assert operation.status is EvaluationOperationStatus.INTAKE_RECORDED
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-second",
            accepted=False,
            reason="changed mind",
        )
    )
    session.advance()
    rejected = session.events()[-1]
    assert rejected.kind == "command_rejected"
    assert "conflicting" in rejected.payload["reason"]
    assert session._state.evaluation_operations[-1].raw_receipt == canonical_json(
        session._operator_evaluation_receipt(
            request,
            _decision(
                session,
                request.request_id,
                "decision-first",
                accepted=True,
                reason="first answer",
                evidence_ref=evidence,
            ),
        )
    )
    assert replay(session.events()) == session._state


def test_missing_evidence_fails_verification_and_rejects(dependencies):
    session = _operator_session(dependencies, "session-operator-missing-evidence")
    request, _ = _pending_request(session, dependencies)
    missing = ArtifactRef("e" * 64, 7, "text/plain", "never published")
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-missing",
            accepted=True,
            reason="claims unseen evidence",
            evidence_ref=missing,
        )
    )
    session.advance()
    kinds = [event.kind for event in session.events()]
    assert "evaluation_intake" in kinds
    assert "evaluation_verification_failed" in kinds
    assert "evaluation_received" not in kinds
    rejected = session.events()[-1]
    assert rejected.kind == "command_rejected"
    assert replay(session.events()) == session._state


def test_replay_rejects_a_manufactured_control_transition_binding(dependencies):
    session = _operator_session(dependencies, "session-forged-binding")
    session.enqueue(
        Pause(command_id="pause-forged", session_id=session.view().session_id)
    )
    events = list(session.events())
    template = events[-1]
    forged = replace(
        template,
        event_id="event-forged-binding",
        sequence=template.sequence + 1,
        kind="command_applied",
        payload={
            "command_id": "pause-forged",
            "transition_kind": "session_paused",
        },
        causation_id="pause-forged",
    )
    with pytest.raises(IntegrityError):
        replay((*events, forged))


def test_model_requested_operator_evaluation_waits_without_dispatch(dependencies):
    session = _operator_session(dependencies, "session-model-operator")
    session.begin_attempt()
    model = ScriptedModel((EvaluationChoice("score the attempt"),))
    session.step(model)
    assert session.view().status is SessionStatus.WAITING
    assert session._state.wait_reason == OPERATOR_EVALUATION_WAIT
    intended = next(
        event for event in session.events() if event.kind == "evaluation_intended"
    )
    assert intended.payload["wait_reason"] == OPERATOR_EVALUATION_WAIT
    assert dependencies[4].requests == []
    assert all(operation.settled for operation in session._state.model_operations)
    assert replay(session.events()) == session._state


def test_corrupted_evidence_cannot_promote_after_resume(dependencies):
    session = _operator_session(dependencies, "session-operator-corrupt")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"original proof", media_type="text/plain", provenance="test"
    )
    session.enqueue(Pause(command_id="pause-1", session_id=session.view().session_id))
    session.advance()
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-corrupt",
            accepted=True,
            reason="accepted while paused",
            evidence_ref=evidence,
        )
    )
    session.advance()
    operation = session._state.evaluation_operations[-1]
    assert operation.status is EvaluationOperationStatus.INTAKE_RECORDED
    corrupted = artifacts.root / evidence.digest[:2] / evidence.digest[2:]
    corrupted.write_bytes(b"tampered evidence")
    session.enqueue(Resume(command_id="resume-1", session_id=session.view().session_id))
    session.advance()
    session.advance()
    kinds = [event.kind for event in session.events()]
    assert "lineage_promoted" not in kinds
    assert "evaluation_verification_failed" in kinds
    assert session.view().lineage_head.evaluation_receipt_id is None
    assert replay(session.events()) == session._state


def test_recovery_consumes_retained_intake_and_settles_the_decision(dependencies):
    session = _operator_session(dependencies, "session-operator-recovery")
    request, artifacts = _pending_request(session, dependencies)
    evidence = artifacts.put(
        b"recovery proof", media_type="text/plain", provenance="test"
    )
    session.enqueue(Pause(command_id="pause-1", session_id=session.view().session_id))
    session.advance()
    session.enqueue(
        _decision(
            session,
            request.request_id,
            "decision-recovery",
            accepted=True,
            reason="accepted while paused",
            evidence_ref=evidence,
        )
    )
    session.advance()
    session.enqueue(Resume(command_id="resume-1", session_id=session.view().session_id))
    session.advance()
    assert session.view().status is SessionStatus.WAITING
    session.reconcile_pending()
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    kinds = [event.kind for event in session.events()]
    assert "command_rejected" not in kinds
    command = next(
        item
        for item in session._state.command_operations
        if item.command.command_id == "decision-recovery"
    )
    assert command.disposition == "applied"
    assert session.view().lineage_head.evaluation_receipt_id is not None
    assert replay(session.events()) == session._state
