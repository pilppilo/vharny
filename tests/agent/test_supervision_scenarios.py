"""Seeded supervision scenarios for PHASE-0003 item 6.

Labeled traces in the EXP-0001 fixture classes (productive, plateaued-exploring,
stalled, repetitive, regressing) exercise the retained EXP-0001 selection
``monitor.p1.v1`` (pinned via ``monitor_policy_p1`` so the ADR-0011 default
switch cannot shift these expectations) and its escalation ladder, and a
session-level scenario shows that repeated failed recovery escalates to an
operator question instead of looping.

Fixtures are scripted, so this suite makes no provider calls. Guidance delivery,
checkpointing, and resume wiring stay covered by ``test_supervision_session.py``;
this file asserts the seeded trigger behavior and the escalation ladder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import pytest

from vharness.agent import (
    ActionChoice,
    ActionProposal,
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
    ActionDefinition,
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
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.supervision import (
    BURN_CAUSE,
    SUPERVISION_TRIGGERED,
    Guidance,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
    action_fingerprint,
    escalation_stage,
    evaluate_supervision,
    monitor_policy_p1,
    receipt_fingerprint,
)

_SEEDS = (0, 1, 2, 3, 4)
# EXP-0001 selection: window 8, repetition threshold 3, cooldown 4.
_POLICY = monitor_policy_p1()
_FIRST_TRIGGER = _POLICY.repeat_threshold + 1
_SECOND_TRIGGER = _FIRST_TRIGGER + _POLICY.cooldown_calls
_RECORDED_AT = "2026-10-01T00:00:00+00:00"
_MONITOR_SESSION = "scenario-monitor"
_PROBE_ERROR = "probe did not change the fixture state"


@dataclass(frozen=True, slots=True)
class Step:
    """One scripted model decision in a labeled scenario trace."""

    action: str
    arguments: tuple[tuple[str, str], ...] = ()
    receipt_status: str = "succeeded"
    receipt_error: str | None = None
    supported_progress: bool = False

    def resolve(self) -> dict[str, str]:
        """Return the action arguments as a plain mapping."""
        return dict(self.arguments)


def _probe(target: str = "a") -> Step:
    return Step("probe", (("target", target),))


def _event(sequence: int, kind: str, payload: Mapping[str, object]) -> Event:
    return Event(
        event_id=f"scenario-event-{sequence}",
        session_id=_MONITOR_SESSION,
        sequence=sequence,
        kind=kind,
        objective_version=1,
        payload=payload,
        recorded_at=_RECORDED_AT,
    )


def _decision_events(sequence: int, step: Step) -> tuple[list[Event], int]:
    """Build the events of one completed decision and the next sequence."""
    events = [
        _event(sequence, "model_intended", {"request_id": f"decision-{sequence}"}),
        _event(sequence + 1, "model_decided", {"result_kind": "ActionChoice"}),
        _event(
            sequence + 2,
            "context_assembled",
            {"manifest": {"serialized_input_tokens": 100}},
        ),
        _event(
            sequence + 3,
            "action_intended",
            {
                "proposal": {
                    "action_name": step.action,
                    "arguments": step.resolve(),
                }
            },
        ),
        _event(
            sequence + 4,
            "receipt_received",
            {"receipt": {"status": step.receipt_status, "error": step.receipt_error}},
        ),
    ]
    cursor = sequence + 5
    if step.supported_progress:
        events.append(
            _event(
                cursor, "progress_claim_recorded", {"claim": {"status": "supported"}}
            )
        )
        cursor += 1
    return events, cursor


def _trigger_event(cursor: int, call: int) -> Event:
    return _event(
        cursor,
        SUPERVISION_TRIGGERED,
        {"call": call, "cause": "repetition", "policy_version": _POLICY.version},
    )


def _decisions(steps: Sequence[Step]) -> list[Event]:
    """Build only the decision events of a trace, without any trigger."""
    events: list[Event] = []
    cursor = 1
    for step in steps:
        decision, cursor = _decision_events(cursor, step)
        events.extend(decision)
    return events


def _simulate(steps: Sequence[Step]) -> tuple[list[Event], list[int]]:
    """Walk every decision boundary, recording each trigger the monitor fires.

    The monitor is evaluated before each decision over the decisions already
    journaled, and a fired trigger is recorded between decisions, exactly as the
    session journals it. Recording the trigger lets the monitor's own cooldown
    rule apply to later boundaries instead of being assumed.
    """
    events: list[Event] = []
    fired: list[int] = []
    cursor = 1
    for call, step in enumerate(steps, start=1):
        if evaluate_supervision(events, call=call, policy=_POLICY) is not None:
            fired.append(call)
            events.append(_trigger_event(cursor, call))
            cursor += 1
        decision, cursor = _decision_events(cursor, step)
        events.extend(decision)
    final = len(steps) + 1
    if evaluate_supervision(events, call=final, policy=_POLICY) is not None:
        fired.append(final)
        events.append(_trigger_event(cursor, final))
    return events, fired


def _productive_trace(seed: int) -> list[Step]:
    """Distinct actions with supported progress: no repetition, no trigger."""
    return [
        Step("inspect", (("target", f"t{seed}"),)),
        Step("focus", supported_progress=True),
        Step("repair", supported_progress=True),
        Step("verify"),
        Step("evaluate"),
    ]


def _plateaued_exploring_trace(seed: int) -> list[Step]:
    """Distinct probes interleaved with productive steps: no repeated fingerprint."""
    return [
        _probe(f"t{seed}-1"),
        _probe(f"t{seed}-2"),
        _probe(f"t{seed}-3"),
        Step("focus"),
        _probe(f"t{seed}-4"),
        _probe(f"t{seed}-5"),
        Step("repair"),
        _probe(f"t{seed}-6"),
        Step("verify"),
        Step("evaluate"),
    ]


def _stalled_trace(seed: int, repeats: int = 10) -> list[Step]:
    """One target probed repeatedly: the repetition monitor's base signal."""
    return [_probe(f"target-{seed}") for _ in range(repeats)]


