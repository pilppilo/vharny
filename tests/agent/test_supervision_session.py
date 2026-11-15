"""Session integration for PHASE-0003 supervision item 5."""

from __future__ import annotations

import pytest

from vharness.agent import (
    ActionChoice,
    BudgetLimits,
    CompletionMode,
    GuidanceResponse,
    ScriptedModel,
    Session,
    SessionStatus,
    TaskSpec,
)
from vharness.agent.models import Message
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError, TransitionError
from vharness.agent.journal import SqliteJournal
from vharness.agent.kernel import parse_agent_result
from vharness.agent.models import (
    ActionDefinition,
    AttemptDisposition,
    ComponentIdentity,
    EnvironmentContract,
    ExecutionReceipt,
    ExecutionStatus,
    RuntimeCapabilities,
    SUPERVISOR_REQUEST_PREFIX,
    StateRef,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.supervision import (
    Guidance,
    GuidanceDecision,
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
        self.receipts = {}

    def identity(self):
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal):
        self.proposals.append(proposal)
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=self.status,
            state=StateRef("fake", f"after-{proposal.proposal_id}", digest="1" * 64),
        )
        self.receipts[proposal.proposal_id] = receipt
        return receipt

    def reconcile(self, operation_id):
        receipt = self.receipts.get(operation_id)
        if receipt is None:
            return ReconciliationResult(ReconciliationKind.UNKNOWN)
        return ReconciliationResult(ReconciliationKind.RECEIPT, receipt)


class FakeEvaluator:
    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.requests = []

    def identity(self):
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
        from vharness.agent.models import EvaluationReceipt

        self.requests.append(request)
        return EvaluationReceipt(
            receipt_id=f"evaluation-{request.request_id}",
            request_id=request.request_id,
            objective_version=request.objective_version,
            evaluated_state=request.evaluated_state,
            baseline_node_id=request.baseline_node_id,
            evaluator_version=request.evaluator_version,
            accepted=self.accepted,
        )

    def reconcile(self, request_id):
        return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)


