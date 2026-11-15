"""Crash/recovery injection for PHASE-0003 item 6.

Each test interrupts a supervised run at one durable boundary -- the advisory
guidance consult, the checkpoint/replan attempt transition, and the external
commit/promotion -- then reopens from the journal and asserts that the
projection replays exactly, no external effect repeats, and failed or abandoned
attempts stay outside committed lineage.

Faults are injected by aborting one event kind in SQLite, mirroring
``test_session.py``'s crash-boundary tests; fixtures are scripted, so this suite
makes no provider calls.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import pytest

from vharness.agent import (
    ActionChoice,
    ActionDefinition,
    ActionProposal,
    BudgetLimits,
    CompletionMode,
    ComponentIdentity,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    GuidanceResponse,
    RuntimeCapabilities,
    ScriptedModel,
    Session,
    SessionStatus,
    StateRef,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import IntegrityError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    AttemptDisposition,
    ExecutionStatus,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.replay import replay
from vharness.agent.supervision import (
    SUPERVISION_TRIGGERED,
    Guidance,
    GuidanceDecision,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
)

_GUIDANCE_EXPIRY = 20


class _FakeEnvironment:
    """Two-action mechanical contract for guided recovery."""

    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="crash",
            version="1",
            initial_state=StateRef("crash", "initial", digest="0" * 64),
            actions=(
                ActionDefinition("inspect", "Inspect the crash fixture"),
                ActionDefinition("focus", "Focus the crash fixture"),
            ),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _FakeRuntime:
    """Runtime that records every submitted proposal exactly once."""

    def __init__(self) -> None:
        self.proposals: list[ActionProposal] = []

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("crash-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self.proposals.append(proposal)
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("crash", f"after-{proposal.proposal_id}", digest="1" * 64),
            usage=UsageMeasurement(f"receipt-{proposal.proposal_id}-usage", actions=1),
        )

    def reconcile(self, operation_id) -> ReconciliationResult:
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _FakeEvaluator:
    """External evaluator with configurable acceptance for the commit path."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.requests: list[object] = []
        self.last_receipt: EvaluationReceipt | None = None

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("crash-evaluator", "v1")

    def evaluate(self, request) -> EvaluationReceipt:
        self.requests.append(request)
        receipt = EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=self.accepted,
            comparison="improved" if self.accepted else "regressed",
            objectives={"score": 1},
        )
        self.last_receipt = receipt
        return receipt

    def reconcile(self, request_id) -> EvaluationReconciliationResult:
        if self.last_receipt is None or self.last_receipt.request_id != request_id:
            return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)
        return EvaluationReconciliationResult(
            ReconciliationKind.RECEIPT, self.last_receipt
        )


@pytest.fixture
def dependencies(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield (
        journal,
        ArtifactStore(tmp_path / "artifacts"),
        _FakeEnvironment(),
        _FakeRuntime(),
        _FakeEvaluator(),
    )
    journal.close()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="crash-task",
        objective="escape the repeated inspection fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=12, model_calls=12, evaluations=3),
        environment_id="crash",
        evaluation_contract_id="crash-evaluator-v1",
    )


def _guidance(expires_after_call: int = _GUIDANCE_EXPIRY) -> Guidance:
    return Guidance(
        diagnosis="repeated inspection without new information",
        constraints=("avoid re-inspecting the same state",),
        suggested_experiments=("change the fixture target",),
        expires_after_call=expires_after_call,
    )


def _supervisor(*responses: SupervisorResponse) -> ScriptedSupervisor:
    return ScriptedSupervisor(responses)


def _supervisor_response() -> SupervisorResponse:
    return SupervisorResponse(
        _guidance(),
        UsageMeasurement("crash-supervisor-usage", model_calls=1),
        "crash-supervisor",
    )


def _capability(
    supervisor: ScriptedSupervisor, cooldown_calls: int = 1
) -> SupervisionCapability:
    # Cooldown 1 lets the ladder advance within a short scripted run; a larger
    # cooldown keeps guidance open long enough to respond to it.
    return SupervisionCapability(
        supervisor,
        MonitorPolicy(
            window_calls=4, repeat_threshold=3, cooldown_calls=cooldown_calls
        ),
    )