def _repetitive_trace(repeats: int = 10) -> list[Step]:
    """Distinct actions that all fail the same way: a repeated failure signal."""
    return [
        Step(f"probe-{index}", receipt_status="failed", receipt_error=_PROBE_ERROR)
        for index in range(repeats)
    ]


def _regressing_trace(cycles: int = 3) -> list[Step]:
    """A focus/revert cycle that keeps re-focusing the same target."""
    cycle = [
        Step("focus"),
        Step("revert"),
        Step("focus"),
        Step("evaluate"),
        Step("revert"),
    ]
    return cycle * cycles


@pytest.mark.parametrize("seed", _SEEDS)
def test_productive_trace_never_triggers(seed: int) -> None:
    """Clear progress never thrashes the supervisor."""
    _, fired = _simulate(_productive_trace(seed))
    assert fired == []


@pytest.mark.parametrize("seed", _SEEDS)
def test_plateaued_exploring_trace_does_not_trigger(seed: int) -> None:
    """Exploring distinct targets without repeated fingerprints does not trigger."""
    _, fired = _simulate(_plateaued_exploring_trace(seed))
    assert fired == []


@pytest.mark.parametrize("seed", _SEEDS)
def test_stalled_trace_triggers_at_threshold_then_honors_cooldown(seed: int) -> None:
    """The first trigger lands at the threshold boundary and respects cooldown."""
    _, fired = _simulate(_stalled_trace(seed))
    assert fired == [_FIRST_TRIGGER, _SECOND_TRIGGER]
    gaps = [later - earlier for earlier, later in zip(fired, fired[1:])]
    assert all(gap >= _POLICY.cooldown_calls for gap in gaps)


def test_repetitive_trace_triggers_on_the_repeated_failure() -> None:
    """Repeated failures of distinct actions still form a repetition signal."""
    steps = _repetitive_trace()
    _, fired = _simulate(steps)
    assert fired[0] == _FIRST_TRIGGER
    prefix = _decisions(steps[: _POLICY.repeat_threshold])
    trigger = evaluate_supervision(prefix, call=_FIRST_TRIGGER, policy=_POLICY)
    assert trigger is not None
    assert trigger.fingerprint == receipt_fingerprint("failed", _PROBE_ERROR)


