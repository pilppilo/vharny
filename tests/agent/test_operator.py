"""Live operator observation surface."""

from __future__ import annotations

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    OPERATOR_ANSWER_WAIT,
    OperatorQuestion,
    PlanUpdate,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
    WaitChoice,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ActionDefinition,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    ExecutionStatus,
    Message,
    Pause,
    Resume,
    RuntimeCapabilities,
    StateRef,
    Steer,
    Stop,
    UsageMeasurement,
)
from vharness.agent.replay import replay
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.supervision import (
    Guidance,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
)


class FakeEnvironment:
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef("fake", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class FakeRuntime:
    def __init__(self, status: ExecutionStatus = ExecutionStatus.SUCCEEDED) -> None:
        self.status = status
        self.proposals = []

    def identity(self):
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal):
        self.proposals.append(proposal)
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=self.status,
            state=StateRef("fake", "after", digest="1" * 64),
        )

    def reconcile(self, operation_id):
        del operation_id
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class FakeEvaluator:
    def identity(self):
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=True,
            comparison="improved",
        )

    def reconcile(self, request_id):
        del request_id
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@pytest.fixture
def session(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    created = Session.create(
        "session-operator",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=2, model_calls=8),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=FakeRuntime(),
        evaluator=FakeEvaluator(),
    )
    yield created
    journal.close()


def test_operator_view_includes_objective_stream_and_questions(session):
    session.enqueue(Message("msg-1", "session-operator", "prefer inspect"))
    session.advance()
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    view = session.operator_view()
    assert view.session_id == session.session_id
    assert view.status is SessionStatus.WAITING
    assert session.view().wait_reason == OPERATOR_ANSWER_WAIT
    assert view.objective == "inspect the fixture"
    assert view.operator_direction == "prefer inspect"
    assert "prefer inspect" in view.acknowledged_steering
    assert any("is inspect enough?" in item for item in view.questions)
    assert any(item.startswith("OPERATOR ") for item in view.action_receipt_stream)
    assert "actions=4" in view.budgets
    assert view.remaining_budgets == view.budgets
    assert view.current_plan == "none"
    assert session.view().lineage_head.node_id in view.evidence_ids


def test_admitted_message_is_not_acknowledged_until_applied(session):
    session.enqueue(Message("msg-1", "session-operator", "prefer inspect"))
    view = session.operator_view()
    assert view.operator_direction is None
    assert view.acknowledged_steering == ()
    assert not any(item.startswith("OPERATOR ") for item in view.action_receipt_stream)
    session.advance()
    applied = session.operator_view()
    assert applied.operator_direction == "prefer inspect"
    assert applied.acknowledged_steering == ("prefer inspect",)
    assert any(item.startswith("OPERATOR ") for item in applied.action_receipt_stream)


def test_steering_while_pending_is_acknowledged_without_new_dispatch(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    runtime = FakeRuntime(ExecutionStatus.ACCEPTED)
    created = Session.create(
        "session-operator",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=2, model_calls=8),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(),
        runtime=runtime,
        evaluator=FakeEvaluator(),
    )
    created.begin_attempt()
    pending = created.submit_action("inspect", {}, rationale="inspect before steering")
    waiting = created.operator_view()
    assert waiting.status is SessionStatus.WAITING
    assert waiting.pending_proposal_id == pending.proposal_id
    created.enqueue(
        Steer("steer-1", "session-operator", "new objective", "new evidence")
    )
    created.advance()
    view = created.operator_view()
    assert view.objective == "new objective"
    assert view.objective_version == 2
    assert view.acknowledged_steering == ("new objective",)
    assert any(item.startswith("ACTION ") for item in view.action_receipt_stream)
    assert any(item.startswith("STEER ") for item in view.action_receipt_stream)
    assert len(runtime.proposals) == 1
    journal.close()


