"""Kernel coverage for the ADR-0011 switch to ``monitor.p5.v1``.

The shipped default composes the retained EXP-0001 repetition policy with the
EXP-0002 R1/R2 evidence-repeat fingerprints and the B2 claim-free burn
trigger. These tests pin the composition, the frozen stdout normalization,
the journaled and reader-fallback evidence paths, and the burn boundaries
against the pre-registered EXP-0002 semantics.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping

import pytest

from vharness.agent import (
    ActionChoice,
    ActionDefinition,
    BudgetLimits,
    CompletionMode,
    ScriptedModel,
    Session,
    TaskSpec,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.errors import ContractError
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ActionProposal,
    ArtifactRef,
    ComponentIdentity,
    EnvironmentContract,
    Event,
    EvaluationReceipt,
    ExecutionReceipt,
    ExecutionStatus,
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
    MONITOR_POLICY_P5_VERSION,
    Guidance,
    MonitorPolicy,
    ScriptedSupervisor,
    SupervisionCapability,
    SupervisorResponse,
    evaluate_supervision,
    facts_history,
    monitor_policy_p1,
    monitor_policy_p5,
    normalize_stdout,
    policy_from_payload,
    policy_payload,
    stdout_fingerprints,
)

_SESSION_ID = "session-p5"
_RECORDED_AT = "2026-11-01T00:00:00+00:00"


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


class _Store:
    """In-memory artifact reader for the evidence-fingerprint fallback."""

    # pylint: disable=too-few-public-methods
    def __init__(self, blobs: Mapping[str, bytes]) -> None:
        self._blobs = dict(blobs)

    def read(self, reference: ArtifactRef) -> bytes:
        return self._blobs[reference.digest]


def _artifact(stdout: bytes, provenance: str = "test-exec") -> ArtifactRef:
    return ArtifactRef(
        digest=hashlib.sha256(stdout).hexdigest(),
        size=len(stdout),
        media_type="text/plain",
        provenance=provenance,
    )


def _serialized(reference: ArtifactRef) -> dict[str, object]:
    return {
        "digest": reference.digest,
        "size": reference.size,
        "media_type": reference.media_type,
        "provenance": reference.provenance,
    }


def _exec_decision(
    sequence: int,
    *,
    action: str = "exec",
    journaled: tuple[str, ...] | None = None,
    artifact: ArtifactRef | None = None,
) -> tuple[list[Event], int]:
    """Build one decision with an optional evidence-bearing successful receipt."""
    proposal: dict[str, object] = {
        "proposal_id": f"proposal-{sequence}",
        "action_name": action,
        # Distinct arguments keep the action fingerprints distinct, so an
        # evidence-fingerprint repeat is the unambiguous repetition winner.
        "arguments": {"command": f"probe-{sequence}"},
    }
    receipt: dict[str, object] = {
        "proposal_id": f"proposal-{sequence}",
        "status": "succeeded",
    }
    if artifact is not None:
        receipt["raw_artifact"] = _serialized(artifact)
    payload: dict[str, object] = {"proposal": proposal, "receipt": receipt}
    if journaled is not None:
        payload["evidence_fingerprints"] = list(journaled)
    events = [
        _event(sequence, "model_intended", {"request_id": f"decision-{sequence}"}),
        _event(sequence + 1, "model_decided", {"result_kind": "ActionChoice"}),
        _event(
            sequence + 2,
            "context_assembled",
            {"manifest": {"serialized_input_tokens": 100}},
        ),
        _event(sequence + 3, "action_intended", {"proposal": proposal}),
        _event(sequence + 4, "receipt_received", payload),
    ]
    return events, sequence + 5


def test_retained_p5_policy_is_the_adopted_adr_0011_composition() -> None:
    policy = monitor_policy_p5()
    assert MONITOR_POLICY_P5_VERSION == "monitor.p5.v1"
    assert policy.version == MONITOR_POLICY_P5_VERSION
    assert (policy.window_calls, policy.repeat_threshold, policy.cooldown_calls) == (
        8,
        3,
        4,
    )
    assert policy.evidence_fingerprints is True
    assert policy.burn_trigger is True
    assert policy.checkpoint_trigger is False
    assert policy.burn_fraction == 0.6


def test_monitor_policy_p1_retains_the_exp_0001_selection() -> None:
    policy = monitor_policy_p1()
    assert policy.version == "monitor.p1.v1"
    assert (policy.window_calls, policy.repeat_threshold, policy.cooldown_calls) == (
        8,
        3,
        4,
    )
    assert policy.evidence_fingerprints is False
    assert policy.burn_trigger is False


def test_policy_payload_round_trips_the_p5_composition() -> None:
    policy = MonitorPolicy()
    assert policy_from_payload(policy_payload(policy)) == policy


def test_legacy_payload_without_flags_parses_as_p1() -> None:
    legacy = {
        "version": "monitor.p1.v1",
        "window_calls": 8,
        "repeat_threshold": 3,
        "cooldown_calls": 4,
    }
    assert policy_from_payload(legacy) == monitor_policy_p1()


def test_policy_validation_rejects_inconsistent_compositions() -> None:
    with pytest.raises(ContractError):
        MonitorPolicy(evidence_fingerprints=False, burn_trigger=True)
    with pytest.raises(ContractError):
        MonitorPolicy(burn_fraction=0.0)
    with pytest.raises(ContractError):
        MonitorPolicy(burn_fraction=1.5)


def test_normalize_stdout_strips_transport_headers_and_echo_markers() -> None:
    raw = "HTTP/1.1 200 OK\nServer: test\n\n----\nAmount: 5\n----\n"
    assert normalize_stdout(raw) == "amount: 5"


def test_normalize_stdout_marker_only_churn_is_empty() -> None:
    assert normalize_stdout("----\n----\n") == ""


def test_stdout_fingerprints_hash_strict_and_normalized_variants() -> None:
    payload = b"Result: ALPHA\nalpha\n"
    strict, normalized = stdout_fingerprints(payload, _Store({}))
    assert strict == f"stdout-strict:{hashlib.sha256(payload).hexdigest()}"
    expected = hashlib.sha256(
        normalize_stdout("Result: ALPHA\nalpha\n").encode("utf-8")
    ).hexdigest()
    assert normalized == f"stdout-normalized:{expected}"


def test_stdout_fingerprints_extract_stdout_stream_artifacts() -> None:
    stdout = b"worker ready\n"
    reference = _artifact(stdout, "job-output")
    envelope = json.dumps(
        {
            "output_artifacts": [
                {"stream": "stdout", "offset": 0, "artifact": _serialized(reference)}
            ]
        }
    ).encode("utf-8")
    store = _Store({reference.digest: stdout})
    strict, _ = stdout_fingerprints(envelope, store)
    assert strict == f"stdout-strict:{hashlib.sha256(stdout).hexdigest()}"


def test_stdout_fingerprints_read_inline_result_envelopes() -> None:
    envelope = json.dumps({"job_id": "job-1", "stdout": "probe\n"}).encode("utf-8")
    strict, _ = stdout_fingerprints(envelope, _Store({}))
    expected = hashlib.sha256(b"probe\n").hexdigest()
    assert strict == f"stdout-strict:{expected}"


def test_evidence_repetition_fires_through_journaled_fingerprints() -> None:
    fingerprint = f"stdout-strict:{'a' * 64}"
    events: list[Event] = []
    cursor = 1
    for _ in range(3):
        decision, cursor = _exec_decision(cursor, journaled=(fingerprint,))
        events.extend(decision)
    trigger = evaluate_supervision(tuple(events), call=4)
    assert trigger is not None
    assert trigger.cause == "repetition"
    assert trigger.fingerprint == fingerprint
    assert trigger.repeat_count == 3


def test_reader_fallback_computes_the_same_fingerprints() -> None:
    stdout = b"probe output\n"
    reference = _artifact(stdout)
    events: list[Event] = []
    cursor = 1
    for _ in range(3):
        decision, cursor = _exec_decision(cursor, artifact=reference)
        events.extend(decision)
    store = _Store({reference.digest: stdout})
    expected = stdout_fingerprints(stdout, store)
    trigger = evaluate_supervision(tuple(events), call=4, artifacts=store)
    assert trigger is not None
    assert trigger.cause == "repetition"
    # R1 and R2 both repeat three times; the deterministic tie-break may name
    # either, so the trigger must name one of the evidence digests.
    assert trigger.fingerprint in expected
    assert trigger.repeat_count == 3
    history = facts_history(tuple(events), store)
    assert [fact.evidence_fingerprints for fact in history] == [expected] * 3


def test_reader_fallback_ignores_non_evidence_actions() -> None:
    stdout = b"file content\n"
    reference = _artifact(stdout)
    events: list[Event] = []
    cursor = 1
    for _ in range(3):
        decision, cursor = _exec_decision(cursor, action="write", artifact=reference)
        events.extend(decision)
    store = _Store({reference.digest: stdout})
    history = facts_history(tuple(events), store)
    assert all(fact.evidence_fingerprints == () for fact in history)
    assert (
        evaluate_supervision(
            tuple(events), call=4, artifacts=store, model_call_budget=100
        )
        is None
    )


def test_retained_p1_policy_ignores_evidence_fingerprints() -> None:
    fingerprint = f"stdout-strict:{'b' * 64}"
    events: list[Event] = []
    cursor = 1
    for _ in range(3):
        decision, cursor = _exec_decision(cursor, journaled=(fingerprint,))
        events.extend(decision)
    journal = tuple(events)
    assert evaluate_supervision(journal, call=4, policy=monitor_policy_p1()) is None
    assert evaluate_supervision(journal, call=4) is not None


def test_burn_fires_at_the_predeclared_boundary() -> None:
    decisions: list[list[Event]] = []
    cursor = 1
    for index in range(6):
        decision, cursor = _exec_decision(cursor, action=f"action-{index}")
        decisions.append(decision)
    assert (
        evaluate_supervision(
            tuple(event for decision in decisions[:5] for event in decision),
            call=6,
            policy=monitor_policy_p5(),
            model_call_budget=10,
        )
        is None
    )
    journal = tuple(event for decision in decisions for event in decision)
    trigger = evaluate_supervision(
        journal, call=7, policy=monitor_policy_p5(), model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == BURN_CAUSE
    assert trigger.fingerprint is None
    assert trigger.repeat_count is None
    assert trigger.actions_used == 6
    assert set(trigger.input_event_ids) == set(event.event_id for event in journal)


def test_first_seen_evidence_resets_the_burn_clock() -> None:
    fresh = f"stdout-strict:{'c' * 64}"
    decisions: list[list[Event]] = []
    cursor = 1
    for index in range(9):
        decision, cursor = _exec_decision(
            cursor,
            action=f"action-{index}",
            journaled=(fresh,) if index == 2 else None,
        )
        decisions.append(decision)
    # Threshold ceil(0.6 * 10) = 6; the fresh evidence at call 3 resets the
    # clock, so the burn fires at call 10 instead of call 7.
    assert (
        evaluate_supervision(
            tuple(event for decision in decisions[:8] for event in decision),
            call=9,
            policy=monitor_policy_p5(),
            model_call_budget=10,
        )
        is None
    )
    journal = tuple(event for decision in decisions for event in decision)
    trigger = evaluate_supervision(
        journal, call=10, policy=monitor_policy_p5(), model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == BURN_CAUSE


def test_repeated_evidence_does_not_reset_the_burn_clock() -> None:
    repeated = f"stdout-strict:{'d' * 64}"
    decisions: list[list[Event]] = []
    cursor = 1
    for index in range(7):
        decision, cursor = _exec_decision(
            cursor,
            action=f"action-{index}",
            journaled=(repeated,) if index < 2 else None,
        )
        decisions.append(decision)
    # Only the first occurrence at call 1 counts as progress; the repeat at
    # call 2 never resets the clock (otherwise the fire would move to call
    # 9), and two repeats stay below the repetition threshold so the burn
    # trigger is the family that fires.
    assert (
        evaluate_supervision(
            tuple(event for decision in decisions[:6] for event in decision),
            call=7,
            policy=monitor_policy_p5(),
            model_call_budget=10,
        )
        is None
    )
    journal = tuple(event for decision in decisions for event in decision)
    trigger = evaluate_supervision(
        journal, call=8, policy=monitor_policy_p5(), model_call_budget=10
    )
    assert trigger is not None
    assert trigger.cause == BURN_CAUSE


def test_burn_shares_the_repetition_cooldown() -> None:
    decisions: list[list[Event]] = []
    cursor = 1
    for index in range(7):
        decision, cursor = _exec_decision(cursor, action=f"action-{index}")
        decisions.append(decision)
    trigger_event = _event(
        cursor,
        "supervision_triggered",
        {
            "call": 4,
            "policy_version": MONITOR_POLICY_P5_VERSION,
            "cause": "repetition",
        },
    )
    # A recorded trigger at call 4 (budget 4 -> threshold 3): call 5 is inside
    # the shared cooldown, call 8 re-arms and the burn fires.
    assert (
        evaluate_supervision(
            tuple(event for decision in decisions[:4] for event in decision)
            + (trigger_event,),
            call=5,
            policy=monitor_policy_p5(),
            model_call_budget=4,
        )
        is None
    )
    journal = tuple(event for decision in decisions for event in decision) + (
        trigger_event,
    )
    trigger = evaluate_supervision(
        journal, call=8, policy=monitor_policy_p5(), model_call_budget=4
    )
    assert trigger is not None
    assert trigger.cause == BURN_CAUSE


def test_repetition_takes_precedence_over_burn() -> None:
    fingerprint = f"stdout-strict:{'e' * 64}"
    events: list[Event] = []
    cursor = 1
    for _ in range(3):
        decision, cursor = _exec_decision(cursor, journaled=(fingerprint,))
        events.extend(decision)
    trigger = evaluate_supervision(tuple(events), call=4, model_call_budget=4)
    assert trigger is not None
    assert trigger.cause == "repetition"
    assert trigger.fingerprint == fingerprint


def test_burn_refuses_to_evaluate_without_a_budget() -> None:
    events: list[Event] = []
    cursor = 1
    for index in range(2):
        decision, cursor = _exec_decision(cursor, action=f"action-{index}")
        events.extend(decision)
    with pytest.raises(ContractError):
        evaluate_supervision(tuple(events), call=3)


class _ExecEnvironment:
    """Mechanical contract exposing one exec and one inspect action."""

    # pylint: disable=too-few-public-methods
    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="p5",
            version="1",
            initial_state=StateRef("p5", "initial", digest="0" * 64),
            actions=(
                ActionDefinition("exec", "Run a probe command"),
                ActionDefinition("inspect", "Inspect the scenario state"),
            ),
            capabilities=RuntimeCapabilities(idempotency=True, reconciliation=True),
        )


class _ExecRuntime:
    """Runtime staging a constant stdout artifact for exec proposals."""

    def __init__(self, store: ArtifactStore) -> None:
        self._store = store
        self.proposals: list[ActionProposal] = []

    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("p5-runtime", "v1")

    def submit(self, proposal: ActionProposal) -> ExecutionReceipt:
        self.proposals.append(proposal)
        artifact = (
            self._store.put(
                b"probe output\n",
                media_type="text/plain",
                provenance="test-exec",
            )
            if proposal.action_name == "exec"
            else None
        )
        return ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=ExecutionStatus.SUCCEEDED,
            state=StateRef("p5", f"after-{proposal.proposal_id}", digest="1" * 64),
            usage=UsageMeasurement(f"receipt-{proposal.proposal_id}-usage", actions=1),
            raw_artifact=artifact,
        )

    def reconcile(self, operation_id: str) -> ReconciliationResult:
        return ReconciliationResult(ReconciliationKind.UNKNOWN)


class _Evaluator:
    """Evaluator that never accepts; the scenario measures signals, not score."""

    # pylint: disable=too-few-public-methods
    def identity(self) -> ComponentIdentity:
        return ComponentIdentity("p5-evaluator", "v1")

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
        task_id="p5-session-task",
        objective="exercise evidence fingerprinting",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=12, evaluations=2, model_calls=32),
        environment_id="p5",
        evaluation_contract_id="p5-evaluator-v1",
    )


def _guidance() -> SupervisorResponse:
    return SupervisorResponse(
        Guidance(
            diagnosis="repeated probe output without new information",
            constraints=("vary the probe",),
            suggested_experiments=("change the command",),
            expires_after_call=8,
        ),
        UsageMeasurement("p5-supervisor-usage", model_calls=1),
        "p5-supervisor",
    )


def _session(
    dependencies, supervisor: ScriptedSupervisor, policy: MonitorPolicy | None = None
) -> Session:
    journal, artifacts, environment, runtime, evaluator = dependencies
    capability = (
        SupervisionCapability(supervisor)
        if policy is None
        else SupervisionCapability(supervisor, policy)
    )
    return Session.create(
        "p5-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=capability,
    )


def test_session_journals_evidence_fingerprints_at_receipt_time(dependencies) -> None:
    session = _session(dependencies, ScriptedSupervisor(()))
    session.step(ScriptedModel((ActionChoice("inspect", "look around"),)))
    session.step(
        ScriptedModel((ActionChoice("exec", "run the probe", {"command": "probe-1"}),))
    )
    receipts = [event for event in session.events() if event.kind == "receipt_received"]
    assert len(receipts) == 2
    assert "evidence_fingerprints" not in receipts[0].payload
    fingerprints = receipts[1].payload["evidence_fingerprints"]
    assert fingerprints == stdout_fingerprints(b"probe output\n", dependencies[1])


def test_reopened_session_replays_fingerprints_from_the_journal(dependencies) -> None:
    session = _session(dependencies, ScriptedSupervisor(()))
    session.step(
        ScriptedModel((ActionChoice("exec", "run the probe", {"command": "probe-1"}),))
    )
    journal, store, environment, runtime, evaluator = dependencies
    reopened = Session.open(
        "p5-session",
        journal=journal,
        artifacts=store,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(ScriptedSupervisor(())),
    )
    history = facts_history(tuple(reopened.events()))
    bearing = [fact for fact in history if fact.evidence_fingerprints]
    assert len(bearing) == 1
    assert bearing[0].evidence_fingerprints == stdout_fingerprints(
        b"probe output\n", store
    )


def test_p5_repetition_fires_on_repeated_probe_output(dependencies) -> None:
    session = _session(dependencies, ScriptedSupervisor((_guidance(),)))
    # The commands differ, so the identical probe output is the only repeating
    # fingerprint and the evidence family is the unambiguous trigger cause.
    # The monitor evaluates at the boundary before each decision, so the
    # trigger fires at the start of the fourth step.
    for index in range(4):
        session.step(
            ScriptedModel(
                (ActionChoice("exec", "run the probe", {"command": f"probe-{index}"}),)
            )
        )
    events = session.events()
    kinds = [event.kind for event in events]
    assert kinds.count("supervision_triggered") == 1
    trigger = next(event for event in events if event.kind == "supervision_triggered")
    assert trigger.payload["cause"] == "repetition"
    # R1 and R2 tie on three repeats; the trigger names one of them.
    assert str(trigger.payload["fingerprint"]).startswith(
        ("stdout-strict:", "stdout-normalized:")
    )
    assert "guidance_received" in kinds


def test_p5_burn_trigger_persists_guides_and_replays(dependencies) -> None:
    """The burn-caused trigger persists, guides, and replays from the journal.

    The Timing attempt-3 acceptance run died exactly here: the replay
    validator accepted only the repetition cause, so the live persist of the
    B2 budget-burn trigger raised IntegrityError before any guidance was
    delivered.
    """
    policy = MonitorPolicy(
        window_calls=4,
        repeat_threshold=3,
        cooldown_calls=4,
        evidence_fingerprints=True,
        burn_trigger=True,
        burn_fraction=0.1,
        checkpoint_trigger=False,
    )
    session = _session(dependencies, ScriptedSupervisor((_guidance(),)), policy)
    # Distinct inspect arguments keep action fingerprints distinct, so the
    # evidence-free burn family is the unambiguous trigger cause.
    for index in range(5):
        session.step(
            ScriptedModel(
                (ActionChoice("inspect", "look around", {"target": f"target-{index}"}),)
            )
        )
    events = session.events()
    kinds = [event.kind for event in events]
    assert kinds.count("supervision_triggered") == 1
    trigger = next(event for event in events if event.kind == "supervision_triggered")
    assert trigger.payload["cause"] == BURN_CAUSE
    assert trigger.payload["fingerprint"] is None
    assert trigger.payload["repeat_count"] is None
    assert trigger.payload["call"] == 5
    assert "guidance_received" in kinds
    # The run survives the trigger: the boundary still reaches a decision.
    session.step(
        ScriptedModel((ActionChoice("inspect", "look around", {"target": "target-5"}),))
    )
    assert [event.kind for event in session.events()].count("model_decided") == 6

    journal, store, environment, runtime, evaluator = dependencies
    reopened = Session.open(
        "p5-session",
        journal=journal,
        artifacts=store,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        supervision=SupervisionCapability(ScriptedSupervisor(()), policy),
    )
    reopened_kinds = [event.kind for event in reopened.events()]
    assert reopened_kinds.count("supervision_triggered") == 1
    assert "guidance_received" in reopened_kinds