def test_regressing_trace_triggers_on_the_reversion_cycle() -> None:
    """Re-focusing the same target three times within the window fires."""
    steps = _regressing_trace()
    _, fired = _simulate(steps)
    assert fired[0] == 7
    prefix = _decisions(steps[:6])
    trigger = evaluate_supervision(prefix, call=7, policy=_POLICY)
    assert trigger is not None
    assert trigger.fingerprint == action_fingerprint("focus", {})
    gaps = [later - earlier for earlier, later in zip(fired, fired[1:])]
    assert all(gap >= _POLICY.cooldown_calls for gap in gaps)


def test_repeated_failed_recovery_advances_the_escalation_ladder() -> None:
    """Two triggers without supported progress reach the operator stage."""
    events, fired = _simulate(_stalled_trace(0))
    assert len(fired) == 2
    assert escalation_stage(events) == 3


def test_supported_progress_resets_the_escalation_ladder() -> None:
    """Supported progress ends the episode; pending claims never do."""
    events, _ = _simulate(_stalled_trace(0))
    pending = events + [
        _event(1000, "progress_claim_recorded", {"claim": {"status": "pending"}})
    ]
    assert escalation_stage(pending) == 3
    supported = events + [
        _event(1001, "progress_claim_recorded", {"claim": {"status": "supported"}})
    ]
    assert escalation_stage(supported) == 1


class _FakeEnvironment:
    """Single-action mechanical contract for the escalation scenario."""

    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="scenario",
            version="1",
            initial_state=StateRef("scenario", "initial", digest="0" * 64),
            actions=(ActionDefinition("inspect", "Inspect the scenario state"),),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _FakeRuntime:
    """Runtime that records every submitted proposal exactly once."""

    def __init__(self) -> None:
        self.proposals: list[ActionProposal] = []

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("scenario-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self.proposals.append(proposal)
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef(
                "scenario", f"after-{proposal.proposal_id}", digest="1" * 64
            ),
            usage=UsageMeasurement(f"receipt-{proposal.proposal_id}-usage", actions=1),
        )

    def reconcile(self, operation_id) -> ReconciliationResult:
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _FakeEvaluator:
    """Evaluator that never accepts; the scenario measures escalation, not score."""

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("scenario-evaluator", "v1")

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
        task_id="scenario-task",
        objective="escape the repeated inspection fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=12, evaluations=2, model_calls=32),
        environment_id="scenario",
        evaluation_contract_id="scenario-evaluator-v1",
    )


def _supervisor_response() -> SupervisorResponse:
    return SupervisorResponse(
        Guidance(
            diagnosis="repeated inspection without new information",
            constraints=("avoid re-inspecting the same state",),
            suggested_experiments=("change the fixture target",),
            expires_after_call=8,
        ),
        UsageMeasurement("scenario-supervisor-usage", model_calls=1),
        "scenario-supervisor",
    )


def _session(dependencies, supervisor) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    # Cooldown 1 lets the ladder advance within a short scripted run. The
    # retained EXP-0001 composition keeps the ladder expectations independent
    # of the ADR-0011 evidence and burn families (covered in
    # test_supervision_p5.py).
    capability = SupervisionCapability(
        supervisor,
        MonitorPolicy(
            window_calls=4,
            repeat_threshold=3,
            cooldown_calls=1,
            evidence_fingerprints=False,
            burn_trigger=False,
        ),
    )
    return Session.create(
        "scenario-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=capability,
    )


def _inspect() -> tuple[ActionChoice, ...]:
    return (ActionChoice("inspect", "re-inspect the fixture without new information"),)


