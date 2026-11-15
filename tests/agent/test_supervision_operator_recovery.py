"""Operator escalation remains durable across a SQLite journal reopen."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    AttemptDisposition,
    ActionProposal,
    ActionDefinition,
    ComponentIdentity,
    EvaluationReceipt,
    EvaluationRequest,
    EnvironmentContract,
    ExecutionReceipt,
    ExecutionStatus,
    Message,
    RuntimeCapabilities,
    StateRef,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.supervision import (
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
)


class _Environment:
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="operator-recovery",
            version="1",
            initial_state=StateRef("operator-recovery", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _Runtime:
    def __init__(self) -> None:
        self.proposals: list[ActionProposal] = []

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("operator-recovery-runtime", "1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self.proposals.append(proposal)
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("operator-recovery", "after-inspect", digest="1" * 64),
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _Evaluator:
    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("operator-recovery-evaluator", "1")

    def evaluate(self, request: EvaluationRequest) -> EvaluationReceipt:
        raise AssertionError("this scenario does not request evaluation")

    def reconcile(self, request_id: str) -> EvaluationReconciliationResult:
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@dataclass(slots=True)
class _Dependencies:
    journal: SqliteJournal
    artifacts: ArtifactStore
    environment: _Environment
    runtime: _Runtime
    evaluator: _Evaluator
    database_path: Path


@pytest.fixture
def stores(tmp_path: Path) -> Iterator[_Dependencies]:
    database_path = tmp_path / "session.sqlite3"
    journal = SqliteJournal(database_path)
    dependencies = _Dependencies(
        journal,
        ArtifactStore(tmp_path / "artifacts"),
        _Environment(),
        _Runtime(),
        _Evaluator(),
        database_path,
    )
    yield dependencies
    dependencies.journal.close()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="operator-recovery-task",
        objective="recover after repeated inspection",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=12, evaluations=2, model_calls=32),
        environment_id="operator-recovery",
        evaluation_contract_id="operator-recovery-evaluator-v1",
    )


def _capability() -> SupervisionCapability:
    return SupervisionCapability(
        ScriptedSupervisor(()),
        MonitorPolicy(window_calls=4, repeat_threshold=3, cooldown_calls=1),
    )


def _inspect() -> ScriptedModel:
    return ScriptedModel((ActionChoice("inspect", "repeat the same inspection"),))


def _open(stores: _Dependencies) -> Session:
    return Session.open(
        "operator-recovery-session",
        journal=stores.journal,
        artifacts=stores.artifacts,
        environment=stores.environment,
        runtime=stores.runtime,
        evaluator=stores.evaluator,
        supervision=_capability(),
    )


def test_operator_wait_survives_reopen_and_answer_resumes_once(
    stores: _Dependencies,
) -> None:
    session = Session.create(
        "operator-recovery-session",
        _task(),
        journal=stores.journal,
        artifacts=stores.artifacts,
        environment=stores.environment,
        runtime=stores.runtime,
        evaluator=stores.evaluator,
        supervision=_capability(),
    )
    for _ in range(6):
        session.step(_inspect())

    before_reopen = list(session.events())
    questions = [event for event in before_reopen if event.kind == "question_recorded"]
    assert len(questions) == 1
    assert session.view().status is SessionStatus.WAITING
    assert len(stores.runtime.proposals) == 5
    counters_before_close = session.counters()
    assert counters_before_close.model_calls == 5
    lineage_before = session.accepted_lineage()
    attempts_before = session.attempts()
    assert any(
        attempt.attempt.disposition is AttemptDisposition.ABANDONED
        for attempt in attempts_before
    )

    stores.journal.close()
    stores.journal = SqliteJournal(stores.database_path)
    reopened = _open(stores)
    assert reopened.view().status is SessionStatus.WAITING
    assert reopened.counters() == counters_before_close
    assert reopened.accepted_lineage() == lineage_before
    assert reopened.attempts() == attempts_before
    assert [event.event_id for event in reopened.events()] == [
        event.event_id for event in before_reopen
    ]
    reopened_question = next(
        event for event in reopened.events() if event.kind == "question_recorded"
    )
    assert reopened_question.payload == questions[0].payload

    waiting_events = list(reopened.events())
    waiting_counters = reopened.counters()
    waiting_lineage = reopened.accepted_lineage()
    for _ in range(3):
        reopened.advance()
        assert reopened.view().status is SessionStatus.WAITING
    assert list(reopened.events()) == waiting_events
    assert reopened.counters() == waiting_counters
    assert reopened.accepted_lineage() == waiting_lineage
    assert len(stores.runtime.proposals) == 5

    reopened.enqueue(
        Message("operator-recovery-answer", "operator-recovery-session", "continue")
    )
    reopened.advance()
    reopened.advance()
    assert reopened.view().status is SessionStatus.RUNNING
    assert [event.kind for event in reopened.events()].count("question_answered") == 1
    assert reopened.counters().model_calls == waiting_counters.model_calls
    assert len(stores.runtime.proposals) == 5

    reopened.step(_inspect())
    assert reopened.counters().model_calls == waiting_counters.model_calls + 1
    assert len(stores.runtime.proposals) == 6
    assert len(reopened.accepted_lineage()) == len(lineage_before)
    assert [event.kind for event in reopened.events()].count("question_recorded") == 1
    post_answer_counters = reopened.counters()
    post_answer_lineage = reopened.accepted_lineage()
    post_answer_attempts = reopened.attempts()
    post_answer_event_ids = [event.event_id for event in reopened.events()]
    reopened_journal = stores.journal
    reopened_journal.close()
    stores.journal = SqliteJournal(stores.database_path)
    replayed = _open(stores)
    assert replayed.counters() == post_answer_counters
    assert replayed.accepted_lineage() == post_answer_lineage
    assert replayed.attempts() == post_answer_attempts
    assert [event.event_id for event in replayed.events()] == post_answer_event_ids
