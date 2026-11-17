"""A restarted attempt starts from the state the environment is really in (COD-74).

In an environment that cannot be restored, abandoning an attempt cannot undo
its effects, so the next attempt, its proposals and its evaluations must name
the state the environment was left in, not the contract's initial state.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vharness.agent import (
    ActionChoice,
    EvaluationChoice,
    GuidanceResponse,
    ScriptedModel,
    Session,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import IntegrityError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ActionDefinition,
    BudgetLimits,
    CompletionMode,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    Event,
    ExecutionReceipt,
    ExecutionStatus,
    RuntimeCapabilities,
    StateRef,
    TaskSpec,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.replay import replay
from vharness.agent.supervision import (
    Guidance,
    GuidanceDecision,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
)
from vharness.agent.models import UsageMeasurement
from vharness.agent.transitions import OBSERVED_STARTING_STATE

SMOKE = (
    Path(__file__).parents[2]
    / "evidence"
    / "phase-0004-arc-agi-3-smoke"
    / "attempt-4-scorecard"
    / "session.sqlite3"
)


class FakeEnvironment:
    def __init__(self, *, restorable: bool) -> None:
        self.restorable = restorable

    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=StateRef(
                "fake", "initial", digest="0" * 64, restorable=self.restorable
            ),
            actions=(ActionDefinition("move", "Change the environment"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class MovingRuntime:
    """Each action moves the environment to a new state, like a game move."""

    def __init__(self, *, restorable: bool) -> None:
        self.restorable = restorable
        self.proposals = []
        self.receipts: dict[str, ExecutionReceipt] = {}

    def identity(self):
        return ComponentIdentity("moving-runtime", "v1")

    def submit(self, proposal):
        self.proposals.append(proposal)
        moves = len(self.proposals)
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef(
                "fake",
                f"after-{moves}-moves",
                digest=f"{moves:064d}",
                restorable=self.restorable,
            ),
        )
        self.receipts[proposal.proposal_id] = receipt
        return receipt

    def reconcile(self, operation_id):
        receipt = self.receipts.get(operation_id)
        if receipt is None:
            return ReconciliationResult(ReconciliationKind.UNKNOWN)
        return ReconciliationResult(ReconciliationKind.RECEIPT, receipt)


class RecordingEvaluator:
    def __init__(self) -> None:
        self.requests = []

    def identity(self):
        return ComponentIdentity("recording-evaluator", "v1")

    def evaluate(self, request):
        self.requests.append(request)
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            accepted=False,
            comparison="regressed",
            **request.receipt_identity(),
        )

    def reconcile(self, request_id):
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        objective="move the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=12, evaluations=2),
        environment_id="fake",
        evaluation_contract_id="recording-evaluator-v1",
    )


def _supervision() -> SupervisionCapability:
    guidance = Guidance(
        diagnosis="repeated moves without new information",
        constraints=("try something else",),
        suggested_experiments=("start over",),
        expires_after_call=10,
    )
    supervisor = ScriptedSupervisor(
        (
            SupervisorResponse(
                guidance, UsageMeasurement("sup-usage-1", model_calls=1), "scripted"
            ),
        )
    )
    policy = MonitorPolicy(
        window_calls=4,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=False,
        burn_trigger=False,
        checkpoint_trigger=False,
    )
    return SupervisionCapability(supervisor, policy)


def _replanned(tmp_path: Path, *, restorable: bool):
    """Move three times, replan, then move once and evaluate."""
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    runtime = MovingRuntime(restorable=restorable)
    evaluator = RecordingEvaluator()
    session = Session.create(
        "session-1",
        _task(),
        journal=journal,
        artifacts=ArtifactStore(tmp_path / "artifacts"),
        environment=FakeEnvironment(restorable=restorable),
        runtime=runtime,
        evaluator=evaluator,
        supervision=_supervision(),
    )
    for index in range(4):
        session.step(ScriptedModel((ActionChoice("move", f"move {index}"),)))
    before_replan = runtime.receipts[runtime.proposals[-1].proposal_id].state
    session.step(
        ScriptedModel((GuidanceResponse(GuidanceDecision.REPLAN, "start over"),))
    )
    started = session.view().active_attempt
    assert started is not None
    session.step(ScriptedModel((ActionChoice("move", "after replan"),)))
    after_move = runtime.receipts[runtime.proposals[-1].proposal_id].state
    session.step(ScriptedModel((EvaluationChoice("check"),)))
    return journal, session, runtime, evaluator, before_replan, started, after_move


def test_unrestorable_replan_starts_from_the_observed_state(tmp_path):
    journal, session, runtime, evaluator, before, started, after = _replanned(
        tmp_path, restorable=False
    )
    try:
        assert started.starting_state == before
        assert started.starting_state.value != "initial"
        assert runtime.proposals[-1].expected_state == before
        assert evaluator.requests[-1].evaluated_state == after
        payloads = [e.payload for e in session.events() if e.kind == "attempt_started"]
        assert all(p["starting_state"] == OBSERVED_STARTING_STATE for p in payloads)
        # The journal replays to the same projection.
        replayed = replay(journal.events("session-1", limit=1000))
        assert replayed.attempt_history == session.attempts()
    finally:
        journal.close()


def test_restorable_replan_returns_to_the_lineage_head(tmp_path):
    journal, _session, runtime, _evaluator, _before, started, _after = _replanned(
        tmp_path, restorable=True
    )
    try:
        assert started.starting_state.value == "initial"
        assert runtime.proposals[-1].expected_state.value == "initial"
    finally:
        journal.close()


def test_reopened_session_keeps_the_observed_starting_state(tmp_path):
    journal, session, _runtime, _evaluator, before, started, _after = _replanned(
        tmp_path, restorable=False
    )
    journal.close()
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    try:
        state = replay(journal.events("session-1", limit=1000))
        attempts = [item.attempt for item in state.attempt_history]
        assert attempts[-1].attempt_id == started.attempt_id
        assert attempts[-1].starting_state == before
        assert state.attempt_history == session.attempts()
    finally:
        journal.close()


def _with_payload(event: Event, payload: dict) -> Event:
    fields = {name: getattr(event, name) for name in event.__dataclass_fields__}
    fields["payload"] = payload
    return Event(**fields)


def test_replay_refuses_an_unknown_or_mismatched_starting_state(tmp_path):
    journal, _session, *_ = _replanned(tmp_path, restorable=False)
    try:
        events = list(journal.events("session-1", limit=1000))
    finally:
        journal.close()
    restarted = [
        index
        for index, event in enumerate(events)
        if event.kind == "attempt_started" and index > 0
    ][-1]
    unknown = dict(events[restarted].payload, starting_state="guessed")
    with pytest.raises(IntegrityError, match="starting_state is unknown"):
        replay(events[:restarted] + [_with_payload(events[restarted], unknown)])
    # Dropping the marker selects the old lineage-head rule, which the
    # recorded attempt does not satisfy, so the forgery is caught.
    legacy = {"attempt": events[restarted].payload["attempt"]}
    with pytest.raises(IntegrityError, match="not the derived attempt"):
        replay(events[:restarted] + [_with_payload(events[restarted], legacy)])


def test_journals_written_before_the_marker_still_replay(tmp_path):
    """The retained COD-38 smoke journal uses the old rule and must verify."""
    copy = tmp_path / "smoke.sqlite3"
    shutil.copy(SMOKE, copy)
    journal = SqliteJournal(copy)
    try:
        session_id = journal.session_ids()[0]
        events = journal.events(session_id, limit=10000)
        assert all(
            "starting_state" not in e.payload
            for e in events
            if e.kind == "attempt_started"
        )
        state = replay(events)
        restarted = [item.attempt for item in state.attempt_history][1]
        assert restarted.starting_state.value == "initial"  # as recorded
    finally:
        journal.close()