def _session(
    dependencies, supervisor: ScriptedSupervisor, cooldown_calls: int = 1
) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    return Session.create(
        "crash-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=_capability(supervisor, cooldown_calls),
    )


def _reopen(
    dependencies,
    supervisor: ScriptedSupervisor | None = None,
    cooldown_calls: int = 1,
) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    return Session.open(
        "crash-session",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=_capability(supervisor or _supervisor(), cooldown_calls),
    )


def _inspect() -> tuple[ActionChoice, ...]:
    return (ActionChoice("inspect", "re-inspect the fixture without new information"),)


def _focus() -> tuple[ActionChoice, ...]:
    return (ActionChoice("focus", "focus the fixture on the known issue"),)


def _kinds(session: Session) -> list[str]:
    return [event.kind for event in session.events()]


def _proposal_ids(runtime: _FakeRuntime) -> list[str]:
    """Return the external proposal ids the runtime received, in order."""
    return [proposal.proposal_id for proposal in runtime.proposals]


def _assert_no_duplicate_effects(runtime: _FakeRuntime, expected: int) -> None:
    """Assert the runtime saw ``expected`` submissions, each exactly once."""
    ids = _proposal_ids(runtime)
    assert len(ids) == expected
    assert len(set(ids)) == expected


def _assert_no_active_reservations(session: Session) -> None:
    """Assert recovery left no unsettled reservation in the durable projection."""
    ledger = session._state.ledger  # pylint: disable=protected-access
    assert ledger.active_reservations == ()


@contextmanager
def _crash_on_event(journal: SqliteJournal, kind: str) -> Iterator[None]:
    """Abort any insert of ``kind``, simulating a process crash at that fact."""
    trigger = f"abort_{kind}"
    connection = journal._connection  # pylint: disable=protected-access
    connection.execute(f"""
        CREATE TRIGGER {trigger}
        BEFORE INSERT ON events
        WHEN NEW.kind = '{kind}'
        BEGIN
            SELECT RAISE(ABORT, 'injected {kind} crash');
        END
        """)
    try:
        yield
    finally:
        connection.execute(f"DROP TRIGGER {trigger}")


def test_guidance_transition_crash_recovers_without_duplicate_consult(
    dependencies,
) -> None:
    """A crash after supervisor intent settles the call and never re-consults."""
    journal = dependencies[0]
    runtime = dependencies[3]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)

    with _crash_on_event(journal, "guidance_received"):
        for _ in range(3):
            session.step(ScriptedModel(_inspect()))
        with pytest.raises(IntegrityError, match="injected guidance_received crash"):
            session.step(ScriptedModel(_inspect()))

    kinds = _kinds(session)
    assert kinds.count(SUPERVISION_TRIGGERED) == 1
    assert kinds.count("supervisor_intended") == 1
    assert "guidance_received" not in kinds
    assert supervisor.calls == 1

    # Reopen with a fresh, observably tracked supervisor: a spurious consult
    # after recovery would land on this instance, not the pre-crash one.
    reopened_supervisor = _supervisor(_supervisor_response())
    reopened = _reopen(dependencies, reopened_supervisor)
    kinds = _kinds(reopened)
    assert kinds.count("supervisor_failed") == 1
    assert kinds.count(SUPERVISION_TRIGGERED) == 1
    assert "guidance_received" not in kinds
    assert supervisor.calls == 1
    assert reopened_supervisor.calls == 0
    assert replay(reopened.events()) == reopened._state
    # The reopened session holds the tracked instance, so the counter assertion
    # above observes the supervisor a duplicate consult would actually reach.
    assert reopened._supervision.supervisor is reopened_supervisor

    # The recovered run continues without a duplicate trigger or consult.
    reopened.step(ScriptedModel(_inspect()))
    kinds = _kinds(reopened)
    assert kinds.count(SUPERVISION_TRIGGERED) == 1
    assert "guidance_received" not in kinds
    assert supervisor.calls == 1
    assert reopened_supervisor.calls == 0
    # Three pre-crash inspections plus one after recovery, each submitted once.
    _assert_no_duplicate_effects(runtime, expected=4)
    _assert_no_active_reservations(reopened)