def test_operator_view_rebuilds_after_reopen(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    environment = FakeEnvironment()
    runtime = FakeRuntime()
    evaluator = FakeEvaluator()
    created = Session.create(
        "session-operator",
        TaskSpec(
            "task-1",
            "inspect the fixture",
            CompletionMode.FINITE,
            "external evaluator acceptance",
            BudgetLimits(actions=4, evaluations=2, model_calls=8),
            "fake",
            "fake-evaluator-v1",
        ),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    created.enqueue(Message("msg-1", "session-operator", "prefer inspect"))
    created.advance()
    created.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    view = created.operator_view()
    reopened = Session.open(
        "session-operator",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert reopened.operator_view() == view
    assert reopened.view().status is SessionStatus.WAITING
    assert reopened.view().wait_reason == OPERATOR_ANSWER_WAIT
    journal.close()


def _question_id(session: Session) -> str:
    for event in session.events(limit=100):
        if event.kind == "question_recorded":
            question_id = event.payload.get("question_id")
            assert isinstance(question_id, str)
            return question_id
    raise AssertionError("no question_recorded event")


def _kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events(limit=200)]


def test_operator_question_waits_and_second_step_does_not_call_the_model(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    assert session.view().status is SessionStatus.WAITING
    assert session.view().wait_reason == OPERATOR_ANSWER_WAIT
    assert _question_id(session).startswith("question-")
    decided = _kinds(session).count("model_decided")
    session.step(ScriptedModel((OperatorQuestion("another?"),)))
    assert session.view().status is SessionStatus.WAITING
    assert _kinds(session).count("model_decided") == decided


def test_correlated_message_answers_the_open_question(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    question_id = _question_id(session)
    session.enqueue(
        Message("msg-answer", "session-operator", "yes, inspect", question_id)
    )
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    assert session.view().wait_reason is None
    kinds = _kinds(session)
    assert "question_answered" in kinds
    assert kinds.count("command_rejected") == 0
    session.step(ScriptedModel((OperatorQuestion("follow up?"),)))
    assert session.view().status is SessionStatus.WAITING


def test_omitted_question_id_binds_to_the_single_open_question(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    session.enqueue(Message("msg-answer", "session-operator", "yes, inspect"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    answered = [
        event
        for event in session.events(limit=100)
        if event.kind == "question_answered"
    ]
    assert answered[0].payload["question_id"] == _question_id(session)


def test_wrong_question_id_is_rejected_and_keeps_waiting(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    session.enqueue(Message("msg-wrong", "session-operator", "nope", "question-other"))
    session.advance()
    assert session.view().status is SessionStatus.WAITING
    assert session.view().wait_reason == OPERATOR_ANSWER_WAIT
    assert "command_rejected" in _kinds(session)
    assert "question_answered" not in _kinds(session)


def test_resume_while_awaiting_answer_is_durably_rejected(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    session.enqueue(Resume("resume-1", "session-operator"))
    session.advance()
    assert session.view().status is SessionStatus.WAITING
    assert session.view().wait_reason == OPERATOR_ANSWER_WAIT
    rejected = [
        event for event in session.events(limit=100) if event.kind == "command_rejected"
    ]
    assert rejected
    assert "operator answer pending" in str(rejected[-1].payload.get("reason"))


def test_pause_then_resume_returns_to_operator_answer_wait(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    session.enqueue(Pause("pause-1", "session-operator"))
    session.advance()
    assert session.view().status is SessionStatus.PAUSED
    session.enqueue(Resume("resume-1", "session-operator"))
    session.advance()
    assert session.view().status is SessionStatus.WAITING
    assert session.view().wait_reason == OPERATOR_ANSWER_WAIT
    assert "question_answered" not in _kinds(session)


def test_paused_question_answer_clears_question_without_resuming(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    question_id = _question_id(session)
    session.enqueue(Pause("pause-1", "session-operator"))
    session.advance()
    session.enqueue(Message("answer-1", "session-operator", "yes", question_id))
    session.advance()

    assert session.view().status is SessionStatus.PAUSED
    assert session.view().wait_reason is None
    replayed = replay(session.events(limit=100))
    assert replayed.status is SessionStatus.PAUSED
    assert replayed.blocking_question_id is None
    session.enqueue(Resume("resume-1", "session-operator"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING


def test_stopped_question_answer_preserves_terminality(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    question_id = _question_id(session)
    session.enqueue(Stop("stop-1", "session-operator"))
    session.advance()
    session.enqueue(Message("answer-1", "session-operator", "yes", question_id))
    session.advance()

    assert session.view().status is SessionStatus.STOPPED
    assert session.view().wait_reason is None
    replayed = replay(session.events(limit=100))
    assert replayed.status is SessionStatus.STOPPED
    assert replayed.blocking_question_id is None


def test_stale_question_id_is_durably_rejected_without_an_open_question(session):
    session.enqueue(Message("message-1", "session-operator", "ordinary direction"))
    session.advance()
    assert session.operator_view().operator_direction == "ordinary direction"
    session.enqueue(
        Message("message-stale", "session-operator", "stale answer", "question-old")
    )
    session.advance()

    rejected = [
        event for event in session.events(limit=100) if event.kind == "command_rejected"
    ]
    assert "no open question" in str(rejected[-1].payload["reason"])
    assert session.operator_view().operator_direction == "ordinary direction"


def test_steer_retires_the_blocking_question_without_answering(session):
    session.step(ScriptedModel((OperatorQuestion("is inspect enough?"),)))
    question_id = _question_id(session)
    session.enqueue(
        Steer("steer-1", "session-operator", "new objective", "new evidence")
    )
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    assert session.view().task.objective == "new objective"
    assert session.view().task.objective_version == 2
    kinds = _kinds(session)
    assert "question_retired" in kinds
    assert "question_answered" not in kinds
    retired = [
        event for event in session.events(limit=100) if event.kind == "question_retired"
    ]
    assert retired[0].payload["question_id"] == question_id
    assert retired[0].payload["reason"] == "objective_steered"


def test_plan_update_is_current_plan_and_clears_on_steer(session):
    session.step(ScriptedModel((PlanUpdate("inspect then evaluate"),)))
    assert session.operator_view().current_plan == "inspect then evaluate"
    session.enqueue(
        Steer("steer-plan", "session-operator", "new objective", "new evidence")
    )
    session.advance()
    assert session.operator_view().current_plan == "none"


def test_reserved_wait_reason_is_rejected(session):
    session.step(ScriptedModel((WaitChoice(OPERATOR_ANSWER_WAIT),)))
    assert session.view().status is SessionStatus.RUNNING
    assert session.view().wait_reason is None
    assert "wait_rejected" in _kinds(session)
    assert "agent_waited" not in _kinds(session)


def _supervision_capability() -> SupervisionCapability:
    return SupervisionCapability(
        ScriptedSupervisor(
            (
                SupervisorResponse(
                    Guidance(
                        diagnosis="repeated inspection without new information",
                        constraints=("avoid re-inspecting",),
                        suggested_experiments=("change the target",),
                        expires_after_call=10,
                    ),
                    UsageMeasurement("op-supervisor-usage", model_calls=1),
                    "scripted-supervisor",
                ),
            )
        ),
        MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
    )


def test_operator_view_surfaces_supervision_notices(tmp_path):
    """Advisory supervision notices appear on the operator surface, display only."""
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    artifacts = ArtifactStore(tmp_path / "artifacts")
    task = TaskSpec(
        "task-supervision",
        "inspect the fixture",
        CompletionMode.FINITE,
        "external evaluator acceptance",
        BudgetLimits(actions=12, evaluations=2, model_calls=12),
        "fake",
        "fake-evaluator-v1",
    )
    try:
        session = Session.create(
            "session-supervision",
            task,
            journal=journal,
            artifacts=artifacts,
            environment=FakeEnvironment(),
            runtime=FakeRuntime(),
            evaluator=FakeEvaluator(),
            supervision=_supervision_capability(),
        )
        for _ in range(4):
            session.step(ScriptedModel((ActionChoice("inspect", "repeat"),)))
        session.step(ScriptedModel((ActionChoice("inspect", "repeat"),)))
        session.step(ScriptedModel((ActionChoice("inspect", "repeat"),)))

        view = session.operator_view()
        stream = view.action_receipt_stream
        assert any(
            item.startswith("SUPERVISOR trigger") and "call=4" in item
            for item in stream
        )
        assert "GUIDANCE repeated inspection without new information" in stream
        assert any(
            item.startswith("SUPERVISOR escalation") and "stage=3" in item
            for item in stream
        )
        assert view.status is SessionStatus.WAITING

        reopened = Session.open(
            "session-supervision",
            journal=journal,
            artifacts=artifacts,
            environment=FakeEnvironment(),
            runtime=FakeRuntime(),
            evaluator=FakeEvaluator(),
            supervision=_supervision_capability(),
        )
        assert reopened.operator_view() == view
    finally:
        journal.close()
