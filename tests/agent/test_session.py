"""End-to-end checks for the phase-one session library."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib

import pytest

from vharness.agent import (
    ActionDefinition,
    BudgetLimits,
    ComponentIdentity,
    CompletionMode,
    EvaluationContract,
    EvaluationObjective,
    ObjectiveDirection,
    EnvironmentContract,
    EvaluationReceipt,
    ExecutionReceipt,
    RuntimeCapabilities,
    RunIdentity,
    RecordEnvelope,
    Session,
    SessionStatus,
    StateRef,
    TaskSpec,
    Observation,
    KnowledgeSource,
    LineageQuery,
    ScriptedModel,
)
from vharness.agent.artifacts import ArtifactStore
from vharness.agent.codec import canonical_json, parse_json_object
from vharness.agent.errors import (
    ContractError,
    IntegrityError,
    PersistenceError,
    TransitionError,
)
from vharness.agent.evidence import MetricAvailability, metrics
from vharness.agent.journal import SqliteJournal
from vharness.agent.models import (
    ArtifactRef,
    EvaluationCommand,
    EvaluationRequest,
    Event,
    ExecutionStatus,
    Message,
    Pause,
    Resume,
    ResourceReservation,
    UsageMeasurement,
)
from vharness.agent.ports import (
    EvaluationReconciliationResult,
    ReconciliationKind,
    ReconciliationResult,
)
from vharness.agent.replay import projection_payload, reduce_event, replay
from vharness.agent.session import RECEIPT_CONTENT_MAX_RECEIPTS
from vharness.agent.session_state import (
    envelope_from_payload,
    evaluation_contract_from_payload,
    receipt_from_payload,
    usage_from_payload,
)
from vharness.agent.transitions import UsageLedger
from vharness.agent.fakes import (
    CandidateRestorableEnvironment,
    TrajectoryOnlyEnvironment,
)
from vharness.agent.demo import main as demo_main


@dataclass
class FakeEnvironment:
    """A deterministic contract source used without external effects."""

    state: StateRef = StateRef("fake", "initial", digest="0" * 64)
    capabilities: RuntimeCapabilities = RuntimeCapabilities(
        idempotency=True, reconciliation=True
    )

    def describe(self) -> EnvironmentContract:
        return EnvironmentContract(
            environment_id="fake",
            version="1",
            initial_state=self.state,
            actions=(ActionDefinition("inspect", "Inspect current state"),),
            capabilities=self.capabilities,
        )


class FakeRuntime:
    """Records submitted proposals and returns a configured receipt status."""

    def __init__(self, status: ExecutionStatus = ExecutionStatus.SUCCEEDED) -> None:
        self.status = status
        self.proposals = []
        self.receipts = {}
        self.raise_after_submit = False
        self.reconciliation_kind = ReconciliationKind.RECEIPT
        self.reconcile_calls = 0
        self.state_revision = None

    def identity(self):
        return ComponentIdentity("fake-runtime", "v1")

    def submit(self, proposal):
        self.proposals.append(proposal)
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=self.status,
            state=StateRef(
                "fake",
                f"after-{proposal.proposal_id}",
                digest="1" * 64,
                revision=self.state_revision,
            ),
        )
        self.receipts[proposal.proposal_id] = receipt
        if self.raise_after_submit:
            raise RuntimeError("transport interrupted after external submit")
        return receipt

    def reconcile(self, operation_id):
        self.reconcile_calls += 1
        receipt = self.receipts.get(operation_id)
        if self.reconciliation_kind is not ReconciliationKind.RECEIPT:
            return ReconciliationResult(self.reconciliation_kind)
        if receipt is None:
            return ReconciliationResult(ReconciliationKind.UNKNOWN)
        return ReconciliationResult(ReconciliationKind.RECEIPT, receipt)


class FakeEvaluator:
    """Returns external acceptance for the exact request it receives."""

    def __init__(self, accepted: bool = True) -> None:
        self.accepted = accepted
        self.requests = []
        self.evidence = ()
        self.last_receipt = None
        self.raise_before_evaluate = False
        self.raise_after_evaluate = False
        self.reconciliation_kind = ReconciliationKind.RECEIPT
        self.valid = True
        self.hard_constraints = {}
        self.completed = False
        self.completion_evidence = ()
        self.required_evidence = ()
        self.usage = None
        self.raw_receipt = None

    def identity(self):
        return ComponentIdentity("fake-evaluator", "v1")

    def evaluate(self, request):
        if self.raise_before_evaluate:
            raise RuntimeError("transport interrupted before external evaluation")
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
            evidence=self.evidence,
            valid=self.valid,
            hard_constraints=self.hard_constraints,
            completed=self.completed,
            completion_evidence=self.completion_evidence,
            required_evidence=self.required_evidence,
            usage=self.usage,
            raw_receipt=self.raw_receipt,
        )
        self.last_receipt = receipt
        if self.raise_after_evaluate:
            raise RuntimeError("transport interrupted after external evaluation")
        return receipt

    def reconcile(self, request_id):
        if self.reconciliation_kind is not ReconciliationKind.RECEIPT:
            return EvaluationReconciliationResult(self.reconciliation_kind)
        if self.last_receipt is None or self.last_receipt.request_id != request_id:
            return EvaluationReconciliationResult(ReconciliationKind.UNKNOWN)
        return EvaluationReconciliationResult(
            ReconciliationKind.RECEIPT, self.last_receipt
        )


@pytest.fixture
def dependencies(tmp_path):
    journal = SqliteJournal(tmp_path / "session.sqlite3")
    yield journal, ArtifactStore(
        tmp_path / "artifacts"
    ), FakeEnvironment(), FakeRuntime(), FakeEvaluator()
    journal.close()


def _task() -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=2, evaluations=1),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def test_session_commits_only_externally_accepted_successor_and_replays(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-1",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    session.begin_attempt("attempt-1")
    receipt = session.submit_action("inspect", {}, rationale="need an observation")
    session.request_evaluation(receipt.state)

    view = session.view()
    assert view.lineage_head.parent_id == "root-session-1"
    assert view.lineage_head.evaluation_receipt_id is not None
    assert view.active_attempt is None
    assert [event.kind for event in session.events()] == [
        "session_created",
        "attempt_started",
        "action_intended",
        "receipt_received",
        "evaluation_intended",
        "evaluation_intake",
        "evaluation_received",
        "lineage_promoted",
    ]
    assert len(session.attempts()) == 1
    assert session.attempts()[0].event_start_cursor == 2
    assert session.attempts()[0].event_end_cursor == 8
    assert len(session.accepted_lineage()) == 2
    assert session.counters().attempts == 1
    assert session.counters().commits == 1
    assert session.manifest().environment_id == "fake"
    reopened = Session.open(
        "session-1",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert reopened.view() == view
    assert len(runtime.proposals) == 1


def test_scripted_session_controls_actions_evaluation_checkpoint_and_reopen(
    dependencies,
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    completion_evidence = artifacts.put(
        b"completed", media_type="text/plain", provenance="fake"
    )
    evaluator.completed = True
    evaluator.completion_evidence = (completion_evidence,)
    evaluator.required_evidence = (completion_evidence.digest,)
    session = Session.create(
        "session-scripted-integration",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    script = (
        ("control", Pause("pause-scripted", session.session_id)),
        ("control", Resume("resume-scripted", session.session_id)),
        ("attempt", "attempt-scripted"),
        ("action", "first observation"),
        ("action", "second observation"),
        ("evaluation", None),
        ("checkpoint", None),
    )

    for step, value in script:
        if step == "control":
            session.enqueue(value)
            session.advance()
        elif step == "attempt":
            session.begin_attempt(value)
        elif step == "action":
            session.submit_action("inspect", {}, rationale=value)
        elif step == "evaluation":
            session.request_evaluation()
        else:
            session.checkpoint()

    opened = Session.open(
        session.session_id,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert session.view().status is SessionStatus.COMPLETED
    assert len(runtime.proposals) == 2
    assert len(evaluator.requests) == 1
    assert opened._state == replay(session.events()) == session._state
    assert opened.view() == session.view()


def test_checkpoint_plus_suffix_replay_matches_opened_state(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-checkpoint-suffix",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-checkpoint-suffix")
    session.submit_action("inspect", {}, rationale="before checkpoint")
    checkpoint = session.checkpoint()
    prefix = session.events()
    assert checkpoint.cursor == prefix[-2].sequence

    session.submit_action("inspect", {}, rationale="after checkpoint")
    suffix_replay = replay(session.events())
    opened = Session.open(
        session.session_id,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    assert replay(prefix) != suffix_replay
    assert opened._state == suffix_replay == session._state


@pytest.mark.parametrize(
    "environment",
    [CandidateRestorableEnvironment(), TrajectoryOnlyEnvironment()],
)
def test_deterministic_environment_profiles_preserve_evidence_on_reopen(
    dependencies, environment
):
    journal, artifacts, _environment, runtime, evaluator = dependencies
    task = replace(_task(), environment_id=environment.environment_id)
    session = Session.create(
        f"session-{environment.environment_id}",
        task,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    session.submit_action("inspect", {}, rationale="deterministic evidence")
    session.checkpoint()
    reopened = Session.open(
        f"session-{environment.environment_id}",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    assert reopened.manifest() == session.manifest()
    assert reopened.counters() == session.counters()
    assert replay(reopened.events()) == reopened._state
    assert reopened.view().lineage_head.state.restorable is (
        isinstance(environment, CandidateRestorableEnvironment)
    )


@pytest.mark.parametrize("profile", ["candidate", "trajectory"])
def test_opt_in_demo_runs_each_deterministic_profile(tmp_path, profile, capsys):
    assert (
        demo_main(["--profile", profile, "--directory", str(tmp_path / profile)]) == 0
    )
    output = capsys.readouterr().out
    assert f"environment_id='{profile}-" in output
    assert "actions=1" in output
    assert "evaluations=1" in output
    assert "status=<SessionStatus.COMPLETED: 'completed'>" in output


@pytest.mark.parametrize("arguments", [[], ["--profile", "invalid"]])
def test_opt_in_demo_rejects_invalid_arguments(arguments):
    with pytest.raises(SystemExit):
        demo_main(arguments)


def test_run_identity_persists_through_replay_and_reopen(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    identity = RunIdentity(
        package_version="test-package-v1",
        configuration_digest="a" * 64,
        runtime=ComponentIdentity("fake-runtime", "v1"),
        evaluator=ComponentIdentity("fake-evaluator", "v1"),
    )
    session = Session.create(
        "identity-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        run_identity=identity,
    )

    reopened = Session.open(
        "identity-session",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        run_identity=identity,
    )

    assert session.manifest().package_version == "test-package-v1"
    assert session.manifest().runtime == identity.runtime
    assert session.manifest().envelope.session_id == "identity-session"
    assert reopened.manifest() == session.manifest()


def test_runtime_raw_artifact_survives_strict_receipt_decoding():
    artifact = ArtifactRef("d" * 64, 3, "application/json", "external runtime")
    receipt = ExecutionReceipt(
        "receipt-1",
        "proposal-1",
        1,
        ExecutionStatus.SUCCEEDED,
        raw_artifact=artifact,
    )

    decoded = receipt_from_payload(parse_json_object(canonical_json(receipt)))

    assert decoded.raw_artifact == artifact


def test_evidence_reads_every_page_for_long_sessions(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "long-evidence-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    for _ in range(225):
        session.checkpoint()

    reopened = Session.open(
        "long-evidence-session",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    assert len(session.events()) == 100
    assert session.counters().checkpoints == 225
    assert reopened.counters() == session.counters()


def test_reopen_rejects_a_different_environment_contract(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    Session.create(
        "dependency-identity-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    changed_environment = FakeEnvironment(
        state=StateRef("fake", "changed", digest="2" * 64)
    )

    with pytest.raises(ContractError, match="environment contract differs"):
        Session.open(
            "dependency-identity-session",
            journal=journal,
            artifacts=artifacts,
            environment=changed_environment,
            runtime=runtime,
            evaluator=evaluator,
        )


def test_creation_contract_inputs_survive_replay_and_reopen(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    envelope = RecordEnvelope(
        "observation-1",
        "contract-input-session",
        1,
        "2026-09-07T00:00:00+00:00",
        "test",
    )
    observation = Observation(
        "observation-1", environment.state, envelope.recorded_at, "test", envelope
    )
    source_envelope = RecordEnvelope(
        "source-1", "contract-input-session", 1, envelope.recorded_at, "test"
    )
    source = KnowledgeSource("source-1", "test", envelope.recorded_at, source_envelope)
    contract_envelope = RecordEnvelope(
        "fake-evaluator-v1", "contract-input-session", 1, envelope.recorded_at, "test"
    )
    contract = EvaluationContract(
        "fake-evaluator-v1",
        ComponentIdentity("fake-evaluator", "v1"),
        contract_envelope,
        objectives=(EvaluationObjective("score", ObjectiveDirection.MAXIMIZE),),
        hard_constraints=("valid",),
    )
    session = Session.create(
        "contract-input-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
        initial_observation=observation,
        knowledge_sources=(source,),
        evaluation_contract=contract,
    )
    reopened = Session.open(
        "contract-input-session",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    state = replay(reopened.events())
    assert state.initial_observation == observation
    assert state.knowledge_sources == (source,)
    assert state.evaluation_contract == contract


@pytest.mark.parametrize("record_kind", ["observation", "knowledge"])
def test_creation_rejects_missing_artifact_inputs(dependencies, record_kind):
    journal, artifacts, environment, runtime, evaluator = dependencies
    envelope = RecordEnvelope(
        record_kind, "artifact-input-session", 1, "2026-09-07T00:00:00+00:00", "test"
    )
    missing = ArtifactRef("a" * 64, 1, "application/json", "test")
    kwargs = (
        {
            "initial_observation": Observation(
                "observation",
                environment.state,
                envelope.recorded_at,
                "test",
                envelope,
                missing,
            )
        }
        if record_kind == "observation"
        else {
            "knowledge_sources": (
                KnowledgeSource(
                    "knowledge", "test", envelope.recorded_at, envelope, missing
                ),
            )
        }
    )
    with pytest.raises(IntegrityError, match="required artifact is missing"):
        Session.create(
            "artifact-input-session",
            _task(),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            **kwargs,
        )


@pytest.mark.parametrize(
    "field",
    ["session_id", "objective_version", "record_id", "recorded_at", "provenance"],
)
def test_creation_rejects_mismatched_observation_envelope(dependencies, field):
    journal, artifacts, environment, runtime, evaluator = dependencies
    values = {
        "record_id": "observation-1",
        "session_id": "envelope-session",
        "objective_version": 1,
        "recorded_at": "2026-09-07T00:00:00+00:00",
        "provenance": "test",
    }
    values[field] = {
        "session_id": "other",
        "objective_version": 2,
        "record_id": "other",
        "recorded_at": "other",
        "provenance": "other",
    }[field]
    envelope = RecordEnvelope(**values)
    observation = Observation(
        "observation-1",
        environment.state,
        "2026-09-07T00:00:00+00:00",
        "test",
        envelope,
    )
    with pytest.raises(ContractError, match="creation input"):
        Session.create(
            "envelope-session",
            _task(),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            initial_observation=observation,
        )


def test_evaluation_contract_rejects_duplicate_objectives_and_invalid_direction():
    envelope = {
        "record_id": "contract",
        "session_id": "session",
        "objective_version": 1,
        "recorded_at": "2026-09-07T00:00:00+00:00",
        "provenance": "test",
        "causation_id": None,
        "correlation_id": None,
        "schema_version": 1,
    }
    payload = {
        "contract_id": "contract",
        "evaluator": {"component_id": "eval", "version": "v1"},
        "envelope": envelope,
        "acceptance_semantics": "external",
        "hard_constraints": [],
        "required_evidence": [],
        "schema_version": 1,
        "objectives": [
            {"name": "score", "direction": "maximize"},
            {"name": "score", "direction": "minimize"},
        ],
    }
    with pytest.raises(IntegrityError, match="objective names"):
        evaluation_contract_from_payload(payload)
    payload["objectives"] = [{"name": "score", "direction": "sideways"}]
    with pytest.raises(IntegrityError, match="invalid evaluation contract"):
        evaluation_contract_from_payload(payload)


def test_creation_rejects_corrupt_observation_artifact(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    reference = artifacts.put(b"ok", media_type="application/json", provenance="test")
    artifacts._path_for(reference.digest).write_bytes(b"corrupt")
    envelope = RecordEnvelope(
        "observation", "corrupt-input", 1, "2026-09-07T00:00:00+00:00", "test"
    )
    observation = Observation(
        "observation",
        environment.state,
        envelope.recorded_at,
        "test",
        envelope,
        reference,
    )
    with pytest.raises(IntegrityError, match="artifact verification failed"):
        Session.create(
            "corrupt-input",
            _task(),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
            initial_observation=observation,
        )


def test_reopen_rejects_connector_substitution(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    Session.create(
        "substitution",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    replacement = FakeRuntime()
    replacement.identity = lambda: ComponentIdentity("replacement", "v1")
    with pytest.raises(ContractError, match="identity"):
        Session.open(
            "substitution",
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=replacement,
            evaluator=evaluator,
        )


def test_replay_rejects_forged_creation_envelope(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "forged-creation",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    events = list(session.events())
    payload = dict(events[0].payload)
    observation = dict(payload["initial_observation"])
    envelope = dict(observation["envelope"])
    envelope["record_id"] = "forged"
    observation["envelope"] = envelope
    payload["initial_observation"] = observation
    events[0] = replace(events[0], payload=payload)
    with pytest.raises(IntegrityError, match="envelope"):
        replay(events)


def test_versioned_envelope_retains_extensions_and_rejects_unknown_versions():
    payload = {
        "record_id": "observation-1",
        "session_id": "session-1",
        "objective_version": 1,
        "recorded_at": "2026-09-06T00:00:00+00:00",
        "provenance": "external environment",
        "causation_id": "action-1",
        "correlation_id": "attempt-1",
        "schema_version": 1,
        "future_field": {"preserved": True},
    }

    envelope = envelope_from_payload(payload)

    assert envelope.extensions == {"future_field": {"preserved": True}}
    with pytest.raises(
        IntegrityError, match="unsupported record envelope schema_version"
    ):
        envelope_from_payload({**payload, "schema_version": 2})


def test_evaluation_contract_round_trips_its_versioned_envelope():
    envelope = {
        "record_id": "contract-1",
        "session_id": "session-1",
        "objective_version": 1,
        "recorded_at": "2026-09-06T00:00:00+00:00",
        "provenance": "external evaluator",
        "causation_id": None,
        "correlation_id": None,
        "schema_version": 1,
        "future_field": "preserved",
    }
    contract = evaluation_contract_from_payload(
        {
            "contract_id": "evaluator-v1",
            "evaluator": {"component_id": "evaluator", "version": "v1"},
            "objectives": [{"name": "score", "direction": "maximize"}],
            "hard_constraints": ["valid output"],
            "acceptance_semantics": "external evaluator judgment",
            "required_evidence": ["result"],
            "envelope": envelope,
            "schema_version": 1,
        }
    )

    assert contract.envelope is not None
    assert contract.envelope.extensions == {"future_field": "preserved"}


def test_replay_preserves_explicit_compatible_event_extensions(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "extension-session",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    events = list(session.events())
    created = events[0]
    events[0] = replace(
        created,
        payload={**created.payload, "extensions": {"future_field": "retained"}},
    )

    assert replay(events).task == session.view().task
    assert events[0].payload["extensions"] == {"future_field": "retained"}


def test_admitted_command_reopens_and_applies_once(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause("pause-1", "session-command")
    assert session.enqueue(command) == "pause-1"
    reopened = Session.open(
        "session-command",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert reopened.advance().status is SessionStatus.PAUSED
    assert reopened.enqueue(command) == "pause-1"
    assert [event.kind for event in reopened.events()].count("session_paused") == 1


def test_control_transition_and_disposition_share_one_journal_batch(
    dependencies, monkeypatch
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-batch",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause("pause-batch", session.session_id)
    session.enqueue(command)
    append_batch = journal.append_batch
    batches = []

    def record_batch(events, *, expected_cursor):
        batch = tuple(events)
        batches.append(tuple(event.kind for event in batch))
        return append_batch(batch, expected_cursor=expected_cursor)

    monkeypatch.setattr(journal, "append_batch", record_batch)
    assert session.advance().status is SessionStatus.PAUSED
    assert batches == [("session_paused", "command_applied")]


@pytest.mark.parametrize("boundary", ["admission", "transition", "acknowledgement"])
def test_command_crash_boundaries_never_partially_apply_a_control(
    dependencies, monkeypatch, boundary
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        f"session-command-crash-{boundary}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause(f"pause-{boundary}", session.session_id)

    if boundary == "admission":

        def fail_admission(_events, *, expected_cursor):
            raise PersistenceError("injected admission crash")

        monkeypatch.setattr(journal, "append_batch", fail_admission)
        with pytest.raises(PersistenceError, match="admission crash"):
            session.enqueue(command)
        assert [event.kind for event in session.events()] == ["session_created"]
        monkeypatch.undo()
        recovered = Session.open(
            session.session_id,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
        )
        recovered.enqueue(command)
    else:
        session.enqueue(command)
        if boundary == "transition":

            def fail_transition(_command):
                raise RuntimeError("injected transition crash")

            monkeypatch.setattr(session, "_prepare_command_transition", fail_transition)
            with pytest.raises(RuntimeError, match="transition crash"):
                session.advance()
            monkeypatch.undo()
        else:
            journal._connection.execute("""
                CREATE TRIGGER abort_command_acknowledgement
                BEFORE INSERT ON events
                WHEN NEW.kind = 'command_applied'
                BEGIN
                    SELECT RAISE(ABORT, 'injected acknowledgement crash');
                END
                """)  # pylint: disable=protected-access
            with pytest.raises(IntegrityError, match="injected acknowledgement crash"):
                session.advance()
            journal._connection.execute(  # pylint: disable=protected-access
                "DROP TRIGGER abort_command_acknowledgement"
            )
        recovered = Session.open(
            session.session_id,
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
        )

    assert recovered.advance().status is SessionStatus.PAUSED
    assert [event.kind for event in recovered.events()].count("session_paused") == 1
    assert [event.kind for event in recovered.events()].count("command_applied") == 1


def test_reopen_uses_only_command_dispositions_to_rebuild_completion(
    dependencies, tmp_path
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-disposition",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause("pause-disposition", session.session_id)
    session.enqueue(command)
    session.advance()
    partial_events = session.events()[:-1]
    recovered_journal = SqliteJournal(tmp_path / "command-disposition.sqlite3")
    try:
        recovered_journal.append(partial_events, expected_cursor=0)
        recovered = Session.open(
            session.session_id,
            journal=recovered_journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
        )
        with pytest.raises(
            TransitionError, match="command rejected after a transition"
        ):
            recovered.advance()
    finally:
        recovered_journal.close()


def test_evaluation_command_durably_intends_before_external_dispatch(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-evaluation",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    command = EvaluationCommand("evaluate-command", session.session_id, receipt.state)
    session.enqueue(command)
    session.advance()

    kinds = [event.kind for event in session.events()]
    intended = kinds.index("evaluation_intended")
    assert kinds[intended : intended + 2] == [
        "evaluation_intended",
        "command_applied",
    ]
    assert evaluator.requests and evaluator.requests[0].evaluated_state == receipt.state


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("without_admission", "has no admission"),
        ("without_transition", "without a transition"),
        ("different_command", "without a transition"),
        ("duplicate_disposition", "already recorded"),
        ("reused_command_id", "already admitted"),
    ],
)
def test_replay_rejects_forged_command_lifecycle(dependencies, corruption, message):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-corruption",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause("pause-command-corruption", session.session_id)
    session.enqueue(command)
    session.advance()
    created, admitted, transition, applied = session.events()

    if corruption == "without_admission":
        events = [created, replace(applied, sequence=2)]
    elif corruption == "without_transition":
        events = [created, admitted, replace(applied, sequence=3)]
    elif corruption == "different_command":
        other = Message(command.command_id + "-other", session.session_id, "other")
        other_admission = replace(
            admitted,
            event_id="event-other-command-admitted",
            sequence=3,
            payload={"command": parse_json_object(canonical_json(other))},
        )
        events = [
            created,
            admitted,
            other_admission,
            replace(transition, sequence=4),
            replace(
                applied,
                sequence=5,
                causation_id=other.command_id,
                payload={"command_id": other.command_id},
            ),
        ]
    elif corruption == "duplicate_disposition":
        events = [
            created,
            admitted,
            transition,
            applied,
            replace(applied, event_id="event-duplicate-disposition", sequence=5),
        ]
    else:
        conflicting = Message(command.command_id, session.session_id, "conflicting")
        events = [
            created,
            admitted,
            replace(
                admitted,
                event_id="event-conflicting-command-admission",
                sequence=3,
                payload={"command": parse_json_object(canonical_json(conflicting))},
            ),
        ]

    with pytest.raises(IntegrityError, match=message):
        replay(events)


def test_replay_rejects_substituted_command_type_at_control_transition(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-type-corruption",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause("pause-type-corruption", session.session_id)
    session.enqueue(command)
    session.advance()
    events = list(session.events())
    transition = events[-2]
    substituted = Message(command.command_id, session.session_id, "substituted")
    events[-2] = replace(
        transition,
        payload={"command": parse_json_object(canonical_json(substituted))},
    )

    with pytest.raises(IntegrityError, match="does not match admission"):
        replay(events)


@pytest.mark.parametrize("command_kind", ["message", "evaluation"])
def test_replay_rejects_altered_command_content_or_evaluation_state(
    dependencies, command_kind
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        f"session-command-altered-{command_kind}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    if command_kind == "message":
        command = Message("message-altered", session.session_id, "original")
        session.enqueue(command)
        session.advance()
        events = list(session.events())
        transition = events[-2]
        altered = Message(command.command_id, session.session_id, "altered")
        events[-2] = replace(
            transition,
            payload={"command": parse_json_object(canonical_json(altered))},
        )
        expected = "does not match admission"
    else:
        session.begin_attempt()
        receipt = session.submit_action("inspect", {}, rationale="candidate")
        command = EvaluationCommand(
            "evaluation-altered", session.session_id, receipt.state
        )
        session.enqueue(command)
        session.advance()
        events = list(session.events())
        index = next(
            item
            for item, event in enumerate(events)
            if event.kind == "evaluation_intended"
        )
        payload = dict(events[index].payload)
        request = dict(payload["request"])
        evaluated_state = dict(request["evaluated_state"])
        evaluated_state["value"] = "altered-state"
        request["evaluated_state"] = evaluated_state
        payload["request"] = request
        events[index] = replace(events[index], payload=payload)
        expected = "state does not match request"

    with pytest.raises(IntegrityError, match=expected):
        replay(events)


def test_replay_rejects_admission_from_another_session(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-session-corruption",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    command = Pause("pause-session-corruption", session.session_id)
    session.enqueue(command)
    events = list(session.events())
    admission = events[-1]
    foreign = Pause(command.command_id, "other-session")
    events[-1] = replace(
        admission,
        payload={"command": parse_json_object(canonical_json(foreign))},
    )

    with pytest.raises(IntegrityError, match="another session"):
        replay(events)


def test_second_action_uses_attempt_result_state(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-sequential",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    session.submit_action("inspect", {}, rationale="first")
    session.submit_action("inspect", {}, rationale="second")
    assert (
        runtime.proposals[1].expected_state
        == runtime.receipts[runtime.proposals[0].proposal_id].state
    )


def test_operational_artifact_reads_are_repeated_and_session_scoped(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    reference = artifacts.put(b"bytes", media_type="text/plain", provenance="test")
    sessions = tuple(
        Session.create(
            f"session-read-{index}",
            _task(),
            journal=journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
        )
        for index in (1, 2)
    )

    assert sessions[0]._read_artifact(reference, "context_observation") == b"bytes"
    assert sessions[0]._read_artifact(reference, "context_observation") == b"bytes"
    assert sessions[1]._read_artifact(reference, "evaluation_evidence") == b"bytes"
    before_failure = len(sessions[0].events())
    with pytest.raises(IntegrityError, match="missing"):
        sessions[0]._read_artifact(
            ArtifactRef("f" * 64, 1, "text/plain", "missing"),
            "context_observation",
        )

    first_reads = [
        event for event in sessions[0].events() if event.kind == "artifact_read"
    ]
    second_reads = [
        event for event in sessions[1].events() if event.kind == "artifact_read"
    ]
    assert len(first_reads) == 2
    assert len(second_reads) == 1
    assert all(event.session_id == sessions[0].session_id for event in first_reads)
    assert second_reads[0].session_id == sessions[1].session_id
    assert len(sessions[0].events()) == before_failure
    assert replay(sessions[0].events()) == sessions[0]._state


def test_context_selection_duration_is_recorded_outside_the_manifest(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-context-duration",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    session.step(ScriptedModel((LineageQuery(),)))

    event = next(
        event for event in session.events() if event.kind == "context_assembled"
    )
    assert isinstance(event.payload["duration_ns"], int)
    assert event.payload["duration_ns"] >= 0
    assert "duration_ns" not in event.payload["manifest"]
    result = metrics(session.events())
    assert result.facts["context_selection_duration_ms"].availability is (
        MetricAvailability.KNOWN
    )
    duration_values = result.facts["context_selection_duration_ms"].value
    assert isinstance(duration_values, tuple)
    assert len(duration_values) == 1


def test_evaluation_evidence_read_is_durable_before_successful_receipt(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evidence = artifacts.put(b"proof", media_type="text/plain", provenance="test")
    evaluator.evidence = (evidence,)
    session = Session.create(
        "session-evidence-read",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(action.state)

    events = session.events()
    read = next(event for event in events if event.kind == "artifact_read")
    received = next(event for event in events if event.kind == "evaluation_received")
    assert read.payload["purpose"] == "evaluation_evidence"
    assert read.payload["bytes_read"] == len(b"proof")
    assert read.sequence < received.sequence
    assert session.view().lineage_head.parent_id == "root-session-evidence-read"
    assert replay(events) == session._state


def test_evaluation_intake_is_durable_when_evidence_verification_fails(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evaluator.evidence = (ArtifactRef("f" * 64, 1, "text/plain", "fixture"),)
    evaluator.usage = UsageMeasurement("failed-evaluation-usage", evaluations=2)
    session = Session.create(
        "session-evidence",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    with pytest.raises(IntegrityError, match="missing"):
        session.request_evaluation(receipt.state)
    kinds = [event.kind for event in session.events()]
    assert "evaluation_intake" in kinds
    assert "evaluation_verification_failed" in kinds
    assert "evaluation_received" not in kinds
    assert "artifact_read" not in kinds
    intake = next(
        event for event in session.events() if event.kind == "evaluation_intake"
    )
    assert intake.payload["raw_receipt"] == canonical_json(evaluator.last_receipt)
    from vharness.agent.transitions import EvaluationOperationStatus

    assert (
        replay(session.events()).evaluation_operations[0].status
        is EvaluationOperationStatus.VERIFICATION_FAILED
    )
    assert session._state.ledger.evaluations == 2
    events = list(session.events())
    repeated_intake = replace(
        intake,
        event_id="event-repeated-failed-intake",
        sequence=len(events) + 1,
    )
    with pytest.raises(IntegrityError, match="follows verification failure"):
        replay([*events, repeated_intake])


def test_action_schema_enforces_declared_properties(dependencies):
    journal, artifacts, _environment, runtime, evaluator = dependencies

    @dataclass
    class StrictEnvironment:
        def describe(self) -> EnvironmentContract:
            return EnvironmentContract(
                environment_id="fake",
                version="1",
                initial_state=StateRef("fake", "initial", digest="0" * 64),
                actions=(
                    ActionDefinition(
                        "inspect",
                        "Inspect current state",
                        {"type": "object", "additionalProperties": False},
                    ),
                ),
                capabilities=RuntimeCapabilities(),
            )

    session = Session.create(
        "session-schema",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=StrictEnvironment(),
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(ContractError, match="undeclared"):
        session.submit_action("inspect", {"unexpected": 1}, rationale="invalid")
    assert runtime.proposals == []


def test_action_schema_rejects_unsupported_nested_constructs(dependencies):
    journal, artifacts, _environment, runtime, evaluator = dependencies

    @dataclass
    class UnsupportedSchemaEnvironment:
        def describe(self) -> EnvironmentContract:
            return EnvironmentContract(
                environment_id="fake",
                version="1",
                initial_state=StateRef("fake", "initial", digest="0" * 64),
                actions=(
                    ActionDefinition(
                        "inspect",
                        "Inspect current state",
                        {"properties": {"mode": {"type": "string", "enum": ["x"]}}},
                    ),
                ),
                capabilities=RuntimeCapabilities(),
            )

    session = Session.create(
        "session-nested-schema",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=UnsupportedSchemaEnvironment(),
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(ContractError, match="unsupported constructs"):
        session.submit_action("inspect", {}, rationale="invalid schema")
    assert runtime.proposals == []


def test_action_schema_enforces_integer_bounds(dependencies):
    journal, artifacts, _environment, runtime, evaluator = dependencies

    @dataclass
    class BoundedEnvironment:
        def describe(self) -> EnvironmentContract:
            return EnvironmentContract(
                environment_id="fake",
                version="1",
                initial_state=StateRef("fake", "initial", digest="0" * 64),
                actions=(
                    ActionDefinition(
                        "step",
                        "Bounded discrete step",
                        {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 3,
                                }
                            },
                            "required": ("action",),
                            "additionalProperties": False,
                        },
                    ),
                ),
                capabilities=RuntimeCapabilities(),
            )

    session = Session.create(
        "session-bounded-schema",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=BoundedEnvironment(),
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(ContractError, match="must be <= 3"):
        session.submit_action("step", {"action": 4}, rationale="out of bounds")
    assert runtime.proposals == []
    session.submit_action("step", {"action": 0}, rationale="legal discrete action")
    assert len(runtime.proposals) == 1


def test_steering_makes_late_receipts_evidence_without_advancing_new_objective(
    dependencies,
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        "session-2",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    pending = session.submit_action("inspect", {}, rationale="inspect before steering")
    assert session.view().status is SessionStatus.WAITING

    from vharness.agent.models import Steer

    session.enqueue(Steer("steer-1", "session-2", "new objective", "new evidence"))
    session.advance()
    late = ExecutionReceipt(
        receipt_id="late-receipt",
        proposal_id=pending.proposal_id,
        objective_version=1,
        status=ExecutionStatus.SUCCEEDED,
        state=StateRef("fake", "late", digest="2" * 64),
    )
    session.receive_receipt(late)

    view = session.view()
    assert view.task.objective_version == 2
    assert view.lineage_head.node_id == "root-session-2"
    assert view.pending_proposal_id is None
    assert any(event.kind == "receipt_received" for event in session.events())
    attempt = session.attempts()[0]
    assert attempt.attempt.disposition.value == "abandoned"
    assert attempt.event_end_cursor is not None
    assert replay(session.events()) == session._state


def test_unknown_reconciliation_never_resubmits_pending_action(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        "session-3",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    session.submit_action("inspect", {}, rationale="may be interrupted")
    runtime.receipts.clear()

    reopened = Session.open(
        "session-3",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    reopened.reconcile_pending()
    assert len(runtime.proposals) == 1
    assert reopened.view().status is SessionStatus.WAITING
    assert "unknown" in (reopened.view().wait_reason or "")


def test_reconciled_nonterminal_action_is_polled_until_terminal(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    runtime.raise_after_submit = True
    session = Session.create(
        "session-reconcile-nonterminal",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(RuntimeError, match="transport interrupted"):
        session.submit_action("inspect", {}, rationale="recover polling")
    proposal = runtime.proposals[0]
    runtime.raise_after_submit = False

    session.reconcile_pending()
    assert session.view().pending_proposal_id == proposal.proposal_id
    assert runtime.reconcile_calls == 1

    runtime.receipts[proposal.proposal_id] = ExecutionReceipt(
        receipt_id=f"terminal-{proposal.proposal_id}",
        proposal_id=proposal.proposal_id,
        objective_version=proposal.objective_version,
        status=ExecutionStatus.SUCCEEDED,
    )
    session.reconcile_pending()

    assert runtime.reconcile_calls == 2
    assert session.view().pending_proposal_id is None
    assert session._state.action_operations[0].settled


@pytest.mark.parametrize("corruption", ["operation_id", "missing_revision"])
def test_reconciliation_rejects_invalid_receipts_before_persistence_and_recovers(
    dependencies, corruption
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    environment.capabilities = RuntimeCapabilities(
        idempotency=True, reconciliation=True, revisions=True
    )
    environment.state = StateRef("fake", "revisioned", revision="1")
    runtime.state_revision = "2"
    runtime.raise_after_submit = True
    session = Session.create(
        f"session-invalid-reconciliation-{corruption}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(RuntimeError, match="transport interrupted"):
        session.submit_action("inspect", {}, rationale="recover validation")
    proposal = runtime.proposals[0]
    valid = runtime.receipts[proposal.proposal_id]
    runtime.raise_after_submit = False
    if corruption == "operation_id":
        invalid = replace(valid, receipt_id="invalid-operation", operation_id="other")
        error = "operation ID"
    else:
        invalid = replace(
            valid,
            receipt_id="invalid-revision",
            state=StateRef("fake", "missing-revision"),
        )
        error = "state revision"
    runtime.receipts[proposal.proposal_id] = invalid

    with pytest.raises(TransitionError, match=error):
        session.reconcile_pending()
    assert all(event.kind != "operation_reconciled" for event in session.events())

    runtime.receipts[proposal.proposal_id] = valid
    session.reconcile_pending()
    assert session.view().pending_proposal_id is None
    assert any(event.kind == "operation_reconciled" for event in session.events())


def test_late_nonterminal_and_contradictory_terminal_receipts_remain_evidence(
    dependencies,
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-monotonic-receipts",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    terminal = session.submit_action("inspect", {}, rationale="terminal first")
    session.receive_receipt(
        ExecutionReceipt(
            "late-running",
            terminal.proposal_id,
            terminal.objective_version,
            ExecutionStatus.RUNNING,
        )
    )
    session.receive_receipt(
        ExecutionReceipt(
            "contradictory-failure",
            terminal.proposal_id,
            terminal.objective_version,
            ExecutionStatus.FAILED,
            state=StateRef("fake", "contradictory", digest="2" * 64),
        )
    )

    operation = session._state.action_operations[0]
    assert operation.receipt_status == ExecutionStatus.SUCCEEDED.value
    assert operation.settled
    assert session.view().active_attempt is not None
    assert session.view().active_attempt.result_state == terminal.state
    assert [receipt.status for receipt in operation.receipts] == [
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.FAILED,
    ]
    assert session._state.ledger.actions == 0
    session.submit_action("inspect", {}, rationale="continue from first terminal")
    assert runtime.proposals[-1].expected_state == terminal.state


def test_revision_semantics_apply_only_when_declared(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    environment.capabilities = RuntimeCapabilities(
        idempotency=True, reconciliation=True, revisions=True
    )
    session = Session.create(
        "session-revisions-required",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(ContractError, match="expected state revision"):
        session.submit_action("inspect", {}, rationale="requires revision")

    environment.state = StateRef("fake", "revisioned", revision="1")
    revisioned = Session.create(
        "session-revisions-present",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    runtime.state_revision = "2"
    revisioned.begin_attempt()
    revisioned.submit_action("inspect", {}, rationale="revision supplied")

    revision_events = list(revisioned.events())
    action_index = next(
        index
        for index, event in enumerate(revision_events)
        if event.kind == "action_intended"
    )
    action_event = revision_events[action_index]
    action_payload = dict(action_event.payload)
    proposal = dict(action_payload["proposal"])
    expected_state = dict(proposal["expected_state"])
    expected_state["revision"] = None
    proposal["expected_state"] = expected_state
    action_payload["proposal"] = proposal
    revision_events[action_index] = replace(action_event, payload=action_payload)
    with pytest.raises(IntegrityError, match="expected state revision"):
        replay(revision_events)

    receipt_index = next(
        index
        for index, event in enumerate(revisioned.events())
        if event.kind == "receipt_received"
    )
    receipt_events = list(revisioned.events())
    receipt_event = receipt_events[receipt_index]
    receipt_payload = dict(receipt_event.payload)
    receipt = dict(receipt_payload["receipt"])
    receipt_state = dict(receipt["state"])
    receipt_state["revision"] = None
    receipt["state"] = receipt_state
    receipt_payload["receipt"] = receipt
    receipt_events[receipt_index] = replace(receipt_event, payload=receipt_payload)
    with pytest.raises(IntegrityError, match="receipt lacks state revision"):
        replay(receipt_events)

    environment.capabilities = RuntimeCapabilities(
        idempotency=True, reconciliation=True, revisions=False
    )
    environment.state = StateRef("fake", "unrevisioned")
    unsupported = Session.create(
        "session-revisions-unsupported",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    runtime.state_revision = None
    unsupported.begin_attempt()
    unsupported.submit_action("inspect", {}, rationale="revision not required")

    with pytest.raises(ContractError, match="receipt lacks state revision"):
        revisioned.receive_receipt(
            ExecutionReceipt(
                "revision-required-late",
                runtime.proposals[-2].proposal_id,
                1,
                ExecutionStatus.FAILED,
                state=StateRef("fake", "missing-revision"),
            )
        )


def test_duplicate_action_operation_id_is_rejected_before_reservation(dependencies):
    from vharness.agent.transitions import intend_action, intend_evaluation

    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-duplicate-action-id",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-duplicate")
    session.submit_action("inspect", {}, rationale="first operation")
    operation = session._state.action_operations[0]

    with pytest.raises(TransitionError, match="proposal ID is already recorded"):
        intend_action(
            session._state,
            operation.proposal_id,
            operation.attempt_id,
            operation.reservation,
        )
    assert session._state.ledger.actions == 0
    assert len(session._state.action_operations) == 1

    attempt = session._state.active_attempt
    assert attempt is not None
    duplicate_request = EvaluationRequest(
        operation.proposal_id,
        session.session_id,
        session.view().task.objective_version,
        attempt.attempt_id,
        attempt.result_state or attempt.starting_state,
        session.view().lineage_head.node_id,
        session.view().task.evaluation_contract_id,
    )
    duplicate_reservation = ResourceReservation(
        "duplicate-evaluation-reservation",
        operation.proposal_id,
        session.view().task.objective_version,
        evaluations=1,
    )
    with pytest.raises(TransitionError, match="request ID is already recorded"):
        intend_evaluation(session._state, duplicate_request, duplicate_reservation)
    assert session._state.ledger.evaluations == 0


def test_malformed_reconciliation_receipt_is_rejected_before_persistence(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.raise_after_submit = True
    session = Session.create(
        "session-malformed-reconciliation",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(RuntimeError, match="transport interrupted"):
        session.submit_action("inspect", {}, rationale="recover receipt")
    proposal = runtime.proposals[0]
    runtime.raise_after_submit = False
    runtime.receipts[proposal.proposal_id] = replace(
        runtime.receipts[proposal.proposal_id], objective_version=2
    )

    with pytest.raises(TransitionError, match="objective version"):
        session.reconcile_pending()

    assert all(event.kind != "operation_reconciled" for event in session.events())


def test_reopen_consumes_evaluator_receipt_after_reconciliation_event_crash(
    dependencies,
):
    from vharness.agent.transitions import ReconciliationStatus, reconcile_evaluation

    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-reconciliation-crash",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    evaluator.raise_after_evaluate = True
    with pytest.raises(RuntimeError, match="external evaluation"):
        session.request_evaluation(action.state)
    evaluator.raise_after_evaluate = False
    operation = session._state.evaluation_operations[0]
    recovered = reconcile_evaluation(
        session._state,
        operation.request.request_id,
        ReconciliationStatus.RECEIPT,
        evaluator.last_receipt,
    )
    session._persist(
        "operation_reconciled",
        recovered,
        session._reconciliation_payload(
            operation.request.request_id,
            operation.kind,
            ReconciliationKind.RECEIPT,
            evaluator.last_receipt,
        ),
    )

    reopened = Session.open(
        session.session_id,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    reopened.reconcile_pending()

    assert reopened.view().pending_proposal_id is None
    assert reopened.view().lineage_head.evaluation_receipt_id is not None
    assert replay(reopened.events()) == reopened._state


def test_not_submitted_action_releases_budget_and_permits_new_operation(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        "session-not-submitted-action",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    runtime.raise_after_submit = True
    with pytest.raises(RuntimeError, match="transport interrupted"):
        session.submit_action("inspect", {}, rationale="interrupted")
    original = runtime.proposals[0]
    runtime.raise_after_submit = False
    runtime.reconciliation_kind = ReconciliationKind.NOT_SUBMITTED

    session.reconcile_pending()

    assert session.view().pending_proposal_id is None
    assert session._state.ledger.actions == 0
    assert session._state.action_operations[0].status.value == "not_submitted"
    replacement = session.submit_action("inspect", {}, rationale="resubmitted")
    assert replacement.proposal_id != original.proposal_id
    assert len(runtime.proposals) == 2
    reconciliation = next(
        event for event in session.events() if event.kind == "operation_reconciled"
    )
    assert reconciliation.payload == {
        "operation_id": original.proposal_id,
        "operation_kind": "action",
        "result": "not_submitted",
        "receipt": None,
    }


@pytest.mark.parametrize(
    "result", [ReconciliationKind.UNKNOWN, ReconciliationKind.UNSUPPORTED]
)
def test_unresolved_action_reconciliation_is_durable_and_idempotent(
    dependencies, result
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        f"session-unresolved-{result.value}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    runtime.raise_after_submit = True
    with pytest.raises(RuntimeError, match="transport interrupted"):
        session.submit_action("inspect", {}, rationale="interrupted")
    pending = runtime.proposals[0]
    runtime.raise_after_submit = False
    runtime.reconciliation_kind = result

    session.reconcile_pending()
    cursor = session.view().cursor
    session.reconcile_pending()

    operation = session._state.action_operations[0]
    assert operation.reconciliation.value == result.value
    assert session.view().pending_proposal_id == pending.proposal_id
    assert session.view().cursor == cursor
    assert replay(session.events()) == session._state


def test_reopen_reconciles_interrupted_evaluation_receipt(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-evaluator-recovery",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    evaluator.raise_after_evaluate = True
    with pytest.raises(RuntimeError, match="external evaluation"):
        session.request_evaluation(action.state)

    evaluator.raise_after_evaluate = False
    reopened = Session.open(
        "session-evaluator-recovery",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    reopened.reconcile_pending()

    assert reopened.view().lineage_head.evaluation_receipt_id is not None
    assert reopened.view().pending_proposal_id is None
    reconciliation = next(
        event for event in reopened.events() if event.kind == "operation_reconciled"
    )
    assert reconciliation.payload["operation_kind"] == "evaluation"
    assert reconciliation.payload["result"] == "receipt"
    assert reconciliation.payload["receipt"] is not None


def test_reopen_recovers_atomic_evaluation_consumption_and_promotion(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-atomic-evaluation-promotion",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    original_append_batch = journal.append_batch

    def fail_received_batch(events, *, expected_cursor):
        if any(event.kind == "evaluation_received" for event in events):
            raise PersistenceError("simulated evaluation batch crash")
        return original_append_batch(events, expected_cursor=expected_cursor)

    journal.append_batch = fail_received_batch
    with pytest.raises(PersistenceError, match="simulated evaluation batch crash"):
        session.request_evaluation(action.state)
    journal.append_batch = original_append_batch
    evaluator.reconciliation_kind = ReconciliationKind.UNKNOWN

    reopened = Session.open(
        "session-atomic-evaluation-promotion",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    reopened.reconcile_pending()

    kinds = [event.kind for event in reopened.events()]
    assert kinds.count("evaluation_received") == 1
    assert kinds.count("lineage_promoted") == 1
    assert reopened.view().lineage_head.evaluation_receipt_id is not None


def test_not_submitted_evaluation_releases_budget_and_permits_retry(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-not-submitted-evaluation",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    evaluator.raise_before_evaluate = True
    with pytest.raises(RuntimeError, match="before external evaluation"):
        session.request_evaluation(action.state)
    evaluator.raise_before_evaluate = False
    evaluator.reconciliation_kind = ReconciliationKind.NOT_SUBMITTED

    session.reconcile_pending()

    assert session.view().pending_proposal_id is None
    assert session._state.ledger.evaluations == 0
    assert session._state.evaluation_operations[0].status.value == "not_submitted"
    evaluator.reconciliation_kind = ReconciliationKind.RECEIPT
    session.request_evaluation(action.state)
    assert len(evaluator.requests) == 1


def test_terminal_unmeasured_action_conservatively_consumes_reservation(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-4",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    session.submit_action("inspect", {}, rationale="first")
    session.submit_action("inspect", {}, rationale="second")
    with pytest.raises(TransitionError, match="action budget exhausted"):
        session.submit_action("inspect", {}, rationale="third")
    assert session._state.ledger.actions == 0


def _reservation(
    operation_id: str, *, actions=1, evaluations=0, cost=None, model_calls=0
):
    return ResourceReservation(
        f"reservation-{operation_id}",
        operation_id,
        1,
        actions=actions,
        evaluations=evaluations,
        cost=cost,
        model_calls=model_calls,
    )


@pytest.mark.parametrize("measured_actions", [0, 1, 2])
def test_ledger_keeps_reservations_separate_from_measured_usage(measured_actions):
    reservation = _reservation("operation-a")
    ledger = UsageLedger().reserve(reservation, BudgetLimits(actions=1))

    assert ledger.actions == 0
    with pytest.raises(TransitionError, match="action budget exhausted"):
        ledger.reserve(_reservation("operation-b"), BudgetLimits(actions=1))

    settled = ledger.settle(
        reservation,
        UsageMeasurement("measurement-a", actions=measured_actions),
    )

    assert settled.actions == measured_actions
    assert settled.active_reservations == ()


def test_ledger_budget_uses_measured_usage_and_active_reservations():
    first = _reservation("operation-a")
    ledger = (
        UsageLedger()
        .reserve(first, BudgetLimits(actions=3))
        .record_measurement("operation-a", UsageMeasurement("measurement-a", actions=2))
    )

    with pytest.raises(TransitionError, match="action budget exhausted"):
        ledger.reserve(_reservation("operation-b"), BudgetLimits(actions=3))


def test_ledger_accepts_late_and_additional_terminal_measurements():
    reservation = _reservation("operation-a", cost=Decimal("1"))
    ledger = UsageLedger().reserve(reservation, BudgetLimits(cost=Decimal("3")))
    terminal = ledger.settle(reservation, None)

    assert terminal.cost is None
    assert terminal.unknown_cost_operation_ids == ("operation-a",)

    late = terminal.record_measurement(
        "operation-a", UsageMeasurement("measurement-a", actions=1, cost=Decimal("1"))
    )
    additional = late.record_measurement(
        "operation-a",
        UsageMeasurement("measurement-b", model_calls=2, cost=Decimal("0.5")),
    )

    assert additional.actions == 1
    assert additional.model_calls == 2
    assert additional.cost == Decimal("1.5")
    assert additional.unknown_cost_operation_ids == ()


def test_ledger_keeps_model_tokens_known_when_non_model_usage_is_partial():
    model_reservation = _reservation("model-operation", actions=0)
    action_reservation = _reservation("action-operation")
    ledger = (
        UsageLedger()
        .reserve(model_reservation, BudgetLimits())
        .reserve(action_reservation, BudgetLimits())
        .settle(
            model_reservation,
            UsageMeasurement(
                "model-measurement",
                model_calls=2,
                input_tokens=101,
                output_tokens=17,
                duration_ms=2300,
            ),
        )
        .settle(
            action_reservation,
            UsageMeasurement("action-measurement", actions=1),
        )
    )

    assert ledger.model_calls == 2
    assert (ledger.input_tokens, ledger.output_tokens, ledger.duration_ms) == (
        101,
        17,
        2300,
    )
    assert ledger.unknown_usage_operation_ids == ()
    assert ledger.unknown_cost_operation_ids == ("model-operation", "action-operation")


def test_ledger_marks_missing_model_telemetry_unknown_without_cost_requirement():
    reservation = _reservation("model-operation", actions=0)
    ledger = (
        UsageLedger()
        .reserve(reservation, BudgetLimits())
        .settle(
            reservation,
            UsageMeasurement("model-measurement", model_calls=1, output_tokens=5),
        )
    )

    assert ledger.unknown_usage_operation_ids == ("model-operation",)
    assert (ledger.input_tokens, ledger.output_tokens, ledger.duration_ms) == (
        None,
        None,
        None,
    )


def test_ledger_keeps_model_totals_unknown_for_unmeasured_model_reservation():
    complete = _reservation("model-complete", actions=0, model_calls=1)
    missing = _reservation("model-missing", actions=0, model_calls=1)
    ledger = (
        UsageLedger()
        .reserve(complete, BudgetLimits())
        .reserve(missing, BudgetLimits())
        .settle(
            complete,
            UsageMeasurement(
                "complete-measurement",
                model_calls=1,
                input_tokens=101,
                output_tokens=17,
                duration_ms=2300,
            ),
        )
        .settle(missing, None)
    )

    assert ledger.unknown_usage_operation_ids == ("model-missing",)
    assert (ledger.input_tokens, ledger.output_tokens, ledger.duration_ms) == (
        None,
        None,
        None,
    )


def test_ledger_preserves_incomplete_model_measurement_across_later_records():
    reservation = _reservation("model-operation", actions=0, model_calls=1)
    ledger = (
        UsageLedger()
        .reserve(reservation, BudgetLimits())
        .settle(
            reservation,
            UsageMeasurement(
                "incomplete-measurement",
                model_calls=1,
                input_tokens=101,
                output_tokens=17,
            ),
        )
    )

    later = ledger.record_measurement(
        "model-operation",
        UsageMeasurement(
            "complete-measurement",
            model_calls=1,
            input_tokens=103,
            output_tokens=19,
            duration_ms=2400,
        ),
    )
    later = later.record_measurement(
        "model-operation",
        UsageMeasurement("non-model-measurement", actions=1),
    )

    assert later.unknown_usage_operation_ids == ("model-operation",)
    assert (later.input_tokens, later.output_tokens, later.duration_ms) == (
        None,
        None,
        None,
    )


def test_ledger_measurement_identity_is_immutable_and_operation_owned():
    first = _reservation("operation-a")
    second = _reservation("operation-b")
    ledger = (
        UsageLedger().reserve(first, BudgetLimits()).reserve(second, BudgetLimits())
    )
    measurement = UsageMeasurement("measurement-a", actions=1)
    recorded = ledger.record_measurement("operation-a", measurement)

    assert recorded.record_measurement("operation-a", measurement) == recorded
    with pytest.raises(TransitionError, match="ownership or content"):
        recorded.record_measurement("operation-a", UsageMeasurement("measurement-a"))
    with pytest.raises(TransitionError, match="ownership or content"):
        recorded.record_measurement("operation-b", measurement)


def test_ledger_not_submitted_releases_without_manufacturing_usage():
    reservation = _reservation("operation-a")
    released = (
        UsageLedger().reserve(reservation, BudgetLimits(actions=1)).release(reservation)
    )

    assert released.actions == 0
    assert released.active_reservations == ()
    assert released.released_operation_ids == ("operation-a",)


def test_ledger_cost_budget_blocks_terminal_unknown_exposure():
    reservation = _reservation("operation-a", cost=Decimal("1"))
    terminal = (
        UsageLedger()
        .reserve(reservation, BudgetLimits(cost=Decimal("2")))
        .settle(reservation, None)
    )

    with pytest.raises(TransitionError, match="unknown exposure"):
        terminal.reserve(
            _reservation("operation-b", cost=Decimal("1")),
            BudgetLimits(cost=Decimal("2")),
        )


def test_additional_unknown_cost_measurement_blocks_cost_admission():
    reservation = _reservation("operation-a", cost=Decimal("1"))
    ledger = (
        UsageLedger()
        .reserve(reservation, BudgetLimits(cost=Decimal("3")))
        .settle(
            reservation,
            UsageMeasurement("measurement-known", cost=Decimal("1")),
        )
    )
    unknown = ledger.record_measurement(
        "operation-a", UsageMeasurement("measurement-unknown")
    )

    with pytest.raises(TransitionError, match="unknown exposure"):
        unknown.reserve(
            _reservation("operation-b", cost=Decimal("1")),
            BudgetLimits(cost=Decimal("3")),
        )


def test_known_incremental_cost_does_not_resolve_prior_unknown_measurement():
    reservation = _reservation("operation-a", cost=Decimal("1"))
    unknown = (
        UsageLedger()
        .reserve(reservation, BudgetLimits(cost=Decimal("3")))
        .settle(
            reservation,
            UsageMeasurement("measurement-unknown"),
        )
    )
    mixed = unknown.record_measurement(
        "operation-a", UsageMeasurement("measurement-known", cost=Decimal("1"))
    )

    assert mixed.cost is None
    with pytest.raises(TransitionError, match="unknown exposure"):
        mixed.reserve(
            _reservation("operation-b", cost=Decimal("1")),
            BudgetLimits(cost=Decimal("3")),
        )


def test_terminal_without_measurement_resolves_only_with_complete_usage():
    reservation = _reservation("operation-a", cost=Decimal("1"))
    terminal = (
        UsageLedger().reserve(reservation, BudgetLimits()).settle(reservation, None)
    )

    assert (
        terminal.input_tokens,
        terminal.output_tokens,
        terminal.duration_ms,
        terminal.cost,
    ) == (None, None, None, None)

    resolved = terminal.record_measurement(
        "operation-a",
        UsageMeasurement(
            "measurement-complete",
            input_tokens=0,
            output_tokens=0,
            duration_ms=0,
            cost=Decimal("0"),
        ),
    )

    assert (
        resolved.input_tokens,
        resolved.output_tokens,
        resolved.duration_ms,
        resolved.cost,
    ) == (0, 0, 0, Decimal("0"))


def test_partial_late_measurement_keeps_conservative_terminal_exposure():
    reservation = _reservation(
        "partially-measured",
        actions=1,
        evaluations=1,
        model_calls=1,
    )
    terminal = (
        UsageLedger()
        .reserve(
            reservation,
            BudgetLimits(actions=1, evaluations=1, model_calls=1),
        )
        .settle(reservation, None)
    )

    partial = terminal.record_measurement(
        "partially-measured",
        UsageMeasurement(
            "partial-measurement",
            actions=0,
            evaluations=None,
            model_calls=None,
        ),
    )

    assert partial.actions == 0
    assert partial.unknown_terminal_operation_ids == ("partially-measured",)
    with pytest.raises(TransitionError, match="action budget exhausted"):
        partial.reserve(
            _reservation("next-operation", actions=1),
            BudgetLimits(actions=1),
        )


def test_ledger_distinguishes_unknown_from_measured_zero_optional_usage():
    reservation = _reservation("operation-a")
    zero = (
        UsageLedger()
        .reserve(reservation, BudgetLimits())
        .settle(
            reservation,
            UsageMeasurement(
                "measurement-zero",
                input_tokens=0,
                output_tokens=0,
                duration_ms=0,
                cost=Decimal("0"),
            ),
        )
    )
    unknown_reservation = _reservation("operation-b")
    unknown = (
        UsageLedger()
        .reserve(unknown_reservation, BudgetLimits())
        .settle(unknown_reservation, UsageMeasurement("measurement-unknown"))
    )

    assert (zero.input_tokens, zero.output_tokens, zero.duration_ms, zero.cost) == (
        0,
        0,
        0,
        Decimal("0"),
    )
    assert (
        unknown.input_tokens,
        unknown.output_tokens,
        unknown.duration_ms,
        unknown.cost,
    ) == (None, None, None, None)


def test_late_terminal_measurement_is_replayed_and_deduplicated(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-late-measurement",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    terminal = session.submit_action("inspect", {}, rationale="terminal")
    measurement = UsageMeasurement(
        "late-measurement",
        actions=2,
        model_calls=1,
        input_tokens=3,
        output_tokens=5,
        duration_ms=7,
        cost=Decimal("0.25"),
    )
    late = ExecutionReceipt(
        "late-measurement-receipt",
        terminal.proposal_id,
        terminal.objective_version,
        ExecutionStatus.SUCCEEDED,
        usage=measurement,
    )
    session.receive_receipt(late)
    session.receive_receipt(
        replace(late, receipt_id="late-measurement-duplicate-receipt")
    )

    ledger = session._state.ledger
    assert (
        ledger.actions,
        ledger.model_calls,
        ledger.input_tokens,
        ledger.output_tokens,
        ledger.duration_ms,
        ledger.cost,
    ) == (2, 1, 3, 5, 7, Decimal("0.25"))
    assert replay(session.events()) == session._state
    reopened = Session.open(
        "session-late-measurement",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert reopened._state == session._state


def test_conflicting_late_measurement_is_rejected(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-conflicting-measurement",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    terminal = session.submit_action("inspect", {}, rationale="terminal")
    session.receive_receipt(
        ExecutionReceipt(
            "measurement-receipt-a",
            terminal.proposal_id,
            terminal.objective_version,
            ExecutionStatus.SUCCEEDED,
            usage=UsageMeasurement("measurement-a", actions=1),
        )
    )

    with pytest.raises(ContractError, match="ownership or content"):
        session.receive_receipt(
            ExecutionReceipt(
                "measurement-receipt-b",
                terminal.proposal_id,
                terminal.objective_version,
                ExecutionStatus.SUCCEEDED,
                usage=UsageMeasurement("measurement-a", actions=2),
            )
        )


def test_legacy_usage_payload_omits_new_optional_values():
    usage = usage_from_payload(
        {
            "measurement_id": "legacy-measurement",
            "actions": 1,
            "model_calls": 0,
            "evaluations": 0,
            "cost": "0.1",
        }
    )

    assert (usage.input_tokens, usage.output_tokens, usage.duration_ms) == (
        None,
        None,
        None,
    )


def test_ledger_facts_change_checkpoint_projection_digest():
    reservation = _reservation("operation-a", cost=Decimal("1"))
    reserved = UsageLedger().reserve(reservation, BudgetLimits(cost=Decimal("2")))
    settled = reserved.settle(reservation, None)
    measured = settled.record_measurement(
        "operation-a", UsageMeasurement("measurement-a", cost=Decimal("1"))
    )

    assert (
        len(
            {
                canonical_json({"ledger": ledger})
                for ledger in (UsageLedger(), reserved, settled, measured)
            }
        )
        == 4
    )


def test_rejected_evaluation_remains_attempt_history_without_lineage_promotion(
    dependencies,
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evaluator.accepted = False
    session = Session.create(
        "session-5",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)

    assert session.view().lineage_head.node_id == "root-session-5"
    assert session.view().active_attempt is None
    assert session.attempts()[0].attempt.disposition.value == "rejected"
    assert session.events()[-1].kind == "evaluation_received"


@pytest.mark.parametrize(
    ("valid", "hard_constraints", "disposition"),
    [
        (False, {}, "invalid"),
        (True, {"must_compile": False}, "hard_constraint_failed"),
    ],
)
def test_invalid_or_hard_constraint_evaluation_does_not_promote(
    dependencies, valid, hard_constraints, disposition
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evaluator.valid = valid
    evaluator.hard_constraints = hard_constraints
    session = Session.create(
        f"session-evaluation-{disposition}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(action.state)

    received = next(
        event for event in session.events() if event.kind == "evaluation_received"
    )
    assert received.payload["disposition"] == disposition
    assert session.view().lineage_head.node_id.startswith("root-")


def test_finite_completion_requires_verified_external_evidence(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evidence = artifacts.put(b"completed", media_type="text/plain", provenance="fake")
    evaluator.completed = True
    evaluator.completion_evidence = (evidence,)
    evaluator.required_evidence = (evidence.digest,)
    session = Session.create(
        "session-finite-completion",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(action.state)

    assert session.view().status is SessionStatus.COMPLETED


def test_ongoing_objective_does_not_complete_from_external_completion_evidence(
    dependencies,
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evidence = artifacts.put(b"completed", media_type="text/plain", provenance="fake")
    evaluator.completed = True
    evaluator.completion_evidence = (evidence,)
    task = replace(_task(), completion_mode=CompletionMode.ONGOING)
    session = Session.create(
        "session-ongoing-completion",
        task,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(action.state)

    assert session.view().status is SessionStatus.RUNNING


def test_evaluation_usage_settles_and_late_duplicates_do_not_charge_twice(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    evaluator.usage = UsageMeasurement("evaluation-usage", evaluations=2)
    session = Session.create(
        "session-evaluation-usage",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    action = session.submit_action("inspect", {}, rationale="candidate")
    receipt = session.request_evaluation(action.state)
    usage = UsageMeasurement("late-evaluation-usage", evaluations=3)
    session.receive_evaluation_usage(receipt.request_id, usage)
    session.receive_evaluation_usage(receipt.request_id, usage)

    assert session._state.ledger.evaluations == 5
    assert replay(session.events()) == session._state


def test_checkpoint_hashes_the_projection_before_writing_its_marker(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-6",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )

    checkpoint = session.checkpoint()

    assert checkpoint.cursor == 1
    assert len(checkpoint.projection_digest) == 64
    assert session.view().cursor == 2
    assert session.events()[-1].kind == "checkpoint"


def test_replay_rejects_a_checkpoint_with_a_wrong_prefix_digest(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-checkpoint-corrupt",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.checkpoint()
    events = list(session.events())
    checkpoint = events[-1]
    corrupted_payload = dict(checkpoint.payload)
    corrupted_checkpoint = dict(corrupted_payload["checkpoint"])
    corrupted_checkpoint["projection_digest"] = "0" * 64
    corrupted_payload["checkpoint"] = corrupted_checkpoint
    events[-1] = replace(checkpoint, payload=corrupted_payload)
    with pytest.raises(IntegrityError, match="digest"):
        replay(events)


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("wrong_session", "session identity"),
        ("wrong_objective", "objective version"),
        ("missing_pending", "pending operation IDs"),
        ("extra_pending", "pending operation IDs"),
    ],
)
def test_replay_rejects_invalid_checkpoint_envelope(dependencies, corruption, message):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        "session-checkpoint-envelope",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    session.submit_action("inspect", {}, rationale="pending operation")
    session.checkpoint()
    events = list(session.events())
    checkpoint = events[-1]
    payload = dict(checkpoint.payload)
    envelope = dict(payload["checkpoint"])
    if corruption == "wrong_session":
        envelope["session_id"] = "forged-session"
    elif corruption == "wrong_objective":
        envelope["objective_version"] = 2
    elif corruption == "missing_pending":
        envelope["pending_operation_ids"] = []
    else:
        envelope["pending_operation_ids"] = [
            *envelope["pending_operation_ids"],
            "forged-operation",
        ]
    payload["checkpoint"] = envelope
    events[-1] = replace(checkpoint, payload=payload)

    with pytest.raises(IntegrityError, match=message):
        replay(events)


def test_checkpoint_digest_covers_operation_and_evaluation_replay_state(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        "session-checkpoint-operation-state",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    session.submit_action("inspect", {}, rationale="pending operation")
    session.checkpoint()
    events = list(session.events())
    checkpoint = events[-1]
    from vharness.agent.replay import projection_payload

    projection = dict(projection_payload(session._state))
    projection.pop("operations")
    legacy_digest = hashlib.sha256(canonical_json(projection).encode()).hexdigest()
    payload = dict(checkpoint.payload)
    checkpoint_data = dict(payload["checkpoint"])
    checkpoint_data["projection_digest"] = legacy_digest
    payload["checkpoint"] = checkpoint_data
    events[-1] = replace(checkpoint, payload=payload)
    with pytest.raises(IntegrityError, match="digest"):
        replay(events)


def test_replay_rejects_an_intake_with_mismatched_receipt_identity(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-intake-identity",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)
    events = list(session.events())
    intake_index = next(
        index for index, event in enumerate(events) if event.kind == "evaluation_intake"
    )
    payload = dict(events[intake_index].payload)
    payload["receipt_id"] = "forged-receipt"
    events[intake_index] = replace(events[intake_index], payload=payload)
    with pytest.raises(IntegrityError, match="receipt identity"):
        replay(events)


def test_duplicate_receipts_are_idempotent_but_conflicting_reuse_is_rejected(
    dependencies,
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-7",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="deduplicate receipt")
    cursor = session.view().cursor

    assert session.receive_receipt(receipt).cursor == cursor
    conflicting = ExecutionReceipt(
        receipt_id=receipt.receipt_id,
        proposal_id=receipt.proposal_id,
        objective_version=receipt.objective_version,
        status=ExecutionStatus.FAILED,
    )
    with pytest.raises(ContractError, match="different content"):
        session.receive_receipt(conflicting)


def test_reopen_reconciles_submit_interruption_without_duplicate_effect(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.raise_after_submit = True
    session = Session.create(
        "session-8",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    with pytest.raises(RuntimeError, match="transport interrupted"):
        session.submit_action("inspect", {}, rationale="recover interrupted submit")

    runtime.raise_after_submit = False
    reopened = Session.open(
        "session-8",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    reopened.reconcile_pending()

    assert len(runtime.proposals) == 1
    assert reopened.view().pending_proposal_id is None
    assert reopened.events()[-1].kind == "receipt_received"


def test_event_contract_has_no_authoritative_projection_snapshot(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-event-contract",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-contract")
    assert all("snapshot" not in event.payload for event in session.events())


def test_replay_rejects_invalid_command_causation(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-causation-corrupt",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.enqueue(Pause("pause-causation", "session-causation-corrupt"))
    session.advance()
    events = list(session.events())
    paused = next(event for event in events if event.kind == "session_paused")
    events[events.index(paused)] = replace(paused, causation_id="wrong-command")
    with pytest.raises(IntegrityError, match="causation"):
        replay(events)


def test_replay_rejects_illegal_command_transition(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-command-transition-corrupt",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    pause = Pause("pause-transition", "session-command-transition-corrupt")
    session.enqueue(pause)
    session.advance()
    events = list(session.events())
    paused = next(event for event in events if event.kind == "session_paused")
    repeated = replace(paused, event_id="event-second-pause", sequence=len(events) + 1)
    with pytest.raises(IntegrityError, match="legal command transition"):
        replay([*events, repeated])


def test_replay_rejects_illegal_attempt_and_objective_transitions(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-transition-corrupt",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-transition")
    events = list(session.events())
    repeated = replace(events[-1], event_id="event-repeated", sequence=3)
    with pytest.raises(IntegrityError, match="attempt_started"):
        replay([*events, repeated])
    from vharness.agent.models import Steer

    session.enqueue(
        Steer(
            "steer-transition",
            "session-transition-corrupt",
            "new objective",
            "new evidence",
        )
    )
    session.advance()
    steered_events = list(session.events())
    regressed = replace(steered_events[-1], objective_version=1)
    with pytest.raises(IntegrityError, match="regresses"):
        replay([*steered_events[:-1], regressed])


def test_replay_rejects_invalid_lineage_parent(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-lineage-corrupt",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)
    events = list(session.events())
    promoted = events[-1]
    payload = dict(promoted.payload)
    node = dict(payload["node"])
    node["parent_id"] = "wrong-parent"
    payload["node"] = node
    events[-1] = replace(promoted, payload=payload)
    with pytest.raises(IntegrityError, match="not derived"):
        replay(events)


def test_late_receipt_for_promoted_attempt_does_not_change_new_attempt(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime.status = ExecutionStatus.ACCEPTED
    session = Session.create(
        "session-late-attempt",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-a")
    pending = session.submit_action("inspect", {}, rationale="candidate")
    terminal = ExecutionReceipt(
        "receipt-a-terminal",
        pending.proposal_id,
        1,
        ExecutionStatus.SUCCEEDED,
        state=StateRef("fake", "a-result", digest="2" * 64),
    )
    session.receive_receipt(terminal)
    session.request_evaluation(terminal.state)
    session.begin_attempt("attempt-b")
    session.receive_receipt(
        ExecutionReceipt(
            "receipt-a-late",
            pending.proposal_id,
            1,
            ExecutionStatus.SUCCEEDED,
            state=StateRef("fake", "a-late-result", digest="3" * 64),
        )
    )

    assert session.view().active_attempt is not None
    assert session.view().active_attempt.attempt_id == "attempt-b"
    assert session.view().active_attempt.result_state is None
    reopened = Session.open(
        "session-late-attempt",
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    assert reopened.view() == session.view()


def test_replay_rejects_promotion_without_matching_evaluation(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-promotion-missing-evaluation",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)
    events = list(session.events())
    promoted = events[-1]
    receipt_event_index = next(
        index for index, event in enumerate(events) if event.kind == "receipt_received"
    )
    forged = replace(promoted, sequence=receipt_event_index + 2)
    with pytest.raises(IntegrityError, match="durably verified|matching accepted"):
        replay([*events[: receipt_event_index + 1], forged])


@pytest.mark.parametrize(
    ("field", "value", "disposition"),
    [
        ("accepted", False, "rejected"),
        ("comparison", "incomparable", "incomparable"),
        ("evaluator_version", "other-evaluator", "stale"),
    ],
)
def test_replay_rejects_nonpromotable_evaluation(
    dependencies, field, value, disposition
):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        f"session-promotion-{disposition}",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)
    events = list(session.events())
    evaluation_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "evaluation_received"
    )
    payload = dict(events[evaluation_index].payload)
    evaluation_receipt = dict(payload["receipt"])
    evaluation_receipt[field] = value
    payload["receipt"] = evaluation_receipt
    payload["disposition"] = disposition
    events[evaluation_index] = replace(events[evaluation_index], payload=payload)
    with pytest.raises(IntegrityError, match="durably verified|matching accepted"):
        replay(events)


def test_replay_rejects_forged_promotion_receipt_state_and_status(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-promotion-forged-fields",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt()
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)
    events = list(session.events())
    promoted = events[-1]
    payload = dict(promoted.payload)
    payload["evaluation_receipt_id"] = "missing-receipt"
    events[-1] = replace(promoted, payload=payload)
    with pytest.raises(IntegrityError, match="receipt identity"):
        replay(events)

    payload = dict(promoted.payload)
    node = dict(payload["node"])
    node["state"] = {"owner": "fake", "value": "forged"}
    payload["node"] = node
    events[-1] = replace(promoted, payload=payload)
    with pytest.raises(IntegrityError, match="not derived"):
        replay(events)

    payload = dict(promoted.payload)
    payload["status"] = "completed"
    events[-1] = replace(promoted, payload=payload)
    with pytest.raises(IntegrityError, match="unexpected or missing"):
        replay(events)


def test_replay_rejects_self_asserted_correlation_fields(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    session = Session.create(
        "session-forged-correlation",
        _task(),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-a")
    receipt = session.submit_action("inspect", {}, rationale="candidate")
    session.request_evaluation(receipt.state)
    events = list(session.events())

    receipt_index = next(
        index for index, event in enumerate(events) if event.kind == "receipt_received"
    )
    payload = dict(events[receipt_index].payload)
    payload["operation"] = {"attempt_id": "forged"}
    events[receipt_index] = replace(events[receipt_index], payload=payload)
    with pytest.raises(IntegrityError, match="unexpected or missing"):
        replay(events)

    events = list(session.events())
    evaluation_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "evaluation_received"
    )
    payload = dict(events[evaluation_index].payload)
    payload["request"] = {"request_id": "forged"}
    events[evaluation_index] = replace(events[evaluation_index], payload=payload)
    with pytest.raises(IntegrityError, match="unexpected or missing"):
        replay(events)


def _append_event(events, kind, payload, *, objective_version=None, causation_id=None):
    return [
        *events,
        replace(
            events[-1],
            event_id=f"event-{kind}-{len(events) + 1}",
            sequence=len(events) + 1,
            kind=kind,
            objective_version=(
                events[-1].objective_version
                if objective_version is None
                else objective_version
            ),
            payload=payload,
            causation_id=causation_id,
        ),
    ]


def _evaluation_a_intake_prefix(dependencies, *, evaluations=1):
    journal, artifacts, environment, runtime, evaluator = dependencies
    task = replace(_task(), budgets=BudgetLimits(actions=2, evaluations=evaluations))
    session = Session.create(
        f"session-evaluation-lifecycle-{evaluations}",
        task,
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-a")
    action = session.submit_action("inspect", {}, rationale="candidate a")
    session.request_evaluation(action.state)
    events = list(session.events())
    intake_index = next(
        index for index, event in enumerate(events) if event.kind == "evaluation_intake"
    )
    received = next(event for event in events if event.kind == "evaluation_received")
    stale_receipt = dict(received.payload["receipt"])
    stale_receipt["evaluator_version"] = "stale-evaluator"
    intake = events[intake_index]
    intake_payload = dict(intake.payload)
    intake_payload["raw_receipt"] = canonical_json(stale_receipt)
    events[intake_index] = replace(intake, payload=intake_payload)
    return events[: intake_index + 1], stale_receipt


def _newer_pending_event(events, kind):
    from vharness.agent.models import Steer
    from vharness.agent.transitions import start_attempt

    steer = Steer(
        "steer-a-to-b",
        events[0].session_id,
        "objective b",
        "evidence b",
    )
    events = _append_event(
        events,
        "command_admitted",
        {"command": parse_json_object(canonical_json(steer))},
    )
    events = _append_event(
        events,
        "objective_steered",
        {"command": parse_json_object(canonical_json(steer))},
        objective_version=2,
        causation_id=steer.command_id,
    )
    attempt = start_attempt(replay(events), "attempt-b")
    events = _append_event(
        events,
        "attempt_started",
        {"attempt": parse_json_object(canonical_json(attempt.active_attempt))},
        objective_version=2,
    )
    if kind == "action":
        template = next(event for event in events if event.kind == "action_intended")
        proposal = dict(template.payload["proposal"])
        reservation = dict(template.payload["reservation"])
        proposal["proposal_id"] = "proposal-b"
        proposal["attempt_id"] = "attempt-b"
        proposal["objective_version"] = 2
        reservation["reservation_id"] = "reservation-b"
        reservation["operation_id"] = "proposal-b"
        reservation["objective_version"] = 2
        return _append_event(
            events,
            "action_intended",
            {"proposal": proposal, "reservation": reservation},
            objective_version=2,
        )
    template = next(event for event in events if event.kind == "evaluation_intended")
    request = dict(template.payload["request"])
    reservation = dict(template.payload["reservation"])
    request["request_id"] = "evaluation-b"
    request["attempt_id"] = "attempt-b"
    request["objective_version"] = 2
    reservation["reservation_id"] = "reservation-b"
    reservation["operation_id"] = "evaluation-b"
    reservation["objective_version"] = 2
    return _append_event(
        events,
        "evaluation_intended",
        {"request": request, "reservation": reservation},
        objective_version=2,
    )


def _late_a_receipt(events, receipt):
    return _append_event(
        events,
        "evaluation_received",
        {"receipt": receipt, "disposition": "stale"},
        objective_version=1,
    )


@pytest.mark.parametrize("pending_kind", ["action", "evaluation"])
def test_late_stale_evaluation_preserves_newer_pending_work(dependencies, pending_kind):
    prefix, stale_receipt = _evaluation_a_intake_prefix(
        dependencies, evaluations=2 if pending_kind == "evaluation" else 1
    )
    with_pending = _newer_pending_event(prefix, pending_kind)
    before = replay(with_pending)
    result = replay(_late_a_receipt(with_pending, stale_receipt))

    assert result.pending_proposal_id == before.pending_proposal_id
    assert result.wait_reason == before.wait_reason
    assert result.status is before.status
    assert result.evaluation_operations[0].status.value == "receipt_consumed"


@pytest.mark.parametrize("duplicate_kind", ["intake", "receipt"])
def test_duplicate_terminal_evaluation_evidence_is_rejected_without_side_effects(
    dependencies, duplicate_kind
):
    prefix, stale_receipt = _evaluation_a_intake_prefix(dependencies)
    with_pending = _newer_pending_event(prefix, "action")
    consumed = _late_a_receipt(with_pending, stale_receipt)
    state = replay(consumed)
    duplicate = prefix[-1] if duplicate_kind == "intake" else consumed[-1]
    duplicate = replace(
        duplicate,
        event_id=f"event-duplicate-{duplicate_kind}",
        sequence=len(consumed) + 1,
    )

    with pytest.raises(IntegrityError, match="consumed receipt|already terminal"):
        reduce_event(state, duplicate)
    with pytest.raises(IntegrityError, match="consumed receipt|already terminal"):
        replay([*consumed, duplicate])
    assert replay(consumed).pending_proposal_id == state.pending_proposal_id
    assert replay(consumed).wait_reason == state.wait_reason
    assert replay(consumed).status is state.status
    assert replay(consumed).ledger == state.ledger


def _lifecycle_events(dependencies, lifecycle):
    prefix, receipt = _evaluation_a_intake_prefix(dependencies)
    if lifecycle == "intended":
        return prefix[:-1]
    if lifecycle == "intake_recorded":
        return prefix
    if lifecycle == "verification_failed":
        return _append_event(
            prefix,
            "evaluation_verification_failed",
            {
                "request_id": prefix[-1].payload["request_id"],
                "receipt_id": prefix[-1].payload["receipt_id"],
                "reason": "artifact missing",
            },
        )
    return _late_a_receipt(prefix, receipt)


@pytest.mark.parametrize(
    "lifecycle",
    ["intended", "intake_recorded", "verification_failed", "receipt_consumed"],
)
def test_open_reconstructs_each_evaluation_lifecycle(dependencies, tmp_path, lifecycle):
    journal, artifacts, environment, runtime, evaluator = dependencies
    events = _lifecycle_events(dependencies, lifecycle)
    reopened_journal = SqliteJournal(tmp_path / f"{lifecycle}.sqlite3")
    try:
        reopened_journal.append(events, expected_cursor=0)
        opened = Session.open(
            events[0].session_id,
            journal=reopened_journal,
            artifacts=artifacts,
            environment=environment,
            runtime=runtime,
            evaluator=evaluator,
        )
        assert opened._state == replay(events)
    finally:
        reopened_journal.close()
        journal.close()


def test_verification_failure_clears_its_own_wait_and_preserves_terminal_statuses(
    dependencies,
):
    from vharness.agent.transitions import fail_evaluation_verification

    prefix, _receipt = _evaluation_a_intake_prefix(dependencies)
    intake = replay(prefix)
    request_id = prefix[-1].payload["request_id"]
    receipt_id = prefix[-1].payload["receipt_id"]
    cleared = fail_evaluation_verification(intake, request_id, receipt_id)
    assert cleared.pending_proposal_id is None
    assert cleared.wait_reason is None
    assert cleared.status is SessionStatus.RUNNING
    for status in (SessionStatus.PAUSED, SessionStatus.STOPPED):
        terminal = fail_evaluation_verification(
            replace(intake, status=status), request_id, receipt_id
        )
        assert terminal.status is status
        assert terminal.pending_proposal_id is None


@pytest.mark.parametrize("lifecycle", ["verification_failed", "receipt_consumed"])
def test_checkpoint_digest_covers_each_terminal_evaluation_branch(
    dependencies, lifecycle
):
    events = _lifecycle_events(dependencies, lifecycle)
    terminal_state = replay(events)
    before_terminal = replay(events[:-1])
    digest = hashlib.sha256(
        canonical_json(projection_payload(terminal_state)).encode()
    ).hexdigest()
    checkpoint = _append_event(
        events,
        "checkpoint",
        {
            "checkpoint": {
                "checkpoint_id": f"checkpoint-{lifecycle}",
                "session_id": events[0].session_id,
                "objective_version": 1,
                "cursor": len(events),
                "projection_digest": digest,
                "pending_operation_ids": [],
            }
        },
    )

    assert (
        digest
        != hashlib.sha256(
            canonical_json(projection_payload(before_terminal)).encode()
        ).hexdigest()
    )
    assert replay(checkpoint) == terminal_state
    corrupted = list(checkpoint)
    if lifecycle == "verification_failed":
        corrupted[-2] = replace(
            corrupted[-2],
            kind="evaluation_received",
            payload={
                "receipt": parse_json_object(events[-2].payload["raw_receipt"]),
                "disposition": "stale",
            },
        )
    else:
        intake = events[-2]
        corrupted[-2] = replace(
            corrupted[-2],
            kind="evaluation_verification_failed",
            payload={
                "request_id": intake.payload["request_id"],
                "receipt_id": intake.payload["receipt_id"],
                "reason": "corrupted",
            },
        )
    with pytest.raises(IntegrityError, match="digest"):
        replay(corrupted)


class ArtifactRuntime(FakeRuntime):
    """Returns receipts whose stdout lives in the artifact store.

    Succeeded receipts carry a real artifact reference; with
    ``missing_artifact`` the reference points at bytes the store never held,
    so excerpt resolution must degrade without breaking the session.
    """

    def __init__(
        self,
        artifacts: ArtifactStore,
        outputs: tuple[bytes, ...],
        *,
        missing_artifact: bool = False,
    ) -> None:
        super().__init__()
        self._artifacts = artifacts
        self._outputs = outputs
        self._missing_artifact = missing_artifact

    def submit(self, proposal):
        self.proposals.append(proposal)
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{proposal.proposal_id}",
            proposal_id=proposal.proposal_id,
            objective_version=proposal.objective_version,
            status=self.status,
            state=StateRef(
                "fake",
                f"after-{proposal.proposal_id}",
                digest="1" * 64,
            ),
        )
        if self.status is ExecutionStatus.SUCCEEDED:
            if self._missing_artifact:
                raw_artifact = ArtifactRef(
                    "f" * 64, 16, "application/json", "process-runtime-result"
                )
            else:
                data = self._outputs[len(self.receipts) % len(self._outputs)]
                raw_artifact = self._artifacts.put(
                    data,
                    media_type="application/json",
                    provenance="process-runtime-result",
                )
            receipt = replace(receipt, raw_artifact=raw_artifact)
        self.receipts[proposal.proposal_id] = receipt
        return receipt


def _action_budget_task(actions: int) -> TaskSpec:
    return TaskSpec(
        task_id="task-1",
        objective="inspect the fixture",
        completion_mode=CompletionMode.FINITE,
        success_evidence="external evaluator acceptance",
        budgets=BudgetLimits(actions=actions, evaluations=1),
        environment_id="fake",
        evaluation_contract_id="fake-evaluator-v1",
    )


def test_receipt_content_map_resolves_bounded_recent_receipts(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime = ArtifactRuntime(
        artifacts,
        (
            b'{"status": "ok", "stdout": "total 4      share"}',
            b'{"status": "ok", "stdout": "open port 8080"}',
        ),
    )
    session = Session.create(
        "session-receipt-content",
        _action_budget_task(4),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-1")
    session.submit_action("inspect", {}, rationale="probe one")
    session.submit_action("inspect", {}, rationale="probe two")

    content = session._receipt_content_map(session.events())  # noqa: SLF001
    assert len(content) == 2
    excerpts = sorted(entry.excerpt for entry in content.values())
    assert excerpts == ["open port 8080", "total 4 share"]
    assert all(
        entry.normalized_fingerprint is not None
        and entry.normalized_fingerprint.startswith("stdout-normalized:")
        for entry in content.values()
    )
    reads = [
        event
        for event in session.events()
        if event.kind == "artifact_read"
        and event.payload.get("purpose") == "context_receipt_content"
    ]
    assert len(reads) == 2


def test_receipt_content_map_bounds_receipts_and_skips_unusable_ones(dependencies):
    journal, artifacts, environment, runtime, evaluator = dependencies
    runtime = ArtifactRuntime(artifacts, (b'{"stdout": "probe output"}',))
    session = Session.create(
        "session-receipt-content-bounds",
        _action_budget_task(12),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=runtime,
        evaluator=evaluator,
    )
    session.begin_attempt("attempt-1")
    for _ in range(RECEIPT_CONTENT_MAX_RECEIPTS + 1):
        session.submit_action("inspect", {}, rationale="probe")
    content = session._receipt_content_map(session.events())  # noqa: SLF001
    assert len(content) == RECEIPT_CONTENT_MAX_RECEIPTS
    assert f"receipt-{runtime.proposals[0].proposal_id}" not in content

    runtime.status = ExecutionStatus.FAILED
    session.submit_action("inspect", {}, rationale="failed probe")
    failed_proposal = runtime.proposals[-1].proposal_id
    content = session._receipt_content_map(session.events())  # noqa: SLF001
    assert f"receipt-{failed_proposal}" not in content
    assert len(content) == RECEIPT_CONTENT_MAX_RECEIPTS

    missing_runtime = ArtifactRuntime(
        artifacts, (b'{"stdout": "lost output"}',), missing_artifact=True
    )
    missing_session = Session.create(
        "session-receipt-content-missing",
        _action_budget_task(4),
        journal=journal,
        artifacts=artifacts,
        environment=environment,
        runtime=missing_runtime,
        evaluator=evaluator,
    )
    missing_session.begin_attempt("attempt-1")
    missing_session.submit_action("inspect", {}, rationale="lost probe")
    content = missing_session._receipt_content_map(  # noqa: SLF001
        missing_session.events()
    )
    # The unreadable artifact contributes no excerpt and no digest: the
    # receipt renders without content instead of blocking the decision.
    assert content == {}