def test_attempt_transition_crash_keeps_failed_attempt_out_of_lineage(
    dependencies,
) -> None:
    """A crash mid-replan abandons the attempt once and starts a fresh one."""
    journal = dependencies[0]
    runtime = dependencies[3]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)

    for _ in range(3):
        session.step(ScriptedModel(_inspect()))
    session.step(ScriptedModel(_inspect()))  # stage-1 guidance, decision 4
    with _crash_on_event(journal, "attempt_started"):
        with pytest.raises(IntegrityError, match="injected attempt_started crash"):
            session.step(ScriptedModel(_inspect()))  # stage-2 checkpoint/replan

    kinds = _kinds(session)
    assert kinds.count(SUPERVISION_TRIGGERED) == 2
    assert kinds.count("attempt_abandoned") == 1
    assert kinds.count("attempt_started") == 1
    assert len(session.accepted_lineage()) == 1

    reopened = _reopen(dependencies)
    assert replay(reopened.events()) == reopened._state
    assert reopened.view().active_attempt is None
    attempts = reopened.attempts()
    assert len(attempts) == 1
    assert attempts[0].attempt.disposition is AttemptDisposition.ABANDONED
    assert len(reopened.accepted_lineage()) == 1

    # The recovered run starts exactly one fresh attempt; no duplicate abandon.
    reopened.step(ScriptedModel(_inspect()))
    kinds = _kinds(reopened)
    assert kinds.count("attempt_started") == 2
    assert kinds.count("attempt_abandoned") == 1
    assert len(reopened.accepted_lineage()) == 1
    # Four pre-crash inspections plus one after recovery, each submitted once.
    _assert_no_duplicate_effects(runtime, expected=5)


def test_commit_transition_crash_promotes_one_successor(dependencies) -> None:
    """A crash inside the promotion batch still commits exactly one successor."""
    journal = dependencies[0]
    runtime = dependencies[3]
    evaluator = dependencies[4]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)

    for _ in range(3):
        session.step(ScriptedModel(_inspect()))
    session.step(ScriptedModel(_inspect()))  # stage-1 guidance, decision 4
    session.step(ScriptedModel(_focus()))  # stage-2 replan, decision 5 on attempt 2
    kinds = _kinds(session)
    assert kinds.count("attempt_abandoned") == 1
    assert kinds.count("attempt_started") == 2

    with _crash_on_event(journal, "lineage_promoted"):
        with pytest.raises(IntegrityError, match="injected lineage_promoted crash"):
            session.request_evaluation()
    assert len(session.accepted_lineage()) == 1
    assert len(evaluator.requests) == 1

    reopened = _reopen(dependencies)
    reopened.reconcile_pending()
    lineage = reopened.accepted_lineage()
    assert len(lineage) == 2
    assert lineage[-1].parent_id == lineage[0].node_id
    assert len(reopened.attempts()) == 2
    # Recovery replays the durable intake instead of re-evaluating externally.
    assert len(evaluator.requests) == 1
    assert replay(reopened.events()) == reopened._state
    # Five inspection/focus submissions, none repeated by recovery.
    _assert_no_duplicate_effects(runtime, expected=5)


@pytest.mark.parametrize(
    ("decision", "event_kind"),
    (
        (GuidanceDecision.ACCEPT, "guidance_accepted"),
        (GuidanceDecision.REJECT, "guidance_rejected"),
    ),
)
def test_guidance_disposition_crash_keeps_guidance_open(
    dependencies, decision, event_kind
) -> None:
    """A crash while recording a disposition leaves the guidance open, not lost."""
    journal = dependencies[0]
    runtime = dependencies[3]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor, cooldown_calls=4)

    for _ in range(4):
        session.step(ScriptedModel(_inspect()))
    assert _kinds(session).count("guidance_received") == 1

    with _crash_on_event(journal, event_kind):
        with pytest.raises(IntegrityError, match=f"injected {event_kind} crash"):
            session.step(ScriptedModel((GuidanceResponse(decision, "recorded once"),)))
    assert event_kind not in _kinds(session)
    _assert_no_duplicate_effects(runtime, expected=4)

    reopened = _reopen(dependencies, cooldown_calls=4)
    assert replay(reopened.events()) == reopened._state
    reopened.step(ScriptedModel((GuidanceResponse(decision, "recorded once"),)))
    kinds = _kinds(reopened)
    assert kinds.count(event_kind) == 1
    assert kinds.count("guidance_received") == 1
    _assert_no_duplicate_effects(runtime, expected=4)