@pytest.fixture
def dependencies(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(
        tmp_path / "artifacts"
    ), FakeEnvironment(), FakeRuntime(), FakeEvaluator()
    journal.close()


def _task(**budgets) -> TaskSpec:
    limits = {"actions": 12, "evaluations": 2}
    limits.update(budgets)
    return TaskSpec(
        task_id="task-1",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(**limits),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def _guidance(**fields) -> Guidance:
    values = {
        "diagnosis": "repeated inspection without new information",
        "constraints": ("avoid rereading main.py",),
        "suggested_experiments": ("read the receipt instead",),
        "expires_after_call": 10,
    }
    values.update(fields)
    return Guidance(**values)


def _supervisor(*responses: SupervisorResponse) -> ScriptedSupervisor:
    return ScriptedSupervisor(responses)


def _guided(
    guidance: Guidance, *, usage: UsageMeasurement | None = None
) -> SupervisorResponse:
    return SupervisorResponse(
        guidance,
        usage if usage is not None else UsageMeasurement("sup-usage-1", model_calls=1),
        "scripted-supervisor",
    )


def _capability(
    supervisor: ScriptedSupervisor, **policy_fields
) -> SupervisionCapability:
    params = {"window_calls": 4, "repeat_threshold": 3, "cooldown_calls": 1}
    params.update(policy_fields)
    # These tests exercise guidance delivery and the escalation ladder on
    # budget-less tasks; the evidence, burn, and checkpoint families need a
    # model-call budget and are covered in test_supervision_p5.py and
    # test_supervision_p6.py.
    params.setdefault("evidence_fingerprints", False)
    params.setdefault("burn_trigger", False)
    params.setdefault("checkpoint_trigger", False)
    return SupervisionCapability(supervisor, MonitorPolicy(**params))


def _session(dependencies, *, capability=None, **budgets) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    return Session.create(
        "session-1",
        _task(**budgets),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=capability,
    )


def _inspect(count: int) -> tuple:
    return tuple(ActionChoice("inspect", f"repeat {index}") for index in range(count))


def _kinds(session: Session) -> list:
    return [event.kind for event in session.events()]


def _manifests(session: Session) -> list:
    return [
        event.payload["manifest"]
        for event in session.events()
        if event.kind == "context_assembled"
    ]


def test_capability_records_enablement_and_reopen_verifies_it(dependencies):
    session = _session(dependencies, capability=_capability(_supervisor()))
    assert "supervision_enabled" in _kinds(session)

    journal, artifacts, environment, runtime, evaluator = dependencies
    reopened = Session.open(
        "session-1",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=_capability(_supervisor()),
    )
    assert reopened.view() == session.view()

    with pytest.raises(ContractError, match="supervision capability must match"):
        Session.open(
            "session-1",
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
        )
    with pytest.raises(ContractError, match="supervision policy does not match"):
        Session.open(
            "session-1",
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            supervision=_capability(_supervisor(), repeat_threshold=2),
        )


def test_supervision_off_keeps_runs_unchanged(dependencies):
    session = _session(dependencies)
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    assert "supervision_enabled" not in _kinds(session)
    assert "supervision_triggered" not in _kinds(session)
    for manifest in _manifests(session):
        assert all(pair[0] != "supervision" for pair in manifest["included"])


def test_trigger_delivers_guidance_into_the_next_decision(dependencies):
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(dependencies, capability=_capability(supervisor))
    for decisions in range(3):
        session.step(ScriptedModel(_inspect(1)))
    assert "supervision_triggered" not in _kinds(session)

    session.step(ScriptedModel(_inspect(1)))
    kinds = _kinds(session)
    trigger = kinds.index("supervision_triggered")
    assert kinds[trigger + 1 : trigger + 3] == [
        "supervisor_intended",
        "guidance_received",
    ]
    assert supervisor.calls == 1
    # Four main calls plus the settled supervisor consult.
    assert session.counters().model_calls == 5
    manifest = _manifests(session)[-1]
    supervision = [pair for pair in manifest["included"] if pair[0] == "supervision"]
    assert len(supervision) == 1
    assert supervision[0][1] == "mandatory"


def test_accept_response_records_reason_and_closes_guidance(dependencies):
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(
        dependencies, capability=_capability(supervisor, cooldown_calls=4)
    )
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    session.step(
        ScriptedModel(
            (GuidanceResponse(GuidanceDecision.ACCEPT, "follows the diagnosis"),)
        )
    )
    events = list(session.events())
    accepted = [event for event in events if event.kind == "guidance_accepted"]
    assert len(accepted) == 1
    assert accepted[0].payload["decision"] == "accept"
    assert accepted[0].payload["reason"] == "follows the diagnosis"
    received = next(event for event in events if event.kind == "guidance_received")
    assert accepted[0].causation_id == received.event_id
    session.step(ScriptedModel(_inspect(1)))
    manifest = _manifests(session)[-1]
    assert all(pair[0] != "supervision" for pair in manifest["included"])


def test_reject_response_records_reason_and_replays_closed_guidance(dependencies):
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(
        dependencies, capability=_capability(supervisor, cooldown_calls=4)
    )
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    response = GuidanceResponse(GuidanceDecision.REJECT, "staying the course")
    session.step(ScriptedModel((response,)))
    rejected = [
        event for event in session.events() if event.kind == "guidance_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0].payload["decision"] == "reject"
    assert rejected[0].payload["reason"] == "staying the course"
    received = next(
        event for event in session.events() if event.kind == "guidance_received"
    )
    assert rejected[0].causation_id == received.event_id

    journal, artifacts, environment, runtime, evaluator = dependencies
    reopened_supervisor = _supervisor()
    reopened = Session.open(
        "session-1",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=_capability(reopened_supervisor, cooldown_calls=4),
    )
    assert any(event.event_id == rejected[0].event_id for event in reopened.events())

    reopened.step(ScriptedModel(_inspect(1)))
    assert reopened_supervisor.calls == 0
    assert _kinds(reopened).count("guidance_received") == 1
    manifest = _manifests(reopened)[-1]
    assert all(pair[0] != "supervision" for pair in manifest["included"])


def test_replan_response_starts_a_fresh_attempt(dependencies):
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(
        dependencies, capability=_capability(supervisor, cooldown_calls=4)
    )
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    first_attempt = session.view().active_attempt.attempt_id
    session.step(
        ScriptedModel((GuidanceResponse(GuidanceDecision.REPLAN, "start over"),))
    )
    kinds = _kinds(session)
    assert kinds.count("attempt_abandoned") == 1
    abandon = kinds.index("attempt_abandoned")
    assert kinds[abandon + 1] == "attempt_started"
    accepted = [
        event for event in session.events() if event.kind == "guidance_accepted"
    ]
    assert accepted[0].payload["decision"] == "replan"
    abandoned = [
        item for item in session.attempts() if item.attempt.attempt_id == first_attempt
    ]
    assert abandoned[0].attempt.disposition is AttemptDisposition.ABANDONED
    assert session.view().active_attempt.attempt_id != first_attempt


def test_response_without_active_guidance_is_rejected(dependencies):
    session = _session(dependencies, capability=_capability(_supervisor()))
    session.step(
        ScriptedModel((GuidanceResponse(GuidanceDecision.ACCEPT, "no guidance"),))
    )
    rejected = [
        event
        for event in session.events()
        if event.kind == "guidance_response_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0].payload["decision"] == "accept"
    assert session.step(ScriptedModel(_inspect(1))).status is not None


def test_guidance_expiry_records_an_event(dependencies):
    supervisor = _supervisor(_guided(_guidance(expires_after_call=4)))
    policy_capability = _capability(supervisor, cooldown_calls=4)
    session = _session(dependencies, capability=policy_capability)
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    session.step(ScriptedModel(_inspect(1)))
    expired = [event for event in session.events() if event.kind == "guidance_expired"]
    assert len(expired) == 1
    assert expired[0].payload["expires_after_call"] == 4
    expired_manifest = _manifests(session)[-1]
    assert all(pair[0] != "supervision" for pair in expired_manifest["included"])


def test_repeated_trigger_checkpoints_and_third_trigger_escalates(dependencies):
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(dependencies, capability=_capability(supervisor))
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    first_attempt = session.view().active_attempt.attempt_id
    # Stage 2: the second trigger replans without consulting the supervisor again.
    session.step(ScriptedModel(_inspect(1)))
    kinds = _kinds(session)
    assert kinds.count("supervision_triggered") == 2
    abandon = kinds.index("attempt_abandoned")
    assert kinds[abandon + 1] == "attempt_started"
    assert supervisor.calls == 1
    assert session.view().active_attempt.attempt_id != first_attempt
    # Stage 3: the third trigger asks the operator and makes no model call.
    decided = kinds.count("model_decided")
    session.step(ScriptedModel(_inspect(1)))
    kinds = _kinds(session)
    escalation = kinds.index("operator_escalated")
    assert kinds[escalation + 1] == "question_recorded"
    assert kinds.count("model_decided") == decided
    assert session.view().status is SessionStatus.WAITING
    question = next(
        event for event in session.events() if event.kind == "question_recorded"
    )
    assert "stage 3" in question.payload["text"]

    session.enqueue(Message("msg-operator", "session-1", "start over from the head"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    session.step(ScriptedModel(_inspect(1)))
    assert session.view().status is SessionStatus.RUNNING


def test_failed_supervisor_consult_records_failure_and_continues(dependencies):
    supervisor = _supervisor()
    session = _session(
        dependencies, capability=_capability(supervisor, cooldown_calls=4)
    )
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    kinds = _kinds(session)
    assert kinds.count("supervisor_intended") == 1
    failed = [event for event in session.events() if event.kind == "supervisor_failed"]
    assert len(failed) == 1
    assert "guidance_received" not in kinds
    assert kinds.count("model_decided") == 4


def test_budget_exhausted_trigger_escalates_to_an_operator_question(dependencies):
    """ADR-0013: a trigger the remaining budget cannot review is a question.

    The stage-1 boundary exposes the trigger as a blocking operator question
    instead of recording a bare supervisor failure; answering resumes, and
    the exhausted budget then ends the step with its typed error.
    """
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(dependencies, capability=_capability(supervisor), model_calls=3)
    for _ in range(3):
        session.step(ScriptedModel(_inspect(1)))
    # The fourth boundary sees three identical decisions with no consult
    # capacity left, so it escalates instead of recording a bare failure.
    session.step(ScriptedModel(_inspect(1)))
    assert session.view().status is SessionStatus.WAITING
    failed = [event for event in session.events() if event.kind == "supervisor_failed"]
    assert failed == []
    assert supervisor.calls == 0
    escalated = next(
        event for event in session.events() if event.kind == "operator_escalated"
    )
    assert escalated.payload["stage"] == 1
    assert escalated.payload["reason"] == (
        "no supervisor call fits the remaining model-call budget"
    )
    question = next(
        event for event in session.events() if event.kind == "question_recorded"
    )
    assert "stage 1" in question.payload["text"]
    session.enqueue(Message("msg-operator", "session-1", "continue without review"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING
    with pytest.raises(TransitionError):
        session.step(ScriptedModel(_inspect(1)))


def test_guidance_response_parses_and_rejects():
    result = parse_agent_result(
        '{"kind": "GuidanceResponse", "decision": "replan", '
        '"reason": "fresh attempt"}'
    )
    assert isinstance(result, GuidanceResponse)
    assert result.decision is GuidanceDecision.REPLAN
    with pytest.raises(ContractError, match="invalid value"):
        parse_agent_result(
            '{"kind": "GuidanceResponse", "decision": "ignore", "reason": "x"}'
        )
    with pytest.raises(ContractError, match="missing required fields"):
        parse_agent_result('{"kind": "GuidanceResponse", "decision": "accept"}')
    with pytest.raises(ContractError):
        GuidanceResponse(GuidanceDecision.ACCEPT, "  ")


def test_system_prompt_names_guidance_response():
    from vharness.agent.context import SYSTEM_PROMPT

    assert "GuidanceResponse" in SYSTEM_PROMPT


def test_supervisor_accounting_is_a_distinct_subset_of_model_calls(dependencies):
    """A consult counts once in the total and once in the supervisor subset."""
    supervisor = _supervisor(
        _guided(
            _guidance(),
            usage=UsageMeasurement(
                "sup-usage-tokens", model_calls=1, input_tokens=120, output_tokens=30
            ),
        )
    )
    session = _session(dependencies, capability=_capability(supervisor))
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))

    counters = session.counters()
    assert counters.model_calls == 5
    assert counters.supervisor_calls == 1
    assert counters.conservative_supervisor_calls == 1
    assert counters.supervisor_input_tokens == 120
    assert counters.supervisor_output_tokens == 30


def test_supervisor_token_dimensions_stay_unknown_when_unreported(dependencies):
    """A consult without token telemetry reports unknown, never zero."""
    supervisor = _supervisor(_guided(_guidance()))
    session = _session(dependencies, capability=_capability(supervisor))
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))

    counters = session.counters()
    assert counters.supervisor_calls == 1
    assert counters.supervisor_input_tokens is None
    assert counters.supervisor_output_tokens is None
    assert counters.conservative_supervisor_calls == 1


def test_supervisor_accounting_is_zero_without_supervision(dependencies):
    """A run with no supervision reports known-zero supervisor usage."""
    session = _session(dependencies)
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))

    counters = session.counters()
    assert counters.supervisor_calls == 0
    assert counters.conservative_supervisor_calls == 0
    assert counters.supervisor_input_tokens == 0
    assert counters.supervisor_output_tokens == 0


def test_supervisor_accounting_survives_reopen(dependencies):
    """Supervisor counters replay exactly across a journal reopen."""
    supervisor = _supervisor(
        _guided(
            _guidance(),
            usage=UsageMeasurement(
                "sup-usage-reopen", model_calls=1, input_tokens=8, output_tokens=4
            ),
        )
    )
    session = _session(dependencies, capability=_capability(supervisor))
    for _ in range(4):
        session.step(ScriptedModel(_inspect(1)))
    before = session.counters()

    journal, artifacts, environment, runtime, evaluator = dependencies
    reopened = Session.open(
        "session-1",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=_capability(_supervisor()),
    )
    assert reopened.counters() == before
    assert reopened.counters().supervisor_calls == 1


def test_submit_action_rejects_a_caller_supplied_supervisor_prefix(dependencies):
    """A caller cannot forge a proposal ID in the reserved supervisor namespace.

    The supervisor reserves the ``supervisor-`` operation prefix for its own
    accounting, so a caller-supplied ID in that namespace would be miscounted as
    supervisor usage. It is rejected before any intent or reservation is
    recorded.
    """
    session = _session(dependencies)
    session.begin_attempt()
    before = len(session.events())

    with pytest.raises(ContractError, match="reserved supervisor prefix"):
        session.submit_action(
            "inspect",
            {},
            rationale="forged supervisor id",
            proposal_id=f"{SUPERVISOR_REQUEST_PREFIX}forged",
        )

    assert len(session.events()) == before


def test_submit_action_accepts_an_ordinary_proposal_id(dependencies):
    """An ordinary caller proposal ID is still accepted and recorded."""
    session = _session(dependencies)
    session.begin_attempt()

    receipt = session.submit_action(
        "inspect", {}, rationale="ordinary id", proposal_id="proposal-caller"
    )

    assert receipt.proposal_id == "proposal-caller"
    assert any(event.kind == "action_intended" for event in session.events())