def test_repeated_failed_recovery_escalates_to_the_operator(dependencies) -> None:
    """Three triggers without supported progress escalate instead of looping."""
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session = _session(dependencies, supervisor)
    runtime = dependencies[3]

    for _ in range(4):
        session.step(ScriptedModel(_inspect()))
    kinds = [event.kind for event in session.events()]
    assert kinds.count(SUPERVISION_TRIGGERED) == 1
    assert "guidance_received" in kinds

    session.step(ScriptedModel(_inspect()))
    kinds = [event.kind for event in session.events()]
    assert kinds.count(SUPERVISION_TRIGGERED) == 2
    abandoned = kinds.index("attempt_abandoned")
    assert kinds[abandoned + 1] == "attempt_started"
    assert supervisor.calls == 1

    decided = kinds.count("model_decided")
    session.step(ScriptedModel(_inspect()))
    kinds = [event.kind for event in session.events()]
    assert kinds.count(SUPERVISION_TRIGGERED) == 3
    escalated = kinds.index("operator_escalated")
    assert kinds[escalated + 1] == "question_recorded"
    assert kinds.count("model_decided") == decided
    assert session.view().status is SessionStatus.WAITING

    proposal_ids = [proposal.proposal_id for proposal in runtime.proposals]
    assert len(proposal_ids) == len(set(proposal_ids))

    session.enqueue(Message("scenario-msg-1", "scenario-session", "change the target"))
    session.advance()
    assert session.view().status is SessionStatus.RUNNING


def test_burn_cause_climbs_the_ladder_to_the_operator(dependencies) -> None:
    """Three claim-free burn triggers climb the ladder like repetitions.

    The Timing attempt-3 run died on the first burn trigger's persist; this
    scenario walks the burn cause through guidance, checkpoint/replan, and
    the operator escalation, then replays the journal.
    """
    journal, artifacts, environment, runtime, evaluator = dependencies
    policy = MonitorPolicy(
        window_calls=4,
        repeat_threshold=3,
        cooldown_calls=1,
        evidence_fingerprints=True,
        burn_trigger=True,
        burn_fraction=0.05,
        checkpoint_trigger=False,
    )
    supervisor = ScriptedSupervisor((_supervisor_response(),))
    session = Session.create(
        "scenario-burn-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(supervisor, policy),
    )

    def _burn_inspect(index: int) -> ScriptedModel:
        # Distinct arguments keep action fingerprints distinct, so every
        # trigger on this evidence-free run comes from the burn family.
        return ScriptedModel(
            (
                ActionChoice(
                    "inspect",
                    "re-inspect the fixture without new information",
                    {"target": f"target-{index}"},
                ),
            )
        )

    for index in range(3):
        session.step(_burn_inspect(index))
    kinds = [event.kind for event in session.events()]
    assert kinds.count(SUPERVISION_TRIGGERED) == 1
    assert "guidance_received" in kinds

    session.step(_burn_inspect(3))
    kinds = [event.kind for event in session.events()]
    assert kinds.count(SUPERVISION_TRIGGERED) == 2
    abandoned = kinds.index("attempt_abandoned")
    assert kinds[abandoned + 1] == "attempt_started"
    assert supervisor.calls == 1

    decided = kinds.count("model_decided")
    session.step(_burn_inspect(4))
    kinds = [event.kind for event in session.events()]
    assert kinds.count(SUPERVISION_TRIGGERED) == 3
    triggers = [
        event for event in session.events() if event.kind == SUPERVISION_TRIGGERED
    ]
    assert all(event.payload["cause"] == BURN_CAUSE for event in triggers)
    escalated = kinds.index("operator_escalated")
    assert kinds[escalated + 1] == "question_recorded"
    assert kinds.count("model_decided") == decided
    assert session.view().status is SessionStatus.WAITING

    session.enqueue(
        Message("scenario-burn-msg-1", "scenario-burn-session", "change the target")
    )
    session.advance()
    assert session.view().status is SessionStatus.RUNNING

    reopened = Session.open(
        "scenario-burn-session",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(ScriptedSupervisor(()), policy),
    )
    reopened_kinds = [event.kind for event in reopened.events()]
    assert reopened_kinds.count(SUPERVISION_TRIGGERED) == 3
    assert reopened_kinds.count("operator_escalated") == 1