def test_guidance_response_without_guidance_crash_recovers(dependencies) -> None:
    """A crash recording a stray response changes nothing and replays cleanly."""
    journal = dependencies[0]
    runtime = dependencies[3]
    session = _session(dependencies, _supervisor())
    response = (GuidanceResponse(GuidanceDecision.ACCEPT, "no active guidance"),)

    with _crash_on_event(journal, "guidance_response_rejected"):
        with pytest.raises(
            IntegrityError, match="injected guidance_response_rejected crash"
        ):
            session.step(ScriptedModel(response))
    assert "guidance_response_rejected" not in _kinds(session)
    assert runtime.proposals == []

    reopened = _reopen(dependencies)
    assert replay(reopened.events()) == reopened._state
    reopened.step(ScriptedModel(response))
    assert _kinds(reopened).count("guidance_response_rejected") == 1
    assert runtime.proposals == []


def test_guidance_expiry_crash_expires_once_on_recovery(dependencies) -> None:
    """A crash at the expiry boundary leaves guidance active, then expires it once."""
    journal = dependencies[0]
    supervisor = _supervisor(
        SupervisorResponse(
            _guidance(expires_after_call=4),
            UsageMeasurement("crash-supervisor-usage", model_calls=1),
            "crash-supervisor",
        )
    )
    session = _session(dependencies, supervisor, cooldown_calls=4)
    for _ in range(4):
        session.step(ScriptedModel(_inspect()))
    assert _kinds(session).count("guidance_received") == 1

    with _crash_on_event(journal, "guidance_expired"):
        with pytest.raises(IntegrityError, match="injected guidance_expired crash"):
            session.step(ScriptedModel(_inspect()))
    assert "guidance_expired" not in _kinds(session)

    reopened = _reopen(dependencies, cooldown_calls=4)
    assert replay(reopened.events()) == reopened._state
    reopened.step(ScriptedModel(_inspect()))
    assert _kinds(reopened).count("guidance_expired") == 1


def test_supervisor_failure_crash_recovers_without_duplicate_consult(
    dependencies,
) -> None:
    """A crash recording advisor failure settles once and never re-consults."""
    journal = dependencies[0]
    supervisor = _supervisor()
    session = _session(dependencies, supervisor)
    for _ in range(3):
        session.step(ScriptedModel(_inspect()))

    with _crash_on_event(journal, "supervisor_failed"):
        with pytest.raises(IntegrityError, match="injected supervisor_failed crash"):
            session.step(ScriptedModel(_inspect()))
    assert supervisor.calls == 1
    kinds = _kinds(session)
    assert "supervisor_failed" not in kinds
    assert kinds.count("supervisor_intended") == 1

    reopened_supervisor = _supervisor()
    reopened = _reopen(dependencies, reopened_supervisor)
    kinds = _kinds(reopened)
    assert kinds.count("supervisor_failed") == 1
    assert "guidance_received" not in kinds
    assert reopened_supervisor.calls == 0
    assert replay(reopened.events()) == reopened._state
    _assert_no_active_reservations(reopened)


def test_attempt_abandon_crash_leaves_the_attempt_intact(dependencies) -> None:
    """A crash before abandonment leaves the attempt whole, not half-closed."""
    journal = dependencies[0]
    runtime = dependencies[3]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)
    for _ in range(4):
        session.step(ScriptedModel(_inspect()))
    active = session.view().active_attempt
    assert active is not None
    first_attempt = active.attempt_id

    with _crash_on_event(journal, "attempt_abandoned"):
        with pytest.raises(IntegrityError, match="injected attempt_abandoned crash"):
            session.step(ScriptedModel(_inspect()))
    assert "attempt_abandoned" not in _kinds(session)
    still_active = session.view().active_attempt
    assert still_active is not None
    assert still_active.attempt_id == first_attempt

    reopened = _reopen(dependencies)
    assert replay(reopened.events()) == reopened._state
    reopened_active = reopened.view().active_attempt
    assert reopened_active is not None
    assert reopened_active.attempt_id == first_attempt
    attempts = reopened.attempts()
    assert len(attempts) == 1
    assert attempts[0].attempt.disposition is not AttemptDisposition.ABANDONED
    _assert_no_duplicate_effects(runtime, expected=4)


@pytest.mark.parametrize("event_kind", ("operator_escalated", "question_recorded"))
def test_operator_escalation_crash_does_not_half_apply(
    dependencies, event_kind
) -> None:
    """A crash in the escalation batch leaves no half-recorded operator question."""
    journal = dependencies[0]
    runtime = dependencies[3]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)
    for _ in range(4):
        session.step(ScriptedModel(_inspect()))
    session.step(ScriptedModel(_inspect()))
    assert _kinds(session).count("attempt_abandoned") == 1

    with _crash_on_event(journal, event_kind):
        with pytest.raises(IntegrityError, match=f"injected {event_kind} crash"):
            session.step(ScriptedModel(_inspect()))
    kinds = _kinds(session)
    assert "operator_escalated" not in kinds
    assert "question_recorded" not in kinds
    assert session.view().status is not SessionStatus.WAITING

    reopened = _reopen(dependencies)
    assert replay(reopened.events()) == reopened._state
    kinds = _kinds(reopened)
    assert "operator_escalated" not in kinds
    assert "question_recorded" not in kinds
    assert reopened.view().status is not SessionStatus.WAITING
    _assert_no_duplicate_effects(runtime, expected=5)


def test_supervision_trigger_crash_recovers_without_duplicate_trigger(
    dependencies,
) -> None:
    """A crash recording the trigger leaves no trigger, then records it once."""
    journal = dependencies[0]
    runtime = dependencies[3]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)
    for _ in range(3):
        session.step(ScriptedModel(_inspect()))

    with _crash_on_event(journal, "supervision_triggered"):
        with pytest.raises(
            IntegrityError, match="injected supervision_triggered crash"
        ):
            session.step(ScriptedModel(_inspect()))
    assert "supervision_triggered" not in _kinds(session)
    _assert_no_duplicate_effects(runtime, expected=3)

    reopened = _reopen(dependencies, _supervisor(_supervisor_response()))
    assert replay(reopened.events()) == reopened._state
    _assert_no_active_reservations(reopened)
    reopened.step(ScriptedModel(_inspect()))
    kinds = _kinds(reopened)
    assert kinds.count("supervision_triggered") == 1
    assert kinds.count("guidance_received") == 1
    _assert_no_duplicate_effects(runtime, expected=4)


def test_supervisor_intent_crash_leaves_no_reservation(dependencies) -> None:
    """A crash recording the consult intent leaves no reservation behind."""
    journal = dependencies[0]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)
    for _ in range(3):
        session.step(ScriptedModel(_inspect()))

    with _crash_on_event(journal, "supervisor_intended"):
        with pytest.raises(IntegrityError, match="injected supervisor_intended crash"):
            session.step(ScriptedModel(_inspect()))
    kinds = _kinds(session)
    assert kinds.count("supervision_triggered") == 1
    assert "supervisor_intended" not in kinds

    reopened = _reopen(dependencies)
    assert replay(reopened.events()) == reopened._state
    kinds = _kinds(reopened)
    assert "supervisor_intended" not in kinds
    assert kinds.count("supervision_triggered") == 1
    _assert_no_active_reservations(reopened)


def test_supervisor_accounting_distinguishes_reserved_from_settled(
    dependencies,
) -> None:
    """An unsettled consult reads as reserved, then settles without double counting."""
    journal = dependencies[0]
    supervisor = _supervisor(_supervisor_response())
    session = _session(dependencies, supervisor)
    for _ in range(3):
        session.step(ScriptedModel(_inspect()))
    with _crash_on_event(journal, "guidance_received"):
        with pytest.raises(IntegrityError, match="injected guidance_received crash"):
            session.step(ScriptedModel(_inspect()))

    reserved = session.counters()
    assert reserved.supervisor_calls == 0
    assert reserved.conservative_supervisor_calls == 1
    assert reserved.supervisor_input_tokens is None
    assert reserved.supervisor_output_tokens is None

    reopened = _reopen(dependencies)
    settled = reopened.counters()
    assert settled.supervisor_calls == 1
    assert settled.conservative_supervisor_calls == 1
    assert settled.supervisor_input_tokens is None
    assert settled.supervisor_output_tokens is None
